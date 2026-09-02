"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { normalizeMessageDate } = require("./message-time.js");

const source = fs.readFileSync(require.resolve("./app.js"), "utf8");
const html = fs.readFileSync(require.resolve("./index.html"), "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));

function functionSource(name) {
  const marker = new RegExp("(?:async\\s+)?function\\s+" + name + "\\s*\\(");
  const match = marker.exec(source);
  assert.ok(match, name + " must remain testable");
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
  assert.notEqual(brace, -1, name + " body must remain testable");
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
    if (char === '"' || char === "'" || char === String.fromCharCode(96)) {
      quote = char;
      continue;
    }
    if (char === "{") depth += 1;
    if (char === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(match.index, index + 1);
    }
  }
  assert.fail(name + " source is incomplete");
}

const cleanParty = {
  id: "party-clean",
  scenario_type: "rp",
  world_id: "day-watch-moscow-v2",
  current_version: 3,
};

const payloadContext = {};
vm.runInNewContext(functionSource("partyMessagePayload"), payloadContext);
assert.deepEqual(
  plain(payloadContext.partyMessagePayload(
    "Я вхожу в арку.",
    "request-clean",
    [],
    "auto",
    null,
    3,
  )),
  {
    content: "Я вхожу в арку.",
    idempotency_key: "request-clean",
    expected_version: 3,
  },
  "clean RP message must contain only the exact optimistic-lock payload",
);
assert.deepEqual(
  plain(payloadContext.partyMessagePayload("Training answer", "request-training")),
  { content: "Training answer", idempotency_key: "request-training" },
  "retained Training keeps the legacy message payload",
);

const turnContext = { AUTO_START_HISTORY_MESSAGE: "[AUTO_START]" };
for (const name of ["turnPlayerText", "turnNarratorText", "isAutoStartTurn"]) {
  vm.runInNewContext(functionSource(name), turnContext);
}
const opening = {
  turn_kind: "opening_scene",
  player_text: "",
  narrator_text: "Ночной Дозор вызывает вас на Арбат.",
  created_at: 1_725_000_000_000_000_000,
};
assert.equal(turnContext.isAutoStartTurn(opening), true);
assert.equal(turnContext.turnPlayerText(opening), "");
assert.equal(turnContext.turnNarratorText(opening), opening.narrator_text);
assert.equal(
  normalizeMessageDate(opening.created_at).toISOString(),
  "2024-08-30T06:40:00.000Z",
  "clean time_ns timestamps must render as wall-clock time",
);

const legacyTurn = {
  player_message: "Игрок отвечает.",
  narrative_response: "Training continues.",
};
assert.equal(turnContext.turnPlayerText(legacyTurn), legacyTurn.player_message);
assert.equal(turnContext.turnNarratorText(legacyTurn), legacyTurn.narrative_response);
assert.match(functionSource("renderChat"), /turnPlayerText\(turn\)/);
assert.match(functionSource("renderChat"), /turnNarratorText\(turn\)/);

async function testOpeningRetryUsesOneIdentity() {
  const calls = [];
  let fail = true;
  let reloads = 0;
  const context = {
    appState: { activeParty: { ...cleanParty, current_version: 0 } },
    isCleanRpParty: () => true,
    activePendingMessage: () => null,
    startPendingMessage() {},
    appendPendingStartMessage() {},
    setPendingStatus() {},
    replacePendingMessage() {},
    clearPendingMessage() {},
    renderCleanRuntimeControls() {},
    showToast() {},
    waitForRecoveredMessage: async () => null,
    reloadPartyIfActive: async () => { reloads += 1; },
    turnNarratorText: turnContext.turnNarratorText,
    async apiPost(path, body, headers) {
      calls.push({ path, body: plain(body), headers: plain(headers) });
      if (fail) {
        fail = false;
        const error = new Error("Narrator unavailable");
        error.status = 502;
        error.detail = { retryable: true };
        throw error;
      }
      return {
        started: true,
        message: { role: "assistant", content: opening.narrator_text },
        turn: opening,
      };
    },
  };
  vm.runInNewContext(functionSource("autoStartParty"), context);
  vm.runInNewContext(functionSource("retryOpeningParty"), context);
  await context.autoStartParty(cleanParty.id);
  await context.retryOpeningParty();
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1], calls[0], "manual opening retry must reuse request and idempotency keys");
  assert.equal(reloads, 1);
}

