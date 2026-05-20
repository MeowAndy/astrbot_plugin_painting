import asyncio
import base64
import json
import mimetypes
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import aiohttp

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp


PLUGIN_ID = "astrbot_plugin_painting"
GITHUB_PRESET_URL = "https://raw.githubusercontent.com/MeowAndy/astrbot_plugin_painting/master/presets/ht.json"


@register(
    PLUGIN_ID,
    "MeowAndy",
    "AI 画图插件：预设画图、#bnn 自定义画图、次数管理、API 余额查询。",
    "1.0.0",
    "https://github.com/MeowAndy/astrbot_plugin_painting",
)
class PaintingPlugin(Star):
    """Painting.js 的 AstrBot 移植版。

    重点：API Key、模型、超时时间等均由 AstrBot Web 控制台的插件配置页管理，
    不在代码中硬编码密钥。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path("data") / "plugins" / PLUGIN_ID
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir = self.data_dir / "generated_images"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.temp_image_dir = self.data_dir / "temp_images"
        self.temp_image_dir.mkdir(parents=True, exist_ok=True)
        self.presets: List[Dict[str, Any]] = []
        self.preset_reg: Optional[re.Pattern[str]] = None
        asyncio.create_task(self._init_presets())

    # =========================
    # Config helpers
    # =========================

    def cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)  # type: ignore[attr-defined]
        except Exception:
            try:
                value = self.config[key]  # type: ignore[index]
            except Exception:
                value = default
        return default if value is None else value

    @property
    def bot_name(self) -> str:
        return str(self.cfg("bot_name", "菲比"))

    @property
    def timeout(self) -> int:
        try:
            return max(5, int(self.cfg("api_timeout", 240)))
        except Exception:
            return 240

    @property
    def command_prefix(self) -> str:
        """画图指令前缀（唤醒词），可在 Web 面板修改。

        这里的前缀是插件自己的独立前缀，不依赖 AstrBot 全局命令前缀。
        例如填 `xy`，用户应直接发送 `xybnn` / `xy预设关键词`。
        """
        raw = str(self.cfg("command_prefix", "#")).strip()
        return raw if raw else "#"

    def painting_prefix_candidates(self) -> List[str]:
        """返回可用于 bnn/预设的插件前缀候选。

        - Web 填 `xy`：主用 `xy`，兼容 `#xy` `/xy` `!xy`
        - Web 填 `#xy`：主用 `#xy`，兼容 `xy`
        - Web 填默认 `#`：只使用 `#`
        """
        raw = self.command_prefix
        candidates = {raw}
        if len(raw) > 1 and raw[0] in "#/!":
            candidates.add(raw[1:])
        elif raw not in {"#", "/", "!"}:
            candidates.update({f"#{raw}", f"/{raw}", f"!{raw}"})
        return sorted((x for x in candidates if x), key=len, reverse=True)

    def strip_painting_prefix(self, msg: str) -> Optional[str]:
        msg = (msg or "").strip()
        for prefix in self.painting_prefix_candidates():
            if msg.startswith(prefix):
                return msg[len(prefix):].strip()
        return None

    @property
    def bnn_max_count(self) -> int:
        try:
            return max(1, int(self.cfg("bnn_max_count", 5)))
        except Exception:
            return 5

    @property
    def initial_user_count(self) -> int:
        try:
            return max(0, int(self.cfg("initial_user_count", 10)))
        except Exception:
            return 10

    def api_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg('api_key', '')}",
            "Content-Type": "application/json",
        }

    def check_api_key(self) -> Optional[str]:
        api_key = str(self.cfg("api_key", "")).strip()
        if not api_key:
            return "⚠️ 尚未配置 API Key，请到 AstrBot 网页控制台 → 插件管理 → Painting 画图 中填写。"
        return None

    # =========================
    # Event / permission helpers
    # =========================

    def text(self, event: AstrMessageEvent) -> str:
        return (getattr(event, "message_str", "") or "").strip()

    def sender_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:
            return str(getattr(event, "sender_id", "") or "")

    def is_admin(self, event: AstrMessageEvent) -> bool:
        # AstrBot 不同版本/平台字段略有差异，这里做兼容判断。
        for attr in ("is_admin", "is_master", "is_admin_event"):
            value = getattr(event, attr, None)
            try:
                if callable(value) and value():
                    return True
                if value is True:
                    return True
            except Exception:
                pass

        sender = self.sender_id(event)
        raw = str(self.cfg("master_ids", "")).strip()
        masters = {x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()}
        return bool(sender and sender in masters)

    def is_group(self, event: AstrMessageEvent) -> bool:
        umo = str(getattr(event, "unified_msg_origin", "") or "").lower()
        if "group" in umo:
            return True
        for attr in ("group_id", "get_group_id"):
            value = getattr(event, attr, None)
            try:
                if callable(value) and value():
                    return True
                if value:
                    return True
            except Exception:
                pass
        return False

    def group_id(self, event: AstrMessageEvent) -> Optional[str]:
        for attr in ("get_group_id", "group_id"):
            value = getattr(event, attr, None)
            try:
                gid = value() if callable(value) else value
                if gid:
                    return str(gid)
            except Exception:
                pass
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        match = re.search(r"group[:_/-](\d+)", umo, re.I)
        return match.group(1) if match else None

    def usage_group_id(self, event: AstrMessageEvent) -> Optional[str]:
        return self.group_id(event) if self.is_group(event) else None

    def usage_user_id(self, event: AstrMessageEvent) -> str:
        return f"user_{self.sender_id(event)}"

    async def send_text(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(event.plain_result(text))

    async def send_images(self, event: AstrMessageEvent, image_paths: Iterable[Path], text: str = "") -> None:
        paths = list(image_paths)
        chain: List[Any] = []
        for path in paths:
            chain.append(Comp.Image.fromFileSystem(str(path)))
        if text:
            chain.append(Comp.Plain(text))
        await event.send(event.chain_result(chain))
        # 关闭存图时生成的是临时发送文件，发送后清理；开启存图时文件位于 generated_images，不删除。
        for path in paths:
            try:
                if path.parent == self.temp_image_dir and path.exists():
                    path.unlink()
            except Exception:
                pass

    # =========================
    # KV storage
    # =========================

    async def kv_get(self, key: str, default: Any) -> Any:
        try:
            value = await self.get_kv_data(key, default)
            return default if value is None else value
        except Exception:
            return default

    async def kv_put(self, key: str, value: Any) -> None:
        try:
            await self.put_kv_data(key, value)
        except Exception:
            # 兜底文件存储，避免老版本 AstrBot KV 异常时完全不可用。
            path = self.data_dir / f"{key}.json"
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    async def get_counts(self) -> Dict[str, int]:
        data = await self.kv_get("usage_counts", {})
        return data if isinstance(data, dict) else {}

    async def put_counts(self, data: Dict[str, int]) -> None:
        await self.kv_put("usage_counts", data)

    async def get_usage_count(self, key: Optional[str]) -> int:
        if not key:
            return 0
        data = await self.get_counts()
        if key not in data:
            data[key] = self.initial_user_count
            await self.put_counts(data)
        try:
            return int(data.get(key, 0))
        except Exception:
            return 0

    async def set_usage_count(self, key: Optional[str], count: int) -> None:
        if not key:
            return
        data = await self.get_counts()
        data[key] = max(0, int(count))
        await self.put_counts(data)

    async def decrease_usage_count(self, key: Optional[str], count: int = 1) -> None:
        if not key:
            return
        current = await self.get_usage_count(key)
        await self.set_usage_count(key, max(0, current - count))

    async def daily_stats(self) -> Dict[str, Any]:
        today = date.today().isoformat()
        data = await self.kv_get("daily_stats", {})
        if not isinstance(data, dict) or not data:
            data = {"date": today, "totalGenerated": 0, "historyTotal": 0, "lastReset": datetime.now().isoformat(), "daily": {}}
            await self.kv_put("daily_stats", data)
            return data
        data.setdefault("daily", {})
        if not isinstance(data.get("daily"), dict):
            data["daily"] = {}
        if data.get("date") != today:
            data["historyTotal"] = int(data.get("historyTotal", 0)) + int(data.get("totalGenerated", 0))
            data["date"] = today
            data["totalGenerated"] = 0
            data["lastReset"] = datetime.now().isoformat()
            await self.kv_put("daily_stats", data)
        data.setdefault("historyTotal", 0)
        data.setdefault("totalGenerated", 0)
        data["daily"].setdefault(today, int(data.get("totalGenerated", 0)))
        return data

    async def increase_today_count(self, n: int = 1) -> int:
        today = date.today().isoformat()
        data = await self.daily_stats()
        data["totalGenerated"] = int(data.get("totalGenerated", 0)) + n
        daily = data.setdefault("daily", {})
        daily[today] = int(daily.get(today, 0)) + n
        await self.kv_put("daily_stats", data)
        return int(data["totalGenerated"])

    async def save_img_enabled(self) -> bool:
        return bool(await self.kv_get("save_img_enabled", False))

    async def set_save_img_enabled(self, enabled: bool) -> None:
        await self.kv_put("save_img_enabled", bool(enabled))

    # =========================
    # Presets
    # =========================

    async def _init_presets(self) -> None:
        saved = await self.kv_get("presets", [])
        if isinstance(saved, list) and saved:
            self.presets = saved
            self.rebuild_preset_regex()
            return

        # 首次安装插件时，先立刻加载仓库内置焚决，避免远程网络不可用/较慢导致没有预设。
        if await self.load_builtin_presets():
            print(f"[Painting] ✅ 首次启动已安装内置焚决预设 {len(self.presets)} 条。")
            return

        await self.fetch_presets()

    async def load_builtin_presets(self) -> bool:
        builtin = Path(__file__).parent / "presets" / "ht.json"
        if not builtin.exists():
            return False
        try:
            data = json.loads(builtin.read_text(encoding="utf-8"))
            if not isinstance(data, list) or not data:
                return False
            self.presets = data
            self.rebuild_preset_regex()
            await self.kv_put("presets", data)
            return True
        except Exception as exc:
            print(f"[Painting] ❎ 读取内置焚决预设失败: {exc}")
            return False

    def rebuild_preset_regex(self) -> None:
        keywords: List[str] = []
        for preset in self.presets:
            for key in preset.get("keywords", []) or []:
                if key:
                    keywords.append(re.escape(str(key)))
        # 不再在 regex 里硬编码前缀，匹配去掉前缀后的纯关键词部分
        self.preset_reg = re.compile(rf"^({'|'.join(keywords)})(?:@(\d+)|(\d+))?$", re.S) if keywords else None

    async def fetch_presets(self) -> bool:
        """多源拉取焚决预设：GitHub → 用户配置 URL → 内置文件。"""
        user_url = str(self.cfg("preset_json_url", "https://ht.pippi.top/pippi.json")).strip()
        sources = [
            ("GitHub", GITHUB_PRESET_URL),
        ]
        if user_url:
            sources.append(("云端(pippi)", user_url))

        for name, url in sources:
            try:
                print(f"[Painting] 正在从 {name} 拉取焚决预设...")
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                    async with session.get(url) as resp:
                        if resp.status >= 400:
                            raise RuntimeError(f"HTTP {resp.status}")
                        data = await resp.json(content_type=None)
                if not isinstance(data, list) or not data:
                    raise RuntimeError("数据格式错误或为空")
                self.presets = data
                self.rebuild_preset_regex()
                await self.kv_put("presets", data)
                print(f"[Painting] ✅ 从 {name} 更新成功，已加载 {len(self.presets)} 条焚决预设。")
                return True
            except Exception as exc:
                print(f"[Painting] ⚠️ 从 {name} 拉取失败: {exc}，尝试下一个源...")

        # 所有远程源失败，尝试内置文件
        if await self.load_builtin_presets():
            print(f"[Painting] ✅ 远程源均不可用，已从内置预设加载 {len(self.presets)} 条焚决。")
            return True

        print("[Painting] ❎ 所有焚决预设源均不可用！")
        return False

    # =========================
    # HTTP / image helpers
    # =========================

    def normalize_api_url(self, raw: str, endpoint: str, base_raw: str = "") -> str:
        """Accept either a full endpoint URL or a base URL and return a full API endpoint.

        Some users paste only a New API/OpenAI-compatible base URL such as
        `https://example.com` or `https://example.com/v1`. This helper makes the
        image/chat endpoints explicit and rejects relative/incomplete values with
        a readable error instead of surfacing aiohttp's confusing `Invalid URL`.
        """
        url = (raw or "").strip()
        if not url:
            return ""
        if not re.match(r"^https?://", url, re.I):
            if url.startswith("/") and re.match(r"^https?://", (base_raw or "").strip(), re.I):
                base = urlparse((base_raw or "").strip())
                url = urlunparse((base.scheme, base.netloc, url, "", "", ""))
            else:
                raise RuntimeError(f"接口地址必须以 http:// 或 https:// 开头，当前为：{url}")

        endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        endpoint_tail = endpoint.rstrip("/")

        if path.endswith(endpoint_tail):
            return url
        if endpoint_tail.endswith("/images/generations"):
            if path.endswith("/v1"):
                path = f"{path}/images/generations"
            elif path.endswith("/v1/images"):
                path = f"{path}/generations"
            else:
                path = f"{path}/v1/images/generations" if path else endpoint_tail
        elif endpoint_tail.endswith("/chat/completions"):
            if path.endswith("/v1"):
                path = f"{path}/chat/completions"
            elif path.endswith("/v1/chat"):
                path = f"{path}/completions"
            else:
                path = f"{path}/v1/chat/completions" if path else endpoint_tail
        else:
            path = f"{path}{endpoint}" if path else endpoint_tail
        return urlunparse(parsed._replace(path=path))

    async def request_json(self, method: str, url: str, *, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
            async with session.request(method, url, headers=self.api_headers(), json=payload) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except Exception:
                    raise RuntimeError(f"响应不是 JSON：HTTP {resp.status} {text[:300]}")
                if resp.status >= 400:
                    msg = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
                    raise RuntimeError(str(msg or f"HTTP {resp.status}"))
                return data

    async def download_bytes(self, url: str) -> Tuple[bytes, str]:
        if url.startswith("data:image"):
            header, b64 = url.split(",", 1)
            ext = ".png" if "png" in header else ".webp" if "webp" in header else ".gif" if "gif" in header else ".jpg"
            return base64.b64decode(b64), ext
        if url.startswith("base64://"):
            return base64.b64decode(url[len("base64://") :]), ".png"
        if url.startswith("file://"):
            path = url[7:]
            return Path(path).read_bytes(), Path(path).suffix or ".jpg"
        if os.path.exists(url):
            return Path(url).read_bytes(), Path(url).suffix or ".jpg"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=min(self.timeout, 120))) as session:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"下载图片失败 HTTP {resp.status}")
                content = await resp.read()
                ctype = resp.headers.get("content-type", "")
        ext = mimetypes.guess_extension(ctype.split(";")[0]) or Path(urlparse(url).path).suffix or ".jpg"
        return content, ext

    async def url_to_data_url(self, url: str) -> str:
        content, ext = await self.download_bytes(url)
        ext_l = ext.lower()
        if ext_l == ".png":
            mime = "image/png"
        elif ext_l == ".webp":
            mime = "image/webp"
        elif ext_l == ".gif":
            mime = "image/gif"
        else:
            mime = "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(content).decode()}"

    async def save_generated_image(self, item: str, prefix: str = "painting") -> Path:
        content, ext = await self.download_bytes(item)
        if ext.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            ext = ".png"
        target_dir = self.image_dir if await self.save_img_enabled() else self.temp_image_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{prefix}_{int(time.time() * 1000)}{ext}"
        path.write_bytes(content)
        return path

    def extract_images_from_response(self, data: Dict[str, Any]) -> List[str]:
        images: List[str] = []
        msg = (((data.get("choices") or [{}])[0]).get("message") or {}) if isinstance(data, dict) else {}
        content = msg.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "image_url" and isinstance(item.get("image_url"), dict):
                        url = item["image_url"].get("url")
                        if url:
                            images.append(url)
                    elif item.get("type") in {"output_image", "image"}:
                        url = item.get("url") or item.get("b64_json")
                        if url:
                            images.append(url if str(url).startswith(("http", "data:", "base64://")) else f"base64://{url}")
        elif isinstance(content, str):
            for url in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", content):
                images.append(url)
            for b64 in re.findall(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", content):
                images.append(b64)
        for item in data.get("data", []) or []:
            if isinstance(item, dict):
                if item.get("b64_json"):
                    images.append("base64://" + item["b64_json"])
                elif item.get("url"):
                    images.append(item["url"])
        for item in data.get("output", []) or []:
            if isinstance(item, dict) and item.get("type") == "image_generation_call" and item.get("result"):
                images.append("base64://" + item["result"])
        nested = msg.get("images")
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    url = item.get("url") or (item.get("image_url") or {}).get("url")
                    if url:
                        images.append(url)
        return images

    def _image_value_to_url(self, value: Any) -> Optional[str]:
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text

    def _extract_image_urls_from_component(self, comp: Any, urls: List[str], depth: int = 0) -> None:
        """递归提取消息链中的图片，包含引用消息 Reply.chain。

        AstrBot 的 aiocqhttp 适配器会把 OneBot 的 reply 段转换为 Reply 组件，
        被引用消息的消息链放在 Reply.chain 里。之前只扫描了当前消息顶层
        message chain，所以“引用一张图再发 #bnn/#预设”会拿不到图片。
        """
        if comp is None or depth > 3:
            return

        # dict 形式兼容（部分平台/旧版本可能直接保留原始 segment dict）
        if isinstance(comp, dict):
            typ = str(comp.get("type", "")).lower()
            data = comp.get("data") if isinstance(comp.get("data"), dict) else comp
            if "image" in typ:
                image_url = data.get("image_url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                value = data.get("url") or data.get("file") or data.get("path") or image_url
                value = self._image_value_to_url(value)
                if value:
                    urls.append(value)
            # reply / node / forward 等嵌套消息链
            for key in ("chain", "message", "messages", "nodes"):
                nested = data.get(key) if isinstance(data, dict) else None
                if isinstance(nested, list):
                    for item in nested:
                        self._extract_image_urls_from_component(item, urls, depth + 1)
            return

        typ = str(getattr(comp, "type", "") or getattr(comp, "__class__", type(comp)).__name__).lower()
        cls_name = str(getattr(comp, "__class__", type(comp)).__name__).lower()

        if "image" in typ or "image" in cls_name:
            for attr in ("url", "file", "path", "image_url"):
                value = getattr(comp, attr, None)
                if isinstance(value, dict):
                    value = value.get("url")
                value = self._image_value_to_url(value)
                if value:
                    urls.append(value)
                    break

        # 重点：AstrBot Reply 组件的 chain 保存被引用消息的消息段列表。
        for attr in ("chain", "message", "messages", "nodes"):
            nested = getattr(comp, attr, None)
            if isinstance(nested, list):
                for item in nested:
                    self._extract_image_urls_from_component(item, urls, depth + 1)

    def extract_incoming_image_urls(self, event: AstrMessageEvent) -> List[str]:
        urls: List[str] = []
        msg_obj = getattr(event, "message_obj", None)
        chain = getattr(msg_obj, "message", None) or getattr(event, "message_chain", None) or []
        try:
            iterable = list(chain)
        except Exception:
            iterable = []
        for comp in iterable:
            self._extract_image_urls_from_component(comp, urls)

        # 再兜底扫 raw_message（适配器异常/旧版本没填 Reply.chain 时也尽量找图）。
        raw = getattr(msg_obj, "raw_message", None)
        raw_msg = None
        try:
            raw_msg = raw.get("message") if hasattr(raw, "get") else None
        except Exception:
            raw_msg = None
        if isinstance(raw_msg, list):
            for comp in raw_msg:
                self._extract_image_urls_from_component(comp, urls)

        # 兜底从文本里提取 URL
        for url in re.findall(r"https?://\S+", self.text(event)):
            if re.search(r"\.(png|jpe?g|webp|gif)(\?|$)", url, re.I):
                urls.append(url)
        return list(dict.fromkeys(urls))

    async def image_urls_as_content(self, urls: List[str], max_images: int = 5) -> Tuple[List[Dict[str, Any]], int]:
        content: List[Dict[str, Any]] = []
        failed = 0
        for url in urls[:max_images]:
            ok = False
            last_exc: Optional[Exception] = None
            for _ in range(2):
                try:
                    data_url = await self.url_to_data_url(url)
                    content.append({"type": "image_url", "image_url": {"url": data_url}})
                    ok = True
                    break
                except Exception as exc:
                    last_exc = exc
                    await asyncio.sleep(1)
            if not ok:
                failed += 1
                print(f"[Painting] 图片转 base64 失败，跳过：{url} / {last_exc}")
        return content, failed

    # =========================
    # Usage consumption
    # =========================

    async def available_count(self, event: AstrMessageEvent) -> Tuple[int, int, int]:
        gid = self.usage_group_id(event)
        uid = self.usage_user_id(event)
        group_count = await self.get_usage_count(gid) if gid else 0
        user_count = await self.get_usage_count(uid)
        return group_count + user_count, group_count, user_count

    async def ensure_enough_count(self, event: AstrMessageEvent, need: int = 1) -> bool:
        if self.is_admin(event):
            return True
        total, _, _ = await self.available_count(event)
        if total >= need:
            return True
        if not self.is_group(event):
            await self.send_text(event, f"呜呜，这个魔法需要消耗次数哦，你的专属次数不足啦，快去请主人充值吧~ 🎀")
        else:
            await self.send_text(event, f"哎呀，本群和你的专属魔法次数都已经用完啦，快去请主人给{self.bot_name}充值吧~ ✨")
        return False

    async def consume_count_and_summary(self, event: AstrMessageEvent, cost: int) -> str:
        today = await self.increase_today_count(cost)
        if self.is_admin(event):
            return f"\n📊 全服作画：{today}张\n👑 主人拥有无限魔法！"
        gid = self.usage_group_id(event)
        uid = self.usage_user_id(event)
        remaining = cost
        group_count = await self.get_usage_count(gid) if gid else 0
        if gid and group_count >= remaining:
            await self.decrease_usage_count(gid, remaining)
            return f"\n📊 全服作画：{today}张\n🎁 本群魔法余量：{group_count - remaining}次"
        if gid and group_count > 0:
            await self.decrease_usage_count(gid, group_count)
            remaining -= group_count
        user_count = await self.get_usage_count(uid)
        await self.decrease_usage_count(uid, remaining)
        return f"\n📊 全服作画：{today}张\n🎁 你的专属魔法余量：{max(0, user_count - remaining)}次"

    # =========================
    # Commands
    # =========================

    def is_update_presets_command(self, msg: str) -> bool:
        msg = (msg or "").strip()
        if not msg:
            return False
        candidates = {"更新焚决", "更新焚诀", "绘图更新预设"}
        if msg in candidates:
            return True
        for prefix in {self.command_prefix, "#", "/", "!"}:
            if prefix and msg.startswith(prefix) and msg[len(prefix):].strip() in candidates:
                return True
        return False

    @filter.regex(r"^.{0,10}(更新焚[决诀]|绘图更新预设)$")
    async def update_resources(self, event: AstrMessageEvent):
        if not self.is_update_presets_command(self.text(event)):
            return
        if not self.is_admin(event):
            yield event.plain_result(f"哼唧，只有主人才能更新{self.bot_name}的魔法书哦~ 🙅‍♀️")
            return
        yield event.plain_result("叮咚~ 更新焚诀中~请稍后...\n正在拉取最新焚决预设（GitHub → 云端 → 内置）")
        ok = await self.fetch_presets()
        if ok:
            yield event.plain_result(f"✅ 魔法书更新成功！\n已加载 {len(self.presets)} 条神奇咒语。✨")
        else:
            yield event.plain_result("更新失败啦，所有源均不可用，请查看后台控制台日志。🥺")

    def is_help_command(self, msg: str) -> bool:
        msg = (msg or "").strip()
        if not msg:
            return False
        target = "绘图帮助"
        if msg == target:
            return True
        for prefix in {self.command_prefix, "#", "/", "!"}:
            if prefix and msg.startswith(prefix) and msg[len(prefix):].strip() == target:
                return True
        return False

    async def send_forward_texts(self, event: AstrMessageEvent, parts: List[str], title: str = "魔法手册") -> bool:
        """尽量用合并转发/合并消息发送；失败返回 False 由调用方降级普通文本。"""
        try:
            nodes = []
            bot_uin = self.sender_id(event) or "0"
            for text in parts:
                nodes.append(Comp.Node(content=[Comp.Plain(text)], name=self.bot_name, uin=bot_uin))
            await event.send(event.chain_result([Comp.Nodes(nodes=nodes)]))
            return True
        except Exception as exc:
            print(f"[Painting] 合并消息发送失败，降级普通文本：{exc}")
            return False

    def build_help_parts(self) -> List[str]:
        preset_lines = []
        for idx, preset in enumerate(self.presets, 1):
            keys = " / ".join(str(x) for x in preset.get("keywords", []) or [])
            if keys:
                preset_lines.append(f"{idx}. #{keys}")
        preset_text = "\n".join(preset_lines) if preset_lines else "暂无本地预设，请发送 #更新焚决 获取。"
        p = self.command_prefix
        return [
            f"🎨 {self.bot_name}Painting魔法使用帮助：",
            f"📌 当前画图前缀（独立唤醒词）：{p}\n填 xy 时直接发送 xybnn / xy预设，不需要再加 #。",
            f"📌 基础咒语（读取自焚决，会消耗群魔法/个人魔法）：\n{preset_text}",
            f"📌 创作咒语：\n{p}bnn <提示词> [图片] - {self.bot_name}看图作画\n{p}bnn <提示词> - {self.bot_name}闭眼想象作画（纯文生图）\n{p}bnn3 <提示词> - 一次生成 3 张（最多 {self.bnn_max_count} 张）\n\n📌 次数与魔法机制：\n优先消耗【群次数】。如果群次数不足，{self.bot_name}会自动检查【你的个人专属次数】哦！",
            "📌 主人专属指令：\n#绘图更新预设 / #更新焚决 / #更新焚诀 - 拉取最新画图咒语\n#绘图增加次数 <数量> [@某人/uQQ号/群号] - 给群或个人充能\n#绘图查询/删除所有次数 - 管理全服魔法账本\n#绘图删除次数 [@某人/uQQ号/群号] - 清空某个记录\n#查询额度 / #查余额 - 查询 API 中转站余额状态\n#开启bnn存图 - 开启本地存图\n#关闭bnn存图 - 关闭本地存图（默认关闭）",
            "📌 大家都可以用的：\n#绘图查询次数 - 看看群里和个人的魔法余量\n#排行bnn - 查看本周每日作画统计排行",
        ]

    @filter.regex(r"^.{0,10}绘图帮助$")
    async def show_help(self, event: AstrMessageEvent):
        if not self.is_help_command(self.text(event)):
            return
        parts = self.build_help_parts()
        if await self.send_forward_texts(event, parts, f"{self.bot_name}的魔法使用帮助"):
            return
        yield event.plain_result("\n\n".join(parts))

    def strip_command_body(self, msg: str, *commands: str) -> Optional[str]:
        msg = (msg or "").strip()
        prefixes = {"", "#", "/", "!", *self.painting_prefix_candidates()}
        for prefix in prefixes:
            for command in commands:
                full = f"{prefix}{command}"
                if msg == full:
                    return ""
                if msg.startswith(full):
                    return msg[len(full):].strip()
        return None

    def at_target_user(self, event: AstrMessageEvent) -> Optional[str]:
        msg_obj = getattr(event, "message_obj", None)
        chain = getattr(msg_obj, "message", None) or getattr(event, "message_chain", None) or []
        try:
            iterable = list(chain)
        except Exception:
            iterable = []
        for comp in iterable:
            if isinstance(comp, dict):
                typ = str(comp.get("type", "")).lower()
                data = comp.get("data") if isinstance(comp.get("data"), dict) else comp
                if typ == "at" or "at" in typ:
                    qq = data.get("qq") or data.get("user_id") or data.get("id")
                    if qq and str(qq).lower() != "all":
                        return str(qq)
                continue
            typ = str(getattr(comp, "type", "") or getattr(comp, "__class__", type(comp)).__name__).lower()
            if "at" in typ:
                qq = getattr(comp, "qq", None) or getattr(comp, "user_id", None) or getattr(comp, "id", None)
                if qq and str(qq).lower() != "all":
                    return str(qq)
        return None

    def parse_positive_count_and_rest(self, raw: str) -> Tuple[Optional[int], str]:
        raw = (raw or "").strip()
        match = re.match(r"^([0-9０-９]+)", raw)
        if not match:
            return None, raw
        digits = match.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        try:
            count = int(digits)
        except Exception:
            return None, raw
        if count <= 0:
            return None, raw
        return count, raw[match.end():].strip()

    def _normalize_digits(self, text: str) -> str:
        return text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))

    def parse_add_count_and_target(self, event: AstrMessageEvent, raw: str) -> Tuple[Optional[int], str]:
        """解析加次数参数，兼容 Yunzai 的 count-first，也兼容 @目标 count。

        支持：
        - #绘图增加次数 10
        - #绘图增加次数10
        - #绘图增加次数 10 u123 / 10 群号 / 10 @某人
        - #绘图增加次数@某人 10
        - #绘图增加次数 u123 10
        """
        raw = (raw or "").strip()

        # 标准 Yunzai 写法：次数在最前面。
        count, rest = self.parse_positive_count_and_rest(raw)
        if count is not None:
            return count, rest

        # @某人 10：消息链里能拿到 at，次数取文本中的最后一个数字，避免 CQ:at 里的 qq 号干扰。
        if self.at_target_user(event):
            nums = list(re.finditer(r"[0-9０-９]+", raw))
            if nums:
                m = nums[-1]
                digits = self._normalize_digits(m.group(0))
                count = int(digits)
                if count > 0:
                    rest = (raw[:m.start()] + raw[m.end():]).strip()
                    return count, rest

        # uQQ号 10：目标在前，次数在后。
        m = re.match(r"^(u[0-9０-９]+)\s+([0-9０-９]+)\s*$", raw, re.I)
        if m:
            count = int(self._normalize_digits(m.group(2)))
            if count > 0:
                return count, m.group(1)

        return None, raw

    def parse_usage_target(self, event: AstrMessageEvent, raw: str, allow_default: bool = True) -> Tuple[Optional[str], str]:
        at_uid = self.at_target_user(event)
        if at_uid:
            return f"user_{at_uid}", f"用户 {at_uid}"

        raw = (raw or "").strip()
        if raw:
            first = raw.split()[0]
            if first.lower().startswith("u"):
                uid_match = re.search(r"\d+", first)
                if uid_match:
                    uid = uid_match.group(0)
                    return f"user_{uid}", f"用户 {uid}"
            gid_match = re.search(r"\d+", first)
            if gid_match:
                gid = gid_match.group(0)
                return gid, f"群 {gid}"

        if allow_default:
            if self.is_group(event):
                return self.usage_group_id(event), "本群"
            return self.usage_user_id(event), "你(专属)"
        return None, ""

    @filter.regex(r"^.{0,10}绘图查询次数.*$")
    async def query_usage_count(self, event: AstrMessageEvent):
        raw = self.strip_command_body(self.text(event), "绘图查询次数")
        if raw is None:
            return
        stats = await self.daily_stats()
        target_id = None
        label = ""
        at_uid = self.at_target_user(event)
        if at_uid:
            target_id, label = f"user_{at_uid}", f"用户 {at_uid}"
        elif self.is_admin(event) and raw:
            target_id, label = self.parse_usage_target(event, raw, allow_default=False)
        if not target_id:
            if self.is_group(event):
                gid = self.usage_group_id(event)
                group_count = await self.get_usage_count(gid)
                user_count = await self.get_usage_count(self.usage_user_id(event))
                yield event.plain_result(
                    f"本群的剩余魔法次数：{group_count} 次 ✨\n"
                    f"你个人的专属魔法次数：{user_count} 次 🎁\n\n"
                    f"📊 {self.bot_name}今日全服作画：{stats.get('totalGenerated', 0)}张\n"
                    f"🏆 {self.bot_name}历史总共作画：{stats.get('historyTotal', 0)}张"
                )
                return
            target_id, label = self.usage_user_id(event), "你的专属"
        count = await self.get_usage_count(target_id)
        yield event.plain_result(
            f"{label} 的剩余魔法次数：{count} 次 ✨\n\n"
            f"📊 {self.bot_name}今日全服作画：{stats.get('totalGenerated', 0)}张\n"
            f"🏆 {self.bot_name}历史总共作画：{stats.get('historyTotal', 0)}张"
        )

    @filter.regex(r"^.{0,10}绘图增加次数.*$")
    async def add_usage_count(self, event: AstrMessageEvent):
        raw = self.strip_command_body(self.text(event), "绘图增加次数")
        if raw is None:
            return
        if not self.is_admin(event):
            yield event.plain_result(f"哼唧，这个是主人的专属魔法，{self.bot_name}不能听你的哦~ 🙅‍♀️")
            return
        count, target_raw = self.parse_add_count_and_target(event, raw)
        if count is None:
            yield event.plain_result("唔...充值的次数必须是正整数才行呀！✨")
            return
        key, label = self.parse_usage_target(event, target_raw, allow_default=True)
        if not key:
            yield event.plain_result(f"{self.bot_name}不知道你要给谁充值，请指定一下哦：#绘图增加次数 <数量> <群号/uQQ号/@某人> ✨")
            return
        current = await self.get_usage_count(key)
        await self.set_usage_count(key, current + count)
        yield event.plain_result(f"好耶！{self.bot_name}已经为 {label} 增加了 {count} 次魔法✨\n🎁 当前剩余：{current + count} 次哟~ 💖")

    @filter.regex(r"^.{0,10}绘图查询(所有|全部)次数$")
    async def query_all_counts(self, event: AstrMessageEvent):
        raw = self.strip_command_body(self.text(event), "绘图查询所有次数", "绘图查询全部次数")
        if raw is None:
            return
        if not self.is_admin(event):
            yield event.plain_result(f"哼唧，这个是主人的专属魔法，{self.bot_name}不能听你的哦~ 🙅‍♀️")
            return
        data = await self.get_counts()
        items = sorted(((k, int(v)) for k, v in data.items() if int(v) > 0), key=lambda x: x[1], reverse=True)
        if not items:
            yield event.plain_result(f"报告主人！{self.bot_name}的账本上还没有任何次数记录哦~ 📝")
            return
        total = sum(v for _, v in items)
        lines = [f"📊 {self.bot_name}的魔法账本：", f"总计分配: {total} 次", "----------------"]
        for i, (key, value) in enumerate(items[:80], 1):
            label = f"用户 {key[5:]}" if key.startswith("user_") else f"群 {key}"
            lines.append(f"{i}. {label}: {value} 次")
        if len(items) > 80:
            lines.append(f"...以及其他 {len(items) - 80} 个目标")
        yield event.plain_result("\n".join(lines))

    @filter.regex(r"^.{0,10}绘图删除(所有|全部)次数$")
    async def delete_all_counts(self, event: AstrMessageEvent):
        raw = self.strip_command_body(self.text(event), "绘图删除所有次数", "绘图删除全部次数")
        if raw is None:
            return
        if not self.is_admin(event):
            yield event.plain_result(f"哼唧，这个是主人的专属魔法，{self.bot_name}不能听你的哦~ 🙅‍♀️")
            return
        await self.put_counts({})
        yield event.plain_result("✅ 已清空所有绘图次数记录。")

    @filter.regex(r"^.{0,10}绘图删除次数.*$")
    async def delete_usage_count(self, event: AstrMessageEvent):
        raw = self.strip_command_body(self.text(event), "绘图删除次数")
        if raw is None:
            return
        if not self.is_admin(event):
            yield event.plain_result(f"哼唧，这个是主人的专属魔法，{self.bot_name}不能听你的哦~ 🙅‍♀️")
            return
        key, label = self.parse_usage_target(event, raw, allow_default=self.is_group(event))
        if not key:
            yield event.plain_result(f"{self.bot_name}不知道你要清零谁，请指定一下哦：#绘图删除次数 <群号/uQQ号/@某人> 🧹")
            return
        data = await self.get_counts()
        if key in data:
            data.pop(key, None)
            await self.put_counts(data)
            yield event.plain_result(f"呼~ {self.bot_name}已经把 {label} 的魔法次数清空啦！🧹")
        else:
            yield event.plain_result(f"咦？{label} 还没有{self.bot_name}的次数记录呢 🐾")

    @filter.regex(r"^[\s\S]{1,30}bnn(\d*)\s+([\s\S]+)$")
    async def make_bnn(self, event: AstrMessageEvent, *args, **kwargs):
        msg = self.text(event)
        after_prefix = self.strip_painting_prefix(msg)
        if after_prefix is None:
            return
        match = re.match(r"^bnn(\d*)\s+([\s\S]+)$", after_prefix)
        if not match:
            return
        api_err = self.check_api_key()
        if api_err:
            yield event.plain_result(api_err)
            return
        gen_count = int(match.group(1) or 1)
        if gen_count < 1:
            gen_count = 1
        if gen_count > self.bnn_max_count:
            gen_count = self.bnn_max_count
            yield event.plain_result(f"最多一次生成 {self.bnn_max_count} 张哦，{self.bot_name}帮你调整到 {self.bnn_max_count} 张啦~ 🎨")
        prompt = match.group(2).strip()
        if not await self.ensure_enough_count(event, gen_count):
            return
        if not self.is_admin(event):
            total, _, _ = await self.available_count(event)
            if total < gen_count:
                gen_count = total
                yield event.plain_result(f"你的剩余次数({total})不够生成这么多张哦，{self.bot_name}帮你调整到 {total} 张~ 🎁")
                if gen_count <= 0:
                    return
        image_urls = self.extract_incoming_image_urls(event)
        if image_urls:
            yield event.plain_result(f"收到 {len(image_urls)} 张图片！{self.bot_name}正在结合提示词努力作画中… 🎨")
            await self._generate_with_reference(event, prompt, image_urls, gen_count, "图生图")
        else:
            yield event.plain_result(f"收到！{self.bot_name}正在生成 {gen_count} 张图，请耐心等待哦… 💭✨")
            await self._generate_text_to_image(event, prompt, gen_count)

    def is_simple_prefixed_command(self, msg: str, *commands: str) -> bool:
        return self.strip_command_body(msg, *commands) == ""

    @filter.regex(r"^.{0,10}(开启|关闭)bnn存图$")
    async def toggle_bnn_save(self, event: AstrMessageEvent):
        msg = self.text(event)
        if not self.is_simple_prefixed_command(msg, "开启bnn存图", "关闭bnn存图"):
            return
        if not self.is_admin(event):
            yield event.plain_result("哼唧，只有主人才能切换存图哦~ 🙅‍♀️")
            return
        enabled = "开启bnn存图" in msg
        await self.set_save_img_enabled(enabled)
        if enabled:
            self.image_dir.mkdir(parents=True, exist_ok=True)
            yield event.plain_result("✅ 已开启bnn存图！生成的图片会保存到本地 data/plugins/astrbot_plugin_painting/generated_images/ 目录 📁")
        else:
            yield event.plain_result("✅ 已关闭bnn存图！后续生成的图片不再长期保存到本地 🚫")

    @filter.regex(r"^.{0,10}排行bnn$")
    async def weekly_ranking(self, event: AstrMessageEvent):
        if not self.is_simple_prefixed_command(self.text(event), "排行bnn"):
            return
        stats = await self.daily_stats()
        daily = stats.get("daily", {}) if isinstance(stats.get("daily"), dict) else {}
        today = date.today()
        monday = today.fromordinal(today.toordinal() - today.weekday())
        week_labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        week_data = []
        total_week = 0
        max_day = (0, "")
        for i in range(7):
            d = monday.fromordinal(monday.toordinal() + i)
            if d > today:
                break
            key = d.isoformat()
            count = int(daily.get(key, 0))
            week_data.append((key, week_labels[i], count))
            total_week += count
            if count > max_day[0]:
                max_day = (count, f"{week_labels[i]} ({key})")
        if not week_data or total_week == 0:
            yield event.plain_result(f"📊 本周还没有任何作画记录哦~ 快来让{self.bot_name}施展魔法吧！✨")
            return
        max_count = max(max_day[0], 1)
        bar_max_len = 12
        lines = [f"📊 {self.bot_name}本周作画排行榜"]
        chart = []
        for key, label, count in week_data:
            bar_len = round((count / max_count) * bar_max_len) if max_count else 0
            bar = "█" * bar_len + "░" * (bar_max_len - bar_len)
            suffix = " 👑" if count == max_day[0] and count > 0 else (" 🔵" if key == today.isoformat() else "")
            chart.append(f"📅 {key} ({label})：{bar} {count}张{suffix}")
        avg = total_week / len(week_data)
        lines.append("\n".join(chart))
        lines.append(f"━━━━━━━━\n🏆 本周总计：{total_week} 张\n📈 日均：{avg:.1f} 张\n🔥 最高日：{max_day[1]} {max_day[0]} 张")
        yield event.plain_result("\n\n".join(lines))

    @filter.regex(r"^.{0,10}(查询额度|查余额|查询api|查api)$")
    async def query_api(self, event: AstrMessageEvent):
        if not self.is_simple_prefixed_command(self.text(event), "查询额度", "查余额", "查询api", "查api"):
            return
        if not self.is_admin(event):
            yield event.plain_result(f"哼唧，这是主人的专属面板，{self.bot_name}不能随便给你看哦~ 🙅‍♀️")
            return
        api_err = self.check_api_key()
        if api_err:
            yield event.plain_result(api_err)
            return
        base = str(self.cfg("balance_base_url", "")).rstrip("/")
        if not base:
            yield event.plain_result("⚠️ 尚未配置 balance_base_url，无法查询余额。")
            return
        yield event.plain_result("正在抓取接口数据，请稍候...")
        try:
            sub = await self.request_json("GET", f"{base}/v1/dashboard/billing/subscription")
            now = date.today()
            start = date.fromordinal(now.toordinal() - 90).isoformat()
            usage = await self.request_json("GET", f"{base}/v1/dashboard/billing/usage?start_date={start}&end_date={now.isoformat()}")
            hard_limit = float(sub.get("hard_limit_usd") or 0)
            used = float(usage.get("total_usage") or 0) / 100
            remain = max(0, hard_limit - used)
            yield event.plain_result(f"📊 中转站 API 状态报告\n=======================\n💰 总限额：${hard_limit:.2f}\n🔥 已消耗：${used:.2f}\n🟢 剩余可用：${remain:.2f}")
        except Exception as exc:
            yield event.plain_result(f"❌ 查询失败：{exc}")

    @filter.regex(r"^.+$", priority=-1)
    async def dynamic_preset_handler(self, event: AstrMessageEvent, *args, **kwargs):
        msg = self.text(event)
        if not msg:
            return
        after_prefix = self.strip_painting_prefix(msg)
        if after_prefix is None:
            return
        # 跳过已被其他 command 处理的指令
        skip_prefixes = ("bnn", "绘图", "更新焚决", "更新焚诀", "查询额度", "查余额", "查询api", "查api", "开启bnn存图", "关闭bnn存图", "排行bnn")
        for sp in skip_prefixes:
            if after_prefix.startswith(sp):
                return
        if not self.preset_reg:
            return
        # 预设 regex 匹配去掉前缀后的内容
        match = self.preset_reg.match(after_prefix)
        if not match:
            return
        api_err = self.check_api_key()
        if api_err:
            yield event.plain_result(api_err)
            return
        if not await self.ensure_enough_count(event, 1):
            return
        keyword = match.group(1)
        preset = next((p for p in self.presets if keyword in (p.get("keywords", []) or [])), None)
        if not preset:
            return
        prompt = str(preset.get("prompt", "")).strip()
        preset_name = str((preset.get("keywords", []) or [keyword])[0])
        image_urls = self.extract_incoming_image_urls(event)
        if preset.get("needImage") and not image_urls:
            yield event.plain_result("呀，这个魔法需要你发送一张参考图片给我哦~ 🖼️")
            return
        yield event.plain_result(f"🪄 {self.bot_name}收到 [{preset_name}] 指令啦，正在为你施展魔法，请稍等哦… 🎨")
        await self._generate_preset(event, prompt, image_urls[:1], preset_name)

    # =========================
    # Generation implementations
    # =========================

    async def _generate_text_to_image(self, event: AstrMessageEvent, prompt: str, count: int) -> None:
        start = time.time()
        raw_url = str(self.cfg("image_api_url", "")).strip()
        model = str(self.cfg("image_model_name", "gpt-image-2")).strip()
        if not raw_url:
            await self.send_text(event, "⚠️ 尚未配置 image_api_url。")
            return
        try:
            url = self.normalize_api_url(raw_url, "/v1/images/generations", str(self.cfg("api_url", "")))
        except Exception as exc:
            await self.send_text(event, f"⚠️ image_api_url 配置错误：{exc}")
            return
        success: List[Path] = []
        errors: List[str] = []
        # 部分中转的 n>1 不稳定，串行 n=1 更稳。
        for i in range(count):
            try:
                data = await self.request_json("POST", url, payload={"model": model, "prompt": prompt, "n": 1, "size": "1024x1024"})
                imgs = self.extract_images_from_response(data)
                if not imgs:
                    raise RuntimeError("响应中未找到图片数据")
                success.append(await self.save_generated_image(imgs[0], "bnn"))
            except Exception as exc:
                errors.append(f"第 {i + 1} 张失败：{exc}")
        if not success:
            await self.send_text(event, f"呜呜呜...{count} 张图全部生成失败了 ({time.time() - start:.2f}s) 🥺\n" + "\n".join(errors[:3]))
            return
        count_info = await self.consume_count_and_summary(event, len(success))
        text = f"\n✨ 铛铛铛！{len(success)}/{count} 张画好啦，总耗时 {time.time() - start:.2f}s ｜类型：文生图{count_info}"
        if errors:
            text += "\n⚠️ " + "；".join(errors[:2])
        await self.send_images(event, success, text)

    async def _generate_with_reference(self, event: AstrMessageEvent, prompt: str, image_urls: List[str], count: int, kind: str) -> None:
        start = time.time()
        raw_api_url = str(self.cfg("api_url", "")).strip()
        model = str(self.cfg("model_name", "gpt-5.5")).strip()
        if not raw_api_url:
            await self.send_text(event, "⚠️ 尚未配置 api_url。")
            return
        try:
            api_url = self.normalize_api_url(raw_api_url, "/v1/chat/completions")
        except Exception as exc:
            await self.send_text(event, f"⚠️ api_url 配置错误：{exc}")
            return
        responses_url = api_url.replace("/v1/chat/completions", "/v1/responses")
        image_content, failed = await self.image_urls_as_content(image_urls, 5)
        if image_urls and not image_content:
            await self.send_text(event, "呜呜，获取你发的图片失败了（可能图片已过期），请重新发送图片试试哦~ 🥺")
            return
        if failed:
            await self.send_text(event, f"⚠️ 有 {failed} 张图片获取失败，先用剩余图片继续画哦~")

        input_content: List[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for item in image_content:
            input_content.append({"type": "input_image", "image_url": item["image_url"]["url"]})
        payload = {"model": model, "input": [{"role": "user", "content": input_content}], "tools": [{"type": "image_generation"}]}

        success: List[Path] = []
        errors: List[str] = []
        for i in range(count):
            try:
                data = await self.request_json("POST", responses_url, payload=payload)
                imgs = self.extract_images_from_response(data)
                if not imgs:
                    raise RuntimeError("响应中未找到图片数据")
                success.append(await self.save_generated_image(imgs[0], "ref"))
            except Exception as exc:
                errors.append(f"第 {i + 1} 张失败：{exc}")
        if not success:
            await self.send_text(event, f"呜呜呜...{count} 张图全部生成失败了 ({time.time() - start:.2f}s) 🥺\n" + "\n".join(errors[:3]))
            return
        count_info = await self.consume_count_and_summary(event, len(success))
        text = f"\n✨ 铛铛铛！{len(success)}/{count} 张画好啦，总耗时 {time.time() - start:.2f}s ｜类型：{kind}{count_info}"
        if errors:
            text += "\n⚠️ " + "；".join(errors[:2])
        await self.send_images(event, success, text)

    async def _generate_preset(self, event: AstrMessageEvent, prompt: str, image_urls: List[str], preset_name: str) -> None:
        start = time.time()
        raw_api_url = str(self.cfg("api_url", "")).strip()
        model = str(self.cfg("model_name", "gpt-5.5")).strip()
        try:
            api_url = self.normalize_api_url(raw_api_url, "/v1/chat/completions")
        except Exception as exc:
            await self.send_text(event, f"⚠️ api_url 配置错误：{exc}")
            return
        image_content, failed = await self.image_urls_as_content(image_urls, 1)
        if failed and image_urls:
            await self.send_text(event, "呜呜，获取你发的图片失败了（可能图片已过期），请重新发送图片试试哦~ 🥺")
            return
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend(image_content)
        payload = {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": 1000, "stream": False}
        try:
            data = await self.request_json("POST", api_url, payload=payload)
            imgs = self.extract_images_from_response(data)
            if not imgs:
                raise RuntimeError("响应中未找到图片数据")
            path = await self.save_generated_image(imgs[0], "preset")
        except Exception as exc:
            await self.send_text(event, f"呜呜呜...画画失败了 ({time.time() - start:.2f}s)\n💣 报错啦: {exc} 🥺")
            return
        count_info = await self.consume_count_and_summary(event, 1)
        await self.send_images(event, [path], f"\n✨ 铛铛铛！画好啦，耗时 {time.time() - start:.2f}s ｜类型：{preset_name}{count_info}")

    async def terminate(self):
        pass
