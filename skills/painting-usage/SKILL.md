---
name: painting-usage
description: Use when answering user questions about the AstrBot Painting plugin: commands, quotas, presets, #bnn image generation, local saving, weekly ranking, and API balance lookup.
---

# Painting Plugin Usage

This skill documents the user-facing behavior of `astrbot_plugin_painting`.

## When to use

Use this skill when the user asks how to use Painting, why a drawing command did not trigger, how quotas work, or how to configure prefixes/API settings.

## Command summary

- `<prefix>bnn <prompt>`: text-to-image.
- `<prefix>bnn3 <prompt>`: generate multiple images; capped by `bnn_max_count`.
- `<prefix><preset>`: run a remote/bundled preset keyword.
- `<prefix>绘图帮助`: show help.
- `<prefix>绘图查询次数`: show group/user quota.
- `<prefix>绘图增加次数 <n> [target]`: admin only.
- `<prefix>绘图删除次数 [target]`: admin only.
- `<prefix>绘图删除所有次数`: admin only.
- `<prefix>开启bnn存图` / `<prefix>关闭bnn存图`: admin only.
- `<prefix>排行bnn`: weekly drawing ranking.
- `<prefix>更新焚决` / `<prefix>绘图更新预设`: admin only, refresh presets.
- `<prefix>查询额度` / `<prefix>查余额`: admin only, requires `balance_base_url`.

`command_prefix` is independent from the global AstrBot prefix. If it is set to `xy`, users send `xybnn cat` or `xyQ版`, not `#xybnn` unless compatibility candidates are desired.

## Common troubleshooting

1. No response:
   - Check `command_prefix` first.
   - Check whether the command is an admin-only command.
   - Check whether `api_key`, `api_url`, and `image_api_url` are configured.
2. Reference image failed:
   - Platform temporary image URLs may expire; resend the image.
   - The plugin downloads incoming images and converts them to base64 before API calls.
3. Quota not enough:
   - Non-admin users consume group/user quota after successful image generation.
   - Admins can add/delete quota with the management commands.
4. Preset missing:
   - Run `<prefix>更新焚决`; if remote sources fail, bundled presets are used.

## Data locations

Runtime data is stored under AstrBot data path: `plugin_data/astrbot_plugin_painting/`.

Generated images:
- temporary send-only files: `temp_images/`
- saved images when enabled: `generated_images/`

## Safety

Do not expose API keys in replies or logs. Ask admins to configure secrets in the AstrBot dashboard.