async function testTerminalPreclaimDoesNotPollUnknownRequest() {
  for (const status of [409, 503]) {
    let recoveryCalls = 0;
    const context = {
      appState: { activeParty: { ...cleanParty, current_version: 0 } },
      startPendingMessage() {},
      appendPendingStartMessage() {},
      setPendingStatus() {},
      replacePendingMessage() {},
      clearPendingMessage() {},
      renderCleanRuntimeControls() {},
      showToast() {},
      turnNarratorText: turnContext.turnNarratorText,
      reloadPartyIfActive: async () => {},
      waitForRecoveredMessage: async () => {
        recoveryCalls += 1;
        return null;
      },
      async apiPost() {
        const error = new Error(status === 409 ? "Narrator binding retired" : "Narrator disabled");
        error.status = status;
        throw error;
      },
    };
    vm.runInNewContext(functionSource("autoStartParty"), context);
    await context.autoStartParty(cleanParty.id);
    assert.equal(
      recoveryCalls,
      0,
      `a pre-claim ${status} must expose retry immediately instead of polling unknown status`,
    );
  }
}

function testFailedRetriesPersistPerParty() {
  const values = new Map();
  const localStorage = {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
  const context = {
    appState: { failedMessageRetries: {} },
    localStorage,
    FAILED_RETRY_STORAGE_KEY: "failed-retries",
    PENDING_MAX_AGE_MS: 60 * 60 * 1000,
  };
  for (const name of [
    "failedMessageRetryForParty",
    "setFailedMessageRetry",
    "clearFailedMessageRetry",
    "restoreFailedMessageRetries",
    "saveFailedMessageRetries",
  ]) {
    vm.runInNewContext(functionSource(name), context);
  }
  const retryA = {
    partyId: "party-a",
    text: "Действие A",
    requestId: "request-a",
    expectedVersion: 1,
    createdAt: Date.now(),
  };
  const retryB = {
    partyId: "party-b",
    text: "Действие B",
    requestId: "request-b",
    expectedVersion: 4,
    createdAt: Date.now(),
  };
  context.setFailedMessageRetry(retryA);
  context.setFailedMessageRetry(retryB);
  context.appState.failedMessageRetries = {};
  context.restoreFailedMessageRetries();
  assert.equal(context.failedMessageRetryForParty("party-a").requestId, "request-a");
  assert.equal(context.failedMessageRetryForParty("party-b").requestId, "request-b");
  context.clearFailedMessageRetry("party-a");
  assert.equal(context.failedMessageRetryForParty("party-a"), null);
  assert.equal(context.failedMessageRetryForParty("party-b").text, "Действие B");
}

async function runMessageFailure(status) {
  const calls = [];
  let reloads = 0;
  let recoveryCalls = 0;
  const lifecycle = [];
  const failedMessageRetries = {};
  const text = status === 409 ? "Устаревшее действие" : "Текст должен остаться в поле";
  const requestId = status === 409 ? "request-stale" : "request-failed";
  const error = new Error(status === 409 ? "Party version conflict" : "Narrator unavailable");
  error.status = status;
  error.detail = status === 409
    ? "party is at version 4, not 3"
    : {
        code: "rp_narrator_unavailable",
        retryable: true,
        request_id: requestId,
        idempotency_key: requestId,
        player_text: text,
      };
  error.response = { detail: error.detail };

  const context = {
    appState: {
      activeParty: { ...cleanParty },
      pendingStoryMemoryCorrections: [],
      pendingGmDraft: null,
      pendingGmRoute: null,
      failedMessageRetries,
    },
    els: { messageInput: { value: "" } },
    activePendingMessage: () => null,
    makeClientRequestId: () => "unexpected-generated-key",
    isCleanRpParty: () => true,
    failedMessageRetryForParty: (partyId) => failedMessageRetries[partyId] || null,
    setFailedMessageRetry: (retry) => { failedMessageRetries[retry.partyId] = retry; },
    clearFailedMessageRetry: (partyId) => { delete failedMessageRetries[partyId]; },
    startPendingMessage() {},
    appendPendingMessage() {},
    setPendingStatus() {},
    replacePendingMessage() {},
    renderChat() {},
    renderGmDecision() {},
    renderMemory() {},
    clearPendingMessage() { lifecycle.push("clear"); },
    showToast() {},
    partyMessagePayload: payloadContext.partyMessagePayload,
    turnNarratorText: turnContext.turnNarratorText,
    recoverTurn: async () => {
      recoveryCalls += 1;
      return null;
    },
    waitForRecoveredMessage: async () => {
      recoveryCalls += 1;
      return null;
    },
    reloadPartyIfActive: async () => { reloads += 1; lifecycle.push("reload"); },
    async apiPost(path, body, headers) {
      calls.push({ path, body: plain(body), headers: plain(headers) });
      throw error;
    },
  };
  vm.runInNewContext(functionSource("submitPartyMessage"), context);
  await context.submitPartyMessage(text, { requestId, expectedVersion: 3 });
  return { calls, context, reloads, recoveryCalls, lifecycle, requestId, text };
}

async function testProviderFailurePreservesRetry() {
  const result = await runMessageFailure(502);
  assert.equal(result.calls.length, 1, "semantic/provider failure must never auto-submit a second model call");
  assert.deepEqual(result.calls[0], {
    path: "/api/parties/" + cleanParty.id + "/messages",
    body: {
      content: result.text,
      idempotency_key: result.requestId,
      expected_version: 3,
    },
    headers: { "X-Request-ID": result.requestId },
  });
  assert.equal(result.context.els.messageInput.value, result.text);
  const retry = result.context.appState.failedMessageRetries[cleanParty.id];
  assert.equal(retry?.text, result.text);
  assert.equal(retry?.requestId, result.requestId);
  assert.equal(retry?.expectedVersion, 3);
  await result.context.submitPartyMessage(result.text);
  assert.equal(result.calls.length, 2, "manual retry is the only second provider request");
  assert.deepEqual(result.calls[1], result.calls[0], "manual retry must preserve exact key and version");
}

async function testStaleReloadsWithoutReplay() {
  const result = await runMessageFailure(409);
  assert.equal(result.calls.length, 1, "stale version must not replay the action automatically");
  assert.equal(result.reloads, 1, "stale version must refresh Party and history");
  assert.equal(result.recoveryCalls, 0, "a terminal 409 is not an ambiguous transport failure");
  assert.ok(
    result.lifecycle.indexOf("clear") < result.lifecycle.indexOf("reload"),
    "409 must clear pending state before Party reload can start recovery polling",
  );
  assert.equal(result.context.els.messageInput.value, result.text);
  assert.equal(
    result.context.appState.failedMessageRetries[cleanParty.id],
    undefined,
    "stale key cannot be reused against a new version",
  );
}

async function testAdministratorDecisions() {
  const calls = [];
  let reloads = 0;
  const context = {
    appState: { activeParty: { ...cleanParty } },
    reloadPartyIfActive: async () => { reloads += 1; },
    async apiPost(path, body) {
      calls.push({ path, body: plain(body) });
      return {
        party_id: cleanParty.id,
        state_version: 4,
        proposal: {
          id: 17,
          status: body.decision === "accept" ? "accepted" : "rejected",
        },
      };
    },
  };
  vm.runInNewContext(functionSource("decideAdministratorProposal"), context);
  await context.decideAdministratorProposal(17, "accept");
  await context.decideAdministratorProposal(18, "reject");
  assert.deepEqual(calls, [
    {
      path: "/api/parties/" + cleanParty.id + "/administrator/proposals/17/decision",
      body: { decision: "accept" },
    },
    {
      path: "/api/parties/" + cleanParty.id + "/administrator/proposals/18/decision",
      body: { decision: "reject" },
    },
  ]);
  assert.equal(reloads, 2);
}

assert.match(html, /id="retryOpeningButton"/);
assert.match(html, /data-administrator-decision="accept"/);
assert.match(html, /data-administrator-decision="reject"/);
assert.match(html, /id="playerCorrectionForm"/);
assert.match(source, /\/player-corrections\/draft/);
assert.match(source, /data-player-correction-decision="accept"/);
assert.match(source, /Исправление войдёт ровно в следующий prompt/);
assert.match(html, /message-time\.js\?v=20260830-rp-clean-time/);
assert.match(html, /app\.js\?v=20260830-rp-clean-cutover/);
assert.match(
  functionSource("createParty"),
  /finally\s*{\s*setBusy\(false\);\s*renderCleanRuntimeControls\(\);/,
  "creation must re-enable opening retry after busy state ends",
);

(async () => {
  await testOpeningRetryUsesOneIdentity();
  await testTerminalPreclaimDoesNotPollUnknownRequest();
  testFailedRetriesPersistPerParty();
  await testProviderFailurePreservesRetry();
  await testStaleReloadsWithoutReplay();
  await testAdministratorDecisions();
  console.log("light gui clean RP turn flow: ok");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
