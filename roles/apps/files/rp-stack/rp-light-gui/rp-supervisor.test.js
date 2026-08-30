"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(require.resolve("./app.js"), "utf8");
const html = fs.readFileSync(require.resolve("./index.html"), "utf8");

function functionSource(name) {
  const marker = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`);
  const match = marker.exec(source);
  assert.ok(match, `${name} must remain testable`);
  const brace = source.indexOf("{", match.index);
  let depth = 0;
  let quote = null;
  let escaped = false;
  for (let index = brace; index < source.length; index += 1) {
    const char = source[index];
    if (quote) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === quote) quote = null;
      continue;
    }
    if (char === '"' || char === "'" || char === "`") {
      quote = char;
      continue;
    }
    if (char === "{") depth += 1;
    if (char === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(match.index, index + 1);
    }
  }
  assert.fail(`${name} source is incomplete`);
}

const roleContext = {
  appState: { supervisor: null },
  escapeHtml: (value) => String(value),
};
vm.runInNewContext(functionSource("rpRoleStatusHtml"), roleContext);
vm.runInNewContext(functionSource("rpSupervisorStatusHtml"), roleContext);

const roles = {
  narrator: {
    role: "narrator",
    enabled: true,
    kill_switch: false,
    provider: "openrouter",
    model: "anthropic/claude-sonnet-4",
    status: "succeeded",
    success_count: 4,
    error_count: 1,
    last_error: "previous narrator failure",
    raw_prompt: "must-not-render",
  },
  atomic_service: {
    role: "atomic_service",
    enabled: false,
    kill_switch: true,
    provider: "openrouter",
    model: "google/gemma-3-27b-it",
    status: "pending",
    success_count: 0,
    error_count: 0,
    last_error: null,
    raw_response: "must-not-render",
  },
  administrator: {
    role: "administrator",
    enabled: true,
    kill_switch: false,
    provider: "gemini",
    model: "gemini-2.5-flash",
    status: "failed",
    success_count: 2,
    error_count: 3,
    last_error: "administrator timeout",
    claim_token: "must-not-render",
  },
};

const narrator = roleContext.rpRoleStatusHtml(roles.narrator);
assert.match(narrator, /openrouter/);
assert.match(narrator, /anthropic\/claude-sonnet-4/);
assert.match(narrator, /succeeded/);
assert.match(narrator, /успешно 4/);
assert.match(narrator, /ошибок 1/);
assert.match(narrator, /previous narrator failure/);

const atomic = roleContext.rpRoleStatusHtml(roles.atomic_service);
assert.match(atomic, /google\/gemma-3-27b-it/);
assert.match(atomic, /pending/);
assert.match(atomic, /kill switch включён/);

const administrator = roleContext.rpRoleStatusHtml(roles.administrator);
assert.match(administrator, /gemini-2.5-flash/);
assert.match(administrator, /failed/);
assert.match(administrator, /успешно 2/);
assert.match(administrator, /ошибок 3/);
assert.match(administrator, /administrator timeout/);

const rendered = [narrator, atomic, administrator].join("\n");
assert.doesNotMatch(rendered, /must-not-render/);
assert.doesNotMatch(rendered, /raw_(?:prompt|response)|claim_token/i);

roleContext.appState.supervisor = { roles };
const threeRoleSupervisor = roleContext.rpSupervisorStatusHtml();
assert.match(threeRoleSupervisor, /Нарратор/);
assert.match(threeRoleSupervisor, /Атомарная служебная модель/);
assert.match(threeRoleSupervisor, /Administrator/);
assert.doesNotMatch(threeRoleSupervisor, /must-not-render|raw_(?:prompt|response)|claim_token/i);

roleContext.appState.supervisor = {
  enabled: true,
  mode: "observe",
  story_turn_count: 7,
  first_retrospective_story_turn: 56,
  next_retrospective_story_turn: 56,
  last_evaluation: null,
  service_model: { title: "OpenRouter service model" },
};
const retainedTraining = roleContext.rpSupervisorStatusHtml();
assert.match(retainedTraining, /Надзор · наблюдение/);
assert.match(retainedTraining, /7\/56 канонических ходов/);
assert.match(retainedTraining, /ничего не добавляет в prompt нарратора/);
assert.match(retainedTraining, /OpenRouter service model/);

for (const [id, role] of [
  ["narratorRoleCard", "narrator"],
  ["atomicServiceRoleCard", "atomic_service"],
  ["administratorRoleCard", "administrator"],
]) {
  assert.match(html, new RegExp(`id="${id}"[^>]*data-rp-role="${role}"`));
}
assert.match(source, /\/api\/parties\/\$\{partyId\}\/supervisor/);

console.log("light gui clean RP three-role supervisor: ok");
