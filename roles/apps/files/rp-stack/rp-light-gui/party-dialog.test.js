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

function sourceBetween(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} must remain testable`);
  return source.slice(start, end);
}

assert.doesNotMatch(html, /training-artifacts\.(?:js|css)/, "RP Light GUI must not load training resources");
assert.doesNotMatch(dockerfile, /training-artifacts\.(?:js|css)/, "RP Light GUI image must not publish training resources");
assert.doesNotMatch(html, /datasetTurnInteractionEvidence|Действия в учебном сайте/, "RP dataset review must not expose training evidence");
assert.doesNotMatch(html, /name="scenarioType"/, "RP Light GUI must not expose a removed training/RP switch");
assert.doesNotMatch(source, /trainingWorldpacks|scenario_type=training|TrainingArtifacts/, "RP Light GUI must not retain training branches");
assert.doesNotMatch(source, /^(?:<<<<<<<|=======|>>>>>>>)/m, "app.js must not contain conflict markers");
assert.doesNotMatch(html, /^(?:<<<<<<<|=======|>>>>>>>)/m, "index.html must not contain conflict markers");

const selectedRadioSource = functionSource("selectedRadioValue");
let selected = null;
const radioContext = {
  document: {
    querySelector() {
      return selected;
    },
  },
};
vm.runInNewContext(selectedRadioSource, radioContext);
assert.equal(radioContext.selectedRadioValue("worldSource"), "ready");
selected = { value: "prompt" };
assert.equal(radioContext.selectedRadioValue("worldSource"), "prompt");

function element(value = "") {
  const classes = new Set(["hidden"]);
  const attributes = new Set();
  return {
    value,
    innerHTML: "",
    disabled: false,
    required: false,
    dataset: {},
    classList: {
      toggle(name, force) {
        if (force) classes.add(name);
        else classes.delete(name);
      },
      contains(name) {
        return classes.has(name);
      },
    },
    toggleAttribute(name, force) {
      if (force) attributes.add(name);
      else attributes.delete(name);
    },
    hasAttribute(name) {
      return attributes.has(name);
    },
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

const rev11Pack = {
  id: "rev11-world",
  title: "Revision 11",
  manifest: {
    player_role: "Legacy alias role",
    scenario_types: { supported: ["rp"] },
  },
  presets: [
    { id: "book", title: "Книжный" },
    { id: "action", title: "Действие" },
  ],
  presets_default: "action",
  openings: [
    { id: "night-trainee", title: "Стажёр", player_role: "Стажёр Ночного Дозора" },
    { id: "independent", title: "Независимый", player_role: "Независимый Иной" },
  ],
  openings_default: "independent",
};

const legacyPack = {
  id: "legacy-world",
  title: "Legacy",
  manifest: { player_role: "Legacy player role" },
};

const cleanPosition = html.indexOf('id="cleanPartyWizardFields"');
const worldPosition = html.indexOf('id="worldSelect"');
const presetPosition = html.indexOf('id="partyPresetSelect"');
const openingPosition = html.indexOf('id="partyOpeningSelect"');
const characterPosition = html.indexOf("<legend>Персонаж</legend>");
assert.ok(cleanPosition >= 0 && cleanPosition < worldPosition, "clean RP wizard must precede the legacy RP wizard");
assert.ok(worldPosition < presetPosition, "preset selector must follow the legacy world selector");
assert.ok(presetPosition < openingPosition, "opening selector must follow the preset selector");
assert.ok(openingPosition < characterPosition, "revision-11 choices must precede legacy character creation");
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
assert.match(html, /data-party-flow="legacy"/, "legacy RP controls must remain available when rebuild is disabled");

const modeContext = { appState: { worldpacks: [legacyPack] } };
vm.runInNewContext(
  [functionSource("isCleanWorldpack"), functionSource("hasCleanRpCatalog"), functionSource("isCleanRpCreation")].join("\n"),
  modeContext,
);
assert.equal(modeContext.isCleanRpCreation(), false, "legacy catalog must keep the legacy RP flow");
modeContext.appState.worldpacks = [cleanWorld];
assert.equal(modeContext.isCleanRpCreation(), true, "clean catalog must activate the World/Scenario flow");

let currentPack = rev11Pack;
const choiceContext = {
  els: {
    partyPresetFields: element(),
    partyPresetSelect: element(),
    partyOpeningFields: element(),
    partyOpeningSelect: element(),
    characterDescriptionInput: element(),
  },
  escapeHtml: (value) => String(value),
  selectedRadioValue: (name) => choiceContext.radioValues[name] || "ready",
  selectedWorldpack: () => currentPack,
  isCleanRpCreation: () => false,
  radioValues: { worldSource: "ready", characterSource: "ready" },
};
vm.runInNewContext(
  [
    sourceBetween("function resolveWorldpackChoice", "function syncAutoPartyTitle"),
    sourceBetween("function syncReadyCharacterDescription", "async function createParty"),
  ].join("\n"),
  choiceContext,
);

choiceContext.renderPartyWorldChoices();
assert.equal(choiceContext.els.partyPresetSelect.value, "action", "legacy preset must use the declared default");
assert.equal(choiceContext.els.partyOpeningSelect.value, "independent", "legacy opening must use the declared default");
assert.equal(choiceContext.els.partyPresetFields.classList.contains("hidden"), false);
assert.equal(choiceContext.els.partyOpeningFields.classList.contains("hidden"), false);
assert.equal(choiceContext.els.partyPresetSelect.hasAttribute("required"), true);
assert.equal(choiceContext.els.partyOpeningSelect.hasAttribute("required"), true);
choiceContext.syncReadyCharacterDescription();
assert.equal(choiceContext.els.characterDescriptionInput.value, "Независимый Иной");

currentPack = legacyPack;
choiceContext.renderPartyWorldChoices();
choiceContext.syncReadyCharacterDescription();
assert.equal(choiceContext.els.partyPresetFields.classList.contains("hidden"), true);
assert.equal(choiceContext.els.partyOpeningFields.classList.contains("hidden"), true);
assert.equal(choiceContext.els.partyPresetSelect.hasAttribute("required"), false);
assert.equal(choiceContext.els.partyOpeningSelect.hasAttribute("required"), false);
assert.equal(choiceContext.els.characterDescriptionInput.value, "Legacy player role");

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

async function runCleanCreateParty(scenario) {
  const calls = [];
  const selectedParties = [];
  const context = {
    els: {
      modelSelect: element("model-1"),
      partyTitleInput: element(`${scenario.source} party`),
      cleanWorldSelect: element(cleanWorld.id),
    },
    selectedRadioValue: () => "ready",
    isCleanRpCreation: () => true,
    loadCleanWorldDetail: async () => cleanWorld,
    cleanScenarioPayload: () => plain(scenario),
    cleanPartyCreatePayload: payloadContext.cleanPartyCreatePayload,
    setBusy() {},
    renderCleanRuntimeControls() {},
    closePartyDialog() {},
    boot: async () => {},
    selectParty: async (partyId) => selectedParties.push(partyId),
    autoStartParty: async () => {},
    showToast() {},
    async apiPost(path, payload) {
      calls.push({ path, payload: plain(payload) });
      if (path === "/api/parties") return { party: { id: "clean-party" } };
      throw new Error(`unexpected POST ${path}`);
    },
  };
  vm.runInNewContext(functionSource("createParty"), context);
  await context.createParty({ preventDefault() {} });
  return { calls, selectedParties };
}

async function runLegacyCreateParty(pack) {
  const calls = [];
  const toasts = [];
  const preset = (pack.presets || []).find((item) => item.id === pack.presets_default) || null;
  const opening = (pack.openings || []).find((item) => item.id === pack.openings_default) || null;
  const context = {
    els: {
      modelSelect: element("model-1"),
      partyTitleInput: element("Тестовая партия"),
      characterDescriptionInput: element(""),
      characterNameInput: element("Игрок"),
    },
    selectedRadioValue: (name, fallback = "ready") => ({ characterSource: "ready" }[name] || fallback),
    isCleanRpCreation: () => false,
    resolveWorldpack: async () => pack,
    selectedPartyWorldChoices: () => ({ preset, opening }),
    setBusy() {},
    renderCleanRuntimeControls() {},
    closePartyDialog() {},
    boot: async () => {},
    selectParty: async () => {},
    autoStartParty: async () => {},
    showToast(message) {
      toasts.push(String(message));
    },
    async apiPost(path, payload) {
      calls.push({ path, payload: plain(payload) });
      if (path === "/api/player-characters/draft") {
        return {
          draft: {
            worldpack_id: payload.worldpack_id,
            opening_id: payload.opening_id,
            name: payload.name,
            description: payload.concept,
            profile: {},
          },
        };
      }
      if (path === "/api/player-characters") return { player_character: { id: "character-1" } };
      if (path === "/api/parties") return { party: { id: "party-1" } };
      throw new Error(`unexpected POST ${path}`);
    },
  };
  vm.runInNewContext(functionSource("createParty"), context);
  await context.createParty({ preventDefault() {} });
  return { calls, toasts };
}

(async () => {
  for (const scenario of [presetScenario, freeScenario]) {
    const result = await runCleanCreateParty(scenario);
    assert.deepEqual(result.calls.map((call) => call.path), ["/api/parties"]);
    assert.equal(result.calls[0].payload.world_id, cleanWorld.id);
    assert.deepEqual(result.calls[0].payload.scenario, scenario);
    assert.deepEqual(result.selectedParties, ["clean-party"]);
  }

  const rev11 = await runLegacyCreateParty(rev11Pack);
  assert.deepEqual(rev11.calls.map((call) => call.path), [
    "/api/player-characters/draft",
    "/api/player-characters",
    "/api/parties",
  ]);
  assert.deepEqual(rev11.calls[0].payload, {
    worldpack_id: "rev11-world",
    name: "Игрок",
    concept: "Независимый Иной",
    opening_id: "independent",
  });
  assert.deepEqual(rev11.calls[2].payload, {
    title: "Тестовая партия",
    scenario_type: "rp",
    worldpack_id: "rev11-world",
    player_character_id: "character-1",
    model_profile_id: "model-1",
    preset_id: "action",
    opening_id: "independent",
  });
  assert.match(rev11.toasts.at(-1), /GM готовит/);

  const legacy = await runLegacyCreateParty(legacyPack);
  assert.deepEqual(legacy.calls[0].payload, {
    worldpack_id: "legacy-world",
    name: "Игрок",
    concept: "Legacy player role",
  });
  assert.deepEqual(legacy.calls[2].payload, {
    title: "Тестовая партия",
    scenario_type: "rp",
    worldpack_id: "legacy-world",
    player_character_id: "character-1",
    model_profile_id: "model-1",
  });

  console.log("light gui party dialog clean and legacy RP: ok");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
