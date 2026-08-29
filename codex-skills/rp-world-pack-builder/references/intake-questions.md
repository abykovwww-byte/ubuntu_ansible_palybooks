# Intake Questions

Ask no more than three questions at a time. Skip answers already present in the
request or committed source.

## Current Executable Boundary

The current Decision 043 loader supports only `day-watch-moscow-v2`. For changes
to that World, ask only what affects the requested source:

1. What reusable World fact changes: canon, rule, faction, place, base NPC,
   relationship ontology, or seed lore?
2. What Scenario dimension changes: player start, style, format, difficulty,
   detail, opening, initial state, active NPCs, or local deviation?
3. Which existing World/Scenario combinations must remain unchanged?

When the user wants speed, make only assumptions that remain within the
existing closed schema and report them explicitly in the task/PR description.
Do not add an undeclared `assumptions` field to `world.json` or a preset.

## Requests For Another World

If the user asks for a different executable World, gather a brief but do not
write a source definition that the current loader will reject. Ask:

1. What is the World name and premise/source: original, historical, or existing
   IP?
2. Which reusable canon, factions, places, NPCs, and relationship model define
   it?
3. Which starting player role and first Scenario are required?

Record the result as a proposal or later-slice requirement. Do not work around
the single-World guard through `manifest.json`.

## Optional Questions

Use only when they materially affect authored source:

- language of play;
- canon-faithful versus canon-divergent treatment;
- hard tone/content boundaries;
- intended narrator style and output format;
- difficulty and detail level;
- active NPC subset;
- whether a change is reusable World canon or a Scenario-local override;
- which of the twelve approved style/start combinations change.

For deterministic scoring, curriculum, typed browser-event assessment, or a
debrief-driven learning scenario, stop and route to
`training-world-pack-builder`.

## Existing IP

- Prefer original campaign situations inside the setting rather than copying
  scenes verbatim.
- Keep entries concise; do not paste long copyrighted passages.
- If exact canon matters and local evidence is insufficient, verify against an
  authoritative source or ask the user for canon notes.
- Preserve player agency; do not force the player to become the original
  protagonist unless requested.

## Runtime Expectation

The current source is offline-only. Do not offer Light GUI creation, server
installation, or runtime visibility as a consequence of authoring it. A later
integration/cutover task must connect the production loader to the product
surface and prove real party play.
