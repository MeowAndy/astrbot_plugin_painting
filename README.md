# AstrBot Painting 画图插件 🎨

从 Yunzai `Painting.js` 移植的 AstrBot 插件，支持预设画图、`#bnn` 自定义画图、次数管理和 API 余额查询。

## 功能

- 🪄 云端预设/焚决：`#更新焚决` 自动拉取预设关键词
- 🎨 `#bnn <提示词>` 文生图
- 🖼️ `#bnn <提示词>` + 图片：图生图/参考图创作
- 🔢 `#bnn3 <提示词>` 多图生成，最大数量可在控制台配置
- 📊 群次数 + 个人次数管理
- 💾 `#开启bnn存图` / `#关闭bnn存图` 本地存图开关
- 🏆 `#排行bnn` 本周作画统计排行
- 💰 API 额度查询（需要配置 `balance_base_url`）
- ⚙️ 所有关键参数都支持 AstrBot 网页控制台配置：API Key、模型、超时时间等

## 安装

将仓库放到 AstrBot 插件目录，例如：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/MeowAndy/astrbot_plugin_painting.git
```

安装依赖并重启 AstrBot：

```bash
pip install -r astrbot_plugin_painting/requirements.txt
```

## 控制台配置

进入 AstrBot 网页控制台 → 插件管理 → Painting 画图，配置：

| 配置项 | 说明 |
|---|---|
| `api_url` | Chat Completions API 地址，用于预设画图/参考图画图 |
| `api_key` | API Key |
| `model_name` | Chat/多模态模型名，默认 `gpt-5.5` |
| `image_api_url` | Images Generations API 地址，用于纯文生图 |
| `image_model_name` | 图片生成模型，默认 `gpt-image-2` |
| `api_timeout` | API 超时时间（秒），默认 240 |
| `bnn_max_count` | `#bnn` 单次最多生成张数 |
| `initial_user_count` | 新群/新用户初始次数 |
| `preset_json_url` | 云端预设 JSON 地址 |
| `bot_name` | 回复里显示的角色名 |
| `command_prefix` | 画图指令独立前缀，如 `#` / `!` / `xy` / `菲比`；填 `xy` 时直接发 `xybnn` 或 `xy预设`，不需要再加 `#` |
| `balance_base_url` | 余额查询 API 基础地址，可留空 |

## 指令

| 指令 | 权限 | 说明 |
|---|---|---|
| `#<预设关键词>` | 所有人 | 使用云端预设画图 |
| `#bnn <提示词>` | 所有人 | 自定义文生图/图生图 |
| `#bnn3 <提示词>` | 所有人 | 一次生成 3 张 |
| `#绘图帮助` | 所有人 | 查看帮助 |
| `#绘图查询次数` | 所有人 | 查询本群和个人次数 |
| `#绘图增加次数 <数量> [uQQ号/群号]` | 管理员 | 增加次数 |
| `#绘图查询所有次数` | 管理员 | 查看次数账本 |
| `#绘图删除次数 [uQQ号/群号]` | 管理员 | 删除指定次数记录 |
| `#绘图删除所有次数` | 管理员 | 清空次数记录 |
| `#开启bnn存图` | 管理员 | 开启本地存图，保存到 `data/plugins/astrbot_plugin_painting/generated_images/` |
| `#关闭bnn存图` | 管理员 | 关闭本地存图 |
| `#排行bnn` | 所有人 | 查看本周每日作画统计排行 |
| `#更新焚决` / `#绘图更新预设` | 管理员 | 更新云端预设 |
| `#查询额度` / `#查余额` | 管理员 | 查询 API 额度 |

## 说明

- 插件不会硬编码 API Key，请在网页控制台填写。
- 图片会先下载并转为 base64 再发给 API，避免 QQ/Telegram 临时图片 URL 外部不可访问。
- 计数和预设使用 AstrBot 插件 KV 存储。

## License

MIT
