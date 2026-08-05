# RP Stack Devkit plugin

Repository-scoped Codex plugin for safe RP Stack development and operations.

It packages:

- an orchestration skill for the repository's established IaC, Wiki, WorldPack, and Graphify workflows;
- a read-only `rp-stack-ops` MCP server and CLI;
- a bounded three-layer eval runner;
- policy hooks that block common destructive or secret-bearing tool calls;
- a browser smoke checklist for post-deployment verification.

The plugin does not contain credentials and has no live mutation or deployment tool.

Remote diagnostics pass `%USERPROFILE%\.ssh\id_ed25519_codex_abykovserv` with
`-i` by default when that file exists. `RP_STACK_OPS_IDENTITY_FILE` can select a
different existing private-key file; the key path remains local and is never
stored in the repository. `RP_STACK_OPS_HOST` and `RP_STACK_OPS_SSH` can
override the default target and SSH executable. See
`docs/repository-work-standard.md` for the checked workstation contract.

## Install from this repository

```powershell
codex plugin marketplace add C:\Users\<user>\Documents\Tavern\ubuntu_ansible_palybooks
codex plugin add rp-stack-devkit@tavern-rp-stack
```

Start a new Codex task after installation so the new skill, hooks, and MCP server are loaded.
