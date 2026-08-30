# WorldPack instructions

- WorldPack files define immutable authored content; Gateway remains the runtime authority.
- Route active packs in this directory through `rp-world-pack-builder`; they must support only `rp`.
- Create or change scored learning scenarios through `training-world-pack-builder` in `tavern-awareness-showroom/worldpacks/`. Retained training directories here are read-only rollback material until the explicit O2 removal.
- Validate RP state seeds, referenced files, and schemas before publishing. Validate training schedules, artifacts, scoring, and debrief contracts in the standalone repository.
- Existing runs must remain pinned to their materialized revisions; do not retrofit historical turns from edited files.
