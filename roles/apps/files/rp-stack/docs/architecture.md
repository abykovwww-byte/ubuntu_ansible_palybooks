# RP Stack Architecture

## Runtime flow

```text
Browser
  -> rp-light-gui (static UI and /api reverse proxy)
  -> rp-gateway (FastAPI authority)
  -> provider APIs or rp-local-llm
```

Light GUI depends only on RP Gateway. RP Gateway owns the active binding between
an RP world pack, player character, model profile, canonical state, turn
history, and memory chapters. All persistent RP party data is scoped by party
and stored in the RP SQLite database plus isolated state files.

Deterministic training is a separate application boundary:

```text
Browser
  -> Showroom on http://192.168.1.88:8011
  -> standalone Training Gateway
  -> standalone Training SQLite/WorldPacks/provider APIs
```

The standalone Gateway owns training programs, assessment, fallbacks,
interactive artifacts, immutable Showroom run flags, typed evidence, and
canonical scoring. It shares neither routes nor database state with RP Gateway.
The zero-window cutover removes the legacy training modules, Awareness
WorldPacks, and static Showroom from this source tree and RP Compose. Old
training and Showroom rows remain in RP SQLite for read-only preservation, but
the RP process neither publishes nor writes them.

The canonical term **service model / служебная модель** means the one global
LLM selected by an administrator for long-term memory, world-state changes,
and character generation across all current and future parties. It is not a
narrator model. BYOK credentials are user-owned and party-scoped; service-model
requests use only stack-managed credentials.

World-pack prompts and `world-info/index.md` provide immutable context.
`state-seed.json` initializes each party. Canonical state and
`AUTHORITATIVE_OUTCOME` override prose memory.

See [Decision 018](decisions/018-separate-training-and-rp-gateways.md) for the
application boundary. Historical training contract details remain in
[Decision 017](decisions/017-worldpack-owned-training-runtime.md) and are active
only in `tavern-awareness-showroom`.

## Deployment

Ansible copies the committed source tree to `/srv/apps/rp-stack`, renders the
Compose and environment files, and runs Compose with orphan cleanup. Mutable
Gateway data remains under `/srv/app-data/rp-stack/gateway`.
