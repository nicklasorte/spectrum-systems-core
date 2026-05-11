# Claude Code Agent Settings

`permissions.defaultMode` is set to `bypassPermissions` in `.claude/settings.json`.
This eliminates permission prompts for all bash commands in Claude Code sessions
launched from this repo.

> Note: the previous `dangerouslySkipPermissions: true` setting was not a valid
> Claude Code settings field and was silently ignored. The valid schema
> equivalent is `permissions.defaultMode: "bypassPermissions"`.

## artifact_kind vs artifact_type policy

Existing schemas in this repo use `artifact_kind` for historical reasons.

- All NEW schemas: use `artifact_type`
- All EXISTING schemas: leave as-is
- Never fail or stop because an existing schema uses `artifact_kind`
- Never add `artifact_kind` to any new schema
