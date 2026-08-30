"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(require.resolve("./app.js"), "utf8");
const html = fs.readFileSync(require.resolve("./index.html"), "utf8");
const dockerfile = fs.readFileSync(require.resolve("./Dockerfile"), "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));

function functionSource(name) {
  const marker = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`);
  const match = marker.exec(source);
  assert.ok(match, `${name} must remain testable`);
  const params = source.indexOf("(", match.index);
  let paramDepth = 0;
  let brace = -1;
  for (let index = params; index < source.length; index += 1) {
    if (source[index] === "(") paramDepth += 1;
    if (source[index] === ")") {
      paramDepth -= 1;
      if (paramDepth === 0) {
        brace = source.indexOf("{", index + 1);
        break;
      }
    }
  }
  assert.notEqual(brace, -1, `${name} body must remain testable`);
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

assert.doesNotMatch(html, /training-artifacts\.(?:js|css)/, "RP Light GUI must not load training resources");
assert.doesNotMatch(dockerfile, /training-artifacts\.(?:js|css)/, "RP Light GUI image must not publish training resources");
assert.doesNotMatch(html, /datasetTurnInteractionEvidence|Действия в учебном сайте/, "RP dataset review must not expose training evidence");

function sourceBetween(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} must remain testable`);
  return source.slice(start, end);
}

const selectedRadioSource = source.match(/function selectedRadioValue[\s\S]*?\n}/)?.[0];
assert.ok(selectedRadioSource, "selectedRadioValue must remain testable");

let selected = null;
const radioContext = {
  document: {
    querySelector() {
      return selected;
    },
  },
};
vm.runInNewContext(selectedRadioSource, radioContext);

assert.equal(radioContext.selectedRadioValue("scenarioType", ""), "");
assert.equal(radioContext.selectedRadioValue("worldSource"), "ready");
selected = { value: "training" };
assert.equal(radioContext.selectedRadioValue("scenarioType", ""), "training");
assert.equal((source.match(/selectedRadioValue\("scenarioType", ""\)/g) || []).length, 7);

const renderDialogOptionsSource = sourceBetween("function renderDialogOptions", "function renderDialogModelOptions");
assert.match(
  renderDialogOptionsSource,
  /input\.checked = input\.value === "rp"/,
  "opening the RP-only dialog must keep the sole RP scenario selected",
);

const worldPosition = html.indexOf('id="worldSelect"');
const presetPosition = html.indexOf('id="partyPresetSelect"');
const openingPosition = html.indexOf('id="partyOpeningSelect"');
const characterPosition = html.indexOf("<legend>Персонаж</legend>");
assert.ok(worldPosition >= 0 && worldPosition < presetPosition, "preset selector must follow the world selector");
assert.ok(presetPosition < openingPosition, "opening selector must follow the preset selector");
assert.ok(openingPosition < characterPosition, "revision-11 choices must precede character creation");
function element(value = "") {
  return {
    value,
    innerHTML: "",
    disabled: false,
    required: false,
    dataset: {},
    classList: { toggle() {} },
    toggleAttribute() {},
  };
}

const cleanWorld = {
  id: "day-watch-moscow-v2",
  title: "Дозоры: Москва",
  scenario_presets: [
    {
      id: "night-trainee",
      title: "Стажёр Ночного Дозора",
      player_role: "Стажёр",
      style: "Книжный",
      format: "plain_scene_text",
      difficulty: null,
      detail_level: "default",
    },
  ],
  free_scenario_seed: {
    source: "free",
    scenario_id: "free-scenario",
    title: "Свободный сценарий",
    player_role: "Независимый Иной",
    style: "Книжный",
    format: "plain_scene_text",
    difficulty: null,
    detail_level: "default",
    narrator_system: "Системный контракт мира",
    narrator_note: "Авторская заметка",
    opening: "Ночной вызов на Арбат.",
    initial_state: {
      characters: { player: { id: "player" } },
      relationships: {},
    },
    active_character_ids: ["player"],
    local_overrides: {},
  },
};

const trainingWorld = {
  id: "awareness-training",
  title: "Awareness",
  manifest: {
    player_role: "Сотрудник",
    scenario_types: { supported: ["training"] },
  },
  openings: [
    { id: "training-start", title: "Старт", player_role: "Сотрудник" },
  ],
  openings_default: "training-start",
};

assert.ok(
  html.indexOf('id="cleanWorldSelect"') < html.indexOf('id="worldSelect"'),
  "clean RP wizard must precede the retained Training wizard",
);
for (const id of [
  "cleanPartyWizardFields",
  "cleanWorldSelect",
  "cleanScenarioPresetSelect",
  "cleanScenarioFreeFields",
  "cleanScenarioTitleInput",
  "cleanPlayerRoleInput",
  "cleanOpeningInput",
]) {
  assert.match(html, new RegExp(`id="${id}"`), `${id} must be present in the party dialog`);
}
assert.match(html, /data-party-flow="legacy"/, "Training controls must remain available");

