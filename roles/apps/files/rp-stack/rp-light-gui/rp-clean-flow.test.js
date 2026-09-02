"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");

const source = fs.readFileSync(require.resolve("./app.js"), "utf8");
const html = fs.readFileSync(require.resolve("./index.html"), "utf8");

for (const endpoint of [
  "/api/worldpacks",
  "/api/model-profiles",
  "/api/parties",
  "/start",
  "/messages",
  "/lore-cards/draft",
  "/player-corrections/draft",
  "/administrator/proposals/",
  "/byok",
]) {
  assert.match(source, new RegExp(endpoint.replaceAll("/", "\\/")));
}

assert.match(source, /idempotency_key:\s*identity\.requestId/);
assert.match(source, /expected_version:\s*identity\.expectedVersion/);
assert.match(source, /state\.party\.current_version = result\.state_version/);
assert.match(source, /source_turn_ids:\s*\[turn\.id\]/);
assert.match(source, /always_on:\s*false/);
assert.match(source, /provider:\s*"openrouter"/);
assert.doesNotMatch(source, /RP_REBUILD_ENABLED|data-legacy|openrouter\/auto|nvidia/i);
assert.doesNotMatch(html, /data-legacy|autotest|checkpoint|D20|world\/instruct/i);

for (const id of [
  "partyList",
  "chatLog",
  "messageForm",
  "roleCards",
  "loreDraftForm",
  "correctionForm",
  "administratorList",
  "byokForm",
  "partyDialog",
]) {
  assert.match(html, new RegExp(`id=["']${id}["']`));
}

console.log("light gui Decision 043 clean-only contract: ok");
