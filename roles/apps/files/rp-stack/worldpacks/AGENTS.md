# WorldPack instructions

- WorldPack files define immutable authored content; Gateway remains the runtime authority.
- Route active packs in this directory through `rp-world-pack-builder`; they must support only `rp`.
- Create or change scored learning scenarios through `training-world-pack-builder` in `tavern-awareness-showroom/worldpacks/`. Zero-window O2 removed the old training directories from this source tree; they are not rollback material. Legacy RP SQLite/state/backups remain preserved outside source cleanup, and failures are fixed forward through application/IaC PRs.
- Validate the production World/Scenario loader boundary and referenced authored files before publishing. Validate training schedules, artifacts, scoring, and debrief contracts in the standalone repository.
- Existing runs must remain pinned to their materialized versions and hashes; do not retrofit historical turns from edited files.
