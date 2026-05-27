# AstrBot Skill Upstream Notes

Reference repo reviewed: https://github.com/EterUltimate/AstrBot-Skill

Applied ideas:

- Use AstrBot data path helper and store plugin runtime files under `plugin_data/<plugin_id>`.
- Stop event propagation after broad all-message handlers match a plugin command.
- Provide bundled plugin skill under `skills/` for AstrBot Skill Manager.
- Provide `.astrbot-plugin/i18n/` dashboard metadata/config translations.
