# Repository work standard

This short file is the checked workstation contract. Read it with `AGENTS.md` at
the start of repository work; use the Wiki for architecture and historical
detail.

## Local workspace and Codex

- Canonical checkout: `C:\Users\Адександр\Documents\Tavern\ubuntu_ansible_palybooks`.
- Work on a `codex/` branch or an isolated worktree. The three unregistered
  pre-rename worktrees (`deepseek-flash-latency`, `rp-validation-fix`, and
  `training-capabilities`) were verified against Git objects and removed on
  2026-08-05; recreate future worktrees from current repository refs.
- Plugin source: `plugins/rp-stack-devkit/`; the repository marketplace is
  `.agents/plugins/marketplace.json`.
- The plugin declares `rp-stack-ops` through
  `plugins/rp-stack-devkit/.mcp.json`. Project `.codex/config.toml` only enables
  hooks; `.codex/hooks.json` declares the project policy hook.
- `gh` 2.97.0 is available as `abykovwww-byte` with `gist`, `read:org`, `repo`,
  and `workflow` scopes when run with normal network access.

## SSH, sudo, and production reads

- Use `ssh -i ~/.ssh/id_ed25519_codex_abykovserv abykov@192.168.1.88 ...`.
  `~/.ssh/config` also maps `abykovserv` and `192.168.1.88` to that identity,
  while `ssh-agent` is stopped and disabled. Keep `-i` explicit in repository
  instructions and automation.
- `sudo` requires interactive user entry; `sudo -n` fails. Codex stops at
  `pushed` and asks the user to run `sudo systemctl start
  ansible-local-apply.service` interactively. Never request, log, or store the
  password.
- For a Python production probe, send a local script on stdin instead of nesting
  PowerShell, SSH, and Python quoting:

  ```text
  ssh -i ~/.ssh/id_ed25519_codex_abykovserv abykov@192.168.1.88 \
    'cd /srv/apps/rp-stack && docker compose exec -T rp-gateway python -' < script.py
  ```

- Open Gateway SQLite only read-only with
  `file:/data/rp_gateway.db?mode=ro` and `uri=True`. Never print secrets or raw
  secret-bearing rows.

## Toolchain and gates

- Bundled runtimes are Python 3.12.13 and Node.js 24.14.0 under
  `%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\`.
  The PATH `python` launcher is not usable on this workstation (observed exit
  9009); `scripts/ci.ps1` resolves the bundled tools through `Resolve-Tool`.
- CI has five jobs: `ansible-syntax`, `browser-clients`, `gateway`,
  `repository-contracts`, and `rp-contracts`. Local parity is
  `powershell.exe -File scripts/ci.ps1`. On its first Gateway test run, the
  script restores the already declared `rp-gateway/requirements.txt` versions
  into the ignored project-local `.test-deps/` directory.
- The existing parent-workspace Graphify graph uses
  `..\graphify-out\.venv\Scripts\python.exe` and the pre-#1504 node-ID scheme.
  A future rebuild should run from this repository root and use the tracked
  `.graphifyignore`; generated graph data remains outside Git.

## Skill source of truth

- `codex-skills/` is canonical. `%USERPROFILE%\.codex\skills\` contains
  generated installed copies, never independently edited sources.
- The retired duplicate under the parent `Tavern/codex-skills/` was consolidated
  into the canonical `rp-world-pack-builder` and removed on 2026-08-05.
- Check drift with `powershell.exe -File scripts/sync-codex-skills.ps1 -Mode
  Check`; install exact repository copies with `-Mode Install`, then start a new
  Codex task so the refreshed skills are loaded.
- `rp-stack-devkit` is installed from the repository plugin marketplace. After
  plugin source changes, refresh its manifest cachebuster/reinstall and start a
  new task.

Codex receives repository `AGENTS.md` automatically and applies closer nested
`AGENTS.md` files under their subtrees. Project `.codex/` settings and installed
plugin/skill registrations are loaded by the Codex host, but an arbitrary file
such as this one is not automatic context; the mandatory `AGENTS.md` start rule
is therefore the bridge that makes these facts available without a manual
reminder.
