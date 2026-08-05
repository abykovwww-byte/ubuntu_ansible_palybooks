# RP Stack Devkit plugin

Repository-scoped Codex plugin for safe RP Stack development and operations.

It packages:

- an orchestration skill for the repository's established IaC, Wiki, WorldPack, and Graphify workflows;
- a read-only `rp-stack-ops` MCP server and CLI;
- a bounded three-layer eval runner;
- policy hooks that block common destructive or secret-bearing tool calls;
- a browser smoke checklist for post-deployment verification.

The plugin does not contain credentials and has no live mutation or deployment tool.

## Install from this repository

```powershell
codex plugin marketplace add C:\Users\<user>\Documents\Tavern\ubuntu_ansible_palybooks
codex plugin add rp-stack-devkit@tavern-rp-stack
```

Start a new Codex task after installation so the new skill, hooks, and MCP server are loaded.