const payloadContext = {};
vm.runInNewContext(functionSource("cleanPartyCreatePayload"), payloadContext);

const presetScenario = { source: "preset", preset_id: "night-trainee" };
assert.deepEqual(
  plain(payloadContext.cleanPartyCreatePayload({
    title: "Preset party",
    worldId: cleanWorld.id,
    scenario: presetScenario,
    modelProfileId: "model-1",
  })),
  {
    title: "Preset party",
    world_id: cleanWorld.id,
    scenario: presetScenario,
    model_profile_id: "model-1",
  },
);

const freeScenario = {
  ...cleanWorld.free_scenario_seed,
  scenario_id: "free-arbat",
  title: "Свободный Арбат",
  player_role: "Независимый Светлый",
  opening: "Я выхожу из метро.",
};
assert.deepEqual(
  plain(payloadContext.cleanPartyCreatePayload({
    title: "Free party",
    worldId: cleanWorld.id,
    scenario: freeScenario,
    modelProfileId: "model-1",
  })),
  {
    title: "Free party",
    world_id: cleanWorld.id,
    scenario: freeScenario,
    model_profile_id: "model-1",
  },
);

async function runCreateParty({ scenarioType, scenario }) {
  const calls = [];
  const selected = [];
  const toasts = [];
  const isClean = scenarioType === "rp";
  const context = {
    appState: {
      cleanWorldDetail: cleanWorld,
      worldpacks: [cleanWorld],
      pendingStoryMemoryCorrections: [],
    },
    els: {
      modelSelect: element("model-1"),
      partyTitleInput: element(isClean ? `${scenario.source} party` : "Training party"),
      cleanWorldSelect: element(cleanWorld.id),
      characterDescriptionInput: element("Сотрудник"),
      characterNameInput: element("Игрок"),
    },
    selectedRadioValue(name, fallback = "ready") {
      if (name === "scenarioType") return scenarioType;
      if (name === "characterSource") return "ready";
      return fallback;
    },
    isCleanRpCreation: () => isClean,
    loadCleanWorldDetail: async () => cleanWorld,
    cleanScenarioPayload: () => plain(scenario),
    cleanPartyCreatePayload: payloadContext.cleanPartyCreatePayload,
    resolveWorldpack: async () => trainingWorld,
    selectedPartyWorldChoices: () => ({
      preset: null,
      opening: trainingWorld.openings[0],
    }),
    setBusy() {},
    renderCleanRuntimeControls() {},
    closePartyDialog() {},
    boot: async () => {},
    selectParty: async (partyId) => selected.push(partyId),
    autoStartParty: async () => {},
    showToast(message) {
      toasts.push(String(message));
    },
    async apiPost(path, payload) {
      calls.push({ path, payload: plain(payload) });
      if (path === "/api/player-characters/draft") {
        return {
          draft: {
            worldpack_id: trainingWorld.id,
            opening_id: trainingWorld.openings_default,
            name: "Игрок",
            description: "Сотрудник",
            profile: {},
          },
        };
      }
      if (path === "/api/player-characters") {
        return { player_character: { id: "training-character" } };
      }
      if (path === "/api/parties") return { party: { id: `${scenarioType}-party` } };
      throw new Error(`unexpected POST ${path}`);
    },
  };
  vm.runInNewContext(functionSource("createParty"), context);
  await context.createParty({ preventDefault() {} });
  return { calls, selected, toasts };
}

(async () => {
  for (const scenario of [presetScenario, freeScenario]) {
    const result = await runCreateParty({ scenarioType: "rp", scenario });
    assert.deepEqual(
      result.calls.map((call) => call.path),
      ["/api/parties"],
      `${scenario.source} creation must use one clean Party request`,
    );
    assert.equal(result.calls[0].payload.world_id, cleanWorld.id);
    assert.deepEqual(result.calls[0].payload.scenario, scenario);
    assert.equal(
      result.calls.some((call) => call.path.startsWith("/api/player-characters")),
      false,
    );
    assert.equal(
      result.calls.some((call) => call.path === "/api/worldpacks/prompt"),
      false,
    );
  }

  const training = await runCreateParty({ scenarioType: "training", scenario: presetScenario });
  assert.deepEqual(training.calls.map((call) => call.path), [
    "/api/player-characters/draft",
    "/api/player-characters",
    "/api/parties",
  ]);
  assert.deepEqual(training.calls[2].payload, {
    title: "Training party",
    scenario_type: "training",
    worldpack_id: trainingWorld.id,
    player_character_id: "training-character",
    model_profile_id: "model-1",
    opening_id: "training-start",
  });

  console.log("light gui party dialog clean RP and retained Training: ok");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
