"use strict";

const state = {
  auth: null,
  parties: [],
  worlds: [],
  models: [],
  party: null,
  turns: [],
  retry: null,
  loreDraft: null,
};

const byId = (id) => document.getElementById(id);
const els = {
  loginScreen: byId("loginScreen"), loginForm: byId("loginForm"),
  loginUsername: byId("loginUsername"), loginPassword: byId("loginPassword"),
  appShell: byId("appShell"), refreshButton: byId("refreshButton"),
  newPartyButton: byId("newPartyButton"), partyList: byId("partyList"),
  activePartyTitle: byId("activePartyTitle"), activeWorld: byId("activeWorld"),
  gatewayDot: byId("gatewayDot"), gatewayStatus: byId("gatewayStatus"),
  chatLog: byId("chatLog"), messageStatus: byId("messageStatus"),
  messageForm: byId("messageForm"), messageInput: byId("messageInput"),
  retryPanel: byId("retryPanel"), retryTitle: byId("retryTitle"), retryButton: byId("retryButton"),
  partyMeta: byId("partyMeta"), roleCards: byId("roleCards"),
  loreList: byId("loreList"), loreDraftForm: byId("loreDraftForm"), loreKind: byId("loreKind"),
  loreConfirmForm: byId("loreConfirmForm"), loreTitle: byId("loreTitle"),
  loreContent: byId("loreContent"), loreKeywords: byId("loreKeywords"),
  correctionForm: byId("correctionForm"), correctionInstruction: byId("correctionInstruction"),
  correctionList: byId("correctionList"), administratorList: byId("administratorList"),
  byokForm: byId("byokForm"), byokKey: byId("byokKey"), byokList: byId("byokList"),
  currentUserLabel: byId("currentUserLabel"), logoutButton: byId("logoutButton"),
  partyDialog: byId("partyDialog"), partyForm: byId("partyForm"), partyTitle: byId("partyTitle"),
  worldSelect: byId("worldSelect"), scenarioSelect: byId("scenarioSelect"),
  modelSelect: byId("modelSelect"), cancelPartyButton: byId("cancelPartyButton"), toast: byId("toast"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof body.detail === "string" ? body.detail : body.detail?.message || `HTTP ${response.status}`);
    error.status = response.status;
    error.detail = body.detail;
    throw error;
  }
  return body;
}

const get = (path) => api(path);
const post = (path, body, headers) => api(path, { method: "POST", body: JSON.stringify(body), headers });

async function bootstrap() {
  bindEvents();
  const me = await get("/api/auth/me");
  state.auth = me;
  showAuthenticated(!me.auth_enabled || me.authenticated);
  if (me.auth_enabled && !me.authenticated) return;
  await loadEverything();
}

function bindEvents() {
  els.loginForm.addEventListener("submit", login);
  els.logoutButton.addEventListener("click", logout);
  els.refreshButton.addEventListener("click", () => loadEverything());
  els.newPartyButton.addEventListener("click", openPartyDialog);
  els.cancelPartyButton.addEventListener("click", () => els.partyDialog.close());
  els.partyForm.addEventListener("submit", createParty);
  els.messageForm.addEventListener("submit", sendMessage);
  els.retryButton.addEventListener("click", retryRequest);
  els.loreDraftForm.addEventListener("submit", draftLore);
  els.loreConfirmForm.addEventListener("submit", confirmLore);
  els.correctionForm.addEventListener("submit", draftCorrection);
  els.correctionList.addEventListener("click", decideCorrection);
  els.administratorList.addEventListener("click", decideAdministrator);
  els.byokForm.addEventListener("submit", saveByok);
}

async function login(event) {
  event.preventDefault();
  try {
    state.auth = await post("/api/auth/login", {
      username: els.loginUsername.value.trim(), password: els.loginPassword.value,
    });
    showAuthenticated(true);
    await loadEverything();
  } catch (error) { toast(error.message); }
}

async function logout() {
  await post("/api/auth/logout", {});
  state.party = null;
  showAuthenticated(false);
}

function showAuthenticated(value) {
  els.loginScreen.classList.toggle("hidden", value);
  els.appShell.classList.toggle("hidden", !value);
  els.currentUserLabel.textContent = state.auth?.user?.username || "Локальный режим";
}

async function loadEverything() {
  setStatus("Загрузка…");
  try {
    const [health, worlds, models, parties] = await Promise.all([
      get("/health"), get("/api/worldpacks"), get("/api/model-profiles"), get("/api/parties"),
    ]);
    state.worlds = worlds.worldpacks || [];
    state.models = models.model_profiles || [];
    state.parties = parties.parties || [];
    els.gatewayDot.classList.toggle("ok", health.status === "ok");
    els.gatewayStatus.textContent = health.status === "ok" ? "gateway online" : "gateway error";
    renderPartyList();
    if (state.party) {
      const current = state.parties.find((item) => item.id === state.party.id);
      if (current) await selectParty(current.id);
    }
    setStatus("");
  } catch (error) { setStatus(error.message); }
}

function renderPartyList() {
  els.partyList.innerHTML = state.parties.length ? state.parties.map((party) => `
    <button class="party-item ${party.id === state.party?.id ? "active" : ""}" data-party-id="${escapeHtml(party.id)}">
      <strong>${escapeHtml(party.title)}</strong><span>${escapeHtml(party.scenario_title)}</span>
    </button>`).join("") : '<div class="empty-state">Создайте первую партию.</div>';
  els.partyList.querySelectorAll("[data-party-id]").forEach((node) => {
    node.addEventListener("click", () => selectParty(node.dataset.partyId));
  });
}

async function selectParty(partyId) {
  try {
    const result = await get(`/api/parties/${encodeURIComponent(partyId)}`);
    state.party = result.party;
    renderPartyList();
    els.activePartyTitle.textContent = state.party.title;
    els.activeWorld.textContent = `${state.party.world_title} · ${state.party.scenario_title}`;
    renderMeta();
    await refreshPartyPanels();
    if (state.party.current_version === 0) await startParty();
  } catch (error) { toast(error.message); }
}

async function refreshPartyPanels() {
  if (!state.party) return;
  const id = encodeURIComponent(state.party.id);
  const [history, supervisor, lore, corrections, proposals, byok] = await Promise.all([
    get(`/api/parties/${id}/history`), get(`/api/parties/${id}/supervisor`),
    get(`/api/parties/${id}/lore-cards`), get(`/api/parties/${id}/player-corrections`),
    get(`/api/parties/${id}/administrator/proposals`), get(`/api/parties/${id}/byok`),
  ]);
  state.turns = history.turns || [];
  renderChat();
  renderRoles(supervisor.roles || {});
  renderLore(lore.cards || []);
  renderCorrections(corrections.proposals || []);
  renderAdministrator(proposals.proposals || []);
  renderByok(byok.api_keys || []);
}

function renderMeta() {
  const party = state.party;
  els.partyMeta.innerHTML = party ? [
    ["Мир", party.world_title], ["Сценарий", party.scenario_title],
    ["Версия", party.current_version], ["Нарратор", party.narrator_model],
    ["World hash", party.world_hash.slice(0, 12)], ["Scenario hash", party.scenario_hash.slice(0, 12)],
  ].map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join("") : "";
}

function renderChat() {
  els.chatLog.innerHTML = state.turns.length ? state.turns.map((turn) => {
    const player = turn.turn_kind === "opening_scene" ? "" : `<article class="message user"><p>${escapeHtml(turn.player_text)}</p></article>`;
    return `${player}<article class="message assistant"><p>${escapeHtml(turn.narrator_text)}</p></article>`;
  }).join("") : '<div class="empty-state">Opening ещё не создан.</div>';
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
}

function renderRoles(roles) {
  els.roleCards.innerHTML = ["narrator", "atomic_service", "administrator"].map((key) => {
    const role = roles[key] || {};
    return `<article class="proposal"><strong>${escapeHtml(key)}</strong><p>${escapeHtml(role.provider || "—")} · ${escapeHtml(role.model || "—")}</p><p>${escapeHtml(role.status || "idle")}${role.kill_switch ? " · выключено" : ""}</p>${role.last_error ? `<p>${escapeHtml(role.last_error)}</p>` : ""}</article>`;
  }).join("");
}

function renderLore(cards) {
  els.loreList.innerHTML = cards.length ? cards.map((card) => `<article class="proposal"><strong>${escapeHtml(card.title || card.key || card.id)}</strong><p>${escapeHtml(card.content || "")}</p><small>${escapeHtml(card.origin || "world")}</small></article>`).join("") : '<div class="proposal">Карточек нет.</div>';
}

function renderCorrections(items) {
  els.correctionList.innerHTML = items.length ? items.map((item) => `<article class="proposal"><strong>${escapeHtml(item.target_slot)}</strong><p>${escapeHtml(item.before || "")} → ${escapeHtml(item.after || "удалить")}</p>${item.status === "pending" ? `<button data-correction="${item.id}" data-decision="accept">Принять</button><button data-correction="${item.id}" data-decision="reject">Отклонить</button>` : `<small>${escapeHtml(item.status)}</small>`}</article>`).join("") : '<div class="proposal">Предложений нет.</div>';
}

function renderAdministrator(items) {
  els.administratorList.innerHTML = items.length ? items.map((item) => `<article class="proposal"><strong>${escapeHtml(item.target_slot)}</strong><p>${escapeHtml(item.before_text || "")} → ${escapeHtml(item.after_text || "")}</p>${item.status === "pending" ? `<button data-administrator="${item.id}" data-decision="accept">Принять</button><button data-administrator="${item.id}" data-decision="reject">Отклонить</button>` : `<small>${escapeHtml(item.status)}</small>`}</article>`).join("") : '<div class="proposal">Предложений нет.</div>';
}

function renderByok(items) {
  els.byokList.innerHTML = items.length ? items.map((item) => `<div class="proposal">${escapeHtml(item.label)} · …${escapeHtml(item.secret_hint)}</div>`).join("") : '<div class="proposal">Используется server key.</div>';
}

function openPartyDialog() {
  els.worldSelect.innerHTML = state.worlds.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`).join("");
  const world = state.worlds[0];
  els.scenarioSelect.innerHTML = (world?.scenario_presets || []).map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`).join("");
  els.modelSelect.innerHTML = state.models.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`).join("");
  els.partyTitle.value = "Новая партия";
  els.partyDialog.showModal();
}

async function createParty(event) {
  event.preventDefault();
  try {
    const result = await post("/api/parties", {
      title: els.partyTitle.value.trim(), world_id: els.worldSelect.value,
      scenario: { source: "preset", preset_id: els.scenarioSelect.value },
      model_profile_id: els.modelSelect.value,
    });
    els.partyDialog.close();
    state.parties.push(result.party);
    await selectParty(result.party.id);
  } catch (error) { toast(error.message); }
}

function requestIdentity(prefix) {
  return `${prefix}-${crypto.randomUUID()}`;
}

async function startParty(retry = null) {
  const identity = retry || { kind: "start", requestId: requestIdentity("opening") };
  state.retry = identity;
  setStatus("Нарратор создаёт opening…");
  try {
    const result = await post(`/api/parties/${encodeURIComponent(state.party.id)}/start`, { idempotency_key: identity.requestId }, { "X-Request-ID": identity.requestId });
    state.retry = null;
    state.party.current_version = result.state_version;
    await refreshPartyPanels();
    showRetry(false);
    setStatus("");
  } catch (error) { handleRetryable(error, identity, "Opening не сохранён"); }
}

async function sendMessage(event) {
  event.preventDefault();
  if (!state.party || state.party.current_version < 1) return;
  const text = els.messageInput.value.trim();
  if (!text) return;
  const identity = { kind: "message", requestId: requestIdentity("turn"), text, expectedVersion: state.party.current_version };
  els.messageInput.value = "";
  await sendTurn(identity);
}

async function sendTurn(identity) {
  state.retry = identity;
  setStatus("Нарратор отвечает…");
  try {
    const result = await post(`/api/parties/${encodeURIComponent(state.party.id)}/messages`, {
      content: identity.text, idempotency_key: identity.requestId, expected_version: identity.expectedVersion,
    }, { "X-Request-ID": identity.requestId });
    state.retry = null;
    state.party.current_version = result.state_version;
    await refreshPartyPanels();
    showRetry(false);
    setStatus("");
  } catch (error) { handleRetryable(error, identity, "Ход не сохранён"); }
}

function handleRetryable(error, identity, title) {
  if (![502, 503].includes(error.status) || error.detail?.retryable !== true) state.retry = null;
  els.retryTitle.textContent = title;
  showRetry(Boolean(state.retry));
  setStatus(error.message);
}

function retryRequest() {
  if (!state.retry) return;
  return state.retry.kind === "start" ? startParty(state.retry) : sendTurn(state.retry);
}

function showRetry(value) { els.retryPanel.classList.toggle("hidden", !value); }

async function draftLore(event) {
  event.preventDefault();
  const turn = state.turns.at(-1);
  if (!turn) return toast("Сначала нужен завершённый ход.");
  try {
    const result = await post(`/api/parties/${encodeURIComponent(state.party.id)}/lore-cards/draft`, {
      source_turn_ids: [turn.id], kind: els.loreKind.value,
      expected_version: state.party.current_version, idempotency_key: requestIdentity("lore"),
    });
    if (result.result === "no_candidate") return toast("Модель не нашла подтверждённый факт.");
    state.loreDraft = result;
    els.loreTitle.value = result.title;
    els.loreContent.value = result.content;
    els.loreKeywords.value = result.keywords.join(", ");
    els.loreConfirmForm.classList.remove("hidden");
  } catch (error) { toast(error.message); }
}

async function confirmLore(event) {
  event.preventDefault();
  const draft = state.loreDraft;
  if (!draft) return;
  try {
    await post(`/api/parties/${encodeURIComponent(state.party.id)}/lore-cards`, {
      title: els.loreTitle.value.trim(), content: els.loreContent.value.trim(),
      keywords: els.loreKeywords.value.split(",").map((item) => item.trim()).filter(Boolean),
      source_turn_ids: draft.source_turn_ids, kind: draft.kind, draft_job_id: draft.job_id,
      expected_version: state.party.current_version, idempotency_key: requestIdentity("lore-confirm"),
      always_on: false, enabled: true,
    });
    state.loreDraft = null;
    els.loreConfirmForm.classList.add("hidden");
    await refreshPartyPanels();
  } catch (error) { toast(error.message); }
}

async function draftCorrection(event) {
  event.preventDefault();
  try {
    await post(`/api/parties/${encodeURIComponent(state.party.id)}/player-corrections/draft`, {
      instruction: els.correctionInstruction.value.trim(), expected_version: state.party.current_version,
      idempotency_key: requestIdentity("correction"),
    });
    els.correctionInstruction.value = "";
    await refreshPartyPanels();
  } catch (error) { toast(error.message); }
}

async function decideCorrection(event) {
  const button = event.target.closest("[data-correction]");
  if (!button) return;
  try {
    await post(`/api/parties/${encodeURIComponent(state.party.id)}/player-corrections/${button.dataset.correction}/decision`, {
      decision: button.dataset.decision, expected_version: state.party.current_version,
      idempotency_key: requestIdentity("correction-decision"),
    });
    await refreshPartyPanels();
  } catch (error) { toast(error.message); }
}

async function decideAdministrator(event) {
  const button = event.target.closest("[data-administrator]");
  if (!button) return;
  try {
    const result = await post(`/api/parties/${encodeURIComponent(state.party.id)}/administrator/proposals/${button.dataset.administrator}/decision`, { decision: button.dataset.decision });
    state.party.current_version = result.state_version;
    await refreshPartyPanels();
  } catch (error) { toast(error.message); }
}

async function saveByok(event) {
  event.preventDefault();
  try {
    await post(`/api/parties/${encodeURIComponent(state.party.id)}/byok`, {
      label: "OpenRouter key", api_key: els.byokKey.value, provider: "openrouter", is_default: true,
    });
    els.byokKey.value = "";
    await refreshPartyPanels();
  } catch (error) { toast(error.message); }
}

function setStatus(text) {
  els.messageStatus.textContent = text;
  els.messageStatus.classList.toggle("hidden", !text);
}

function toast(text) {
  els.toast.textContent = text;
  els.toast.classList.add("visible");
  setTimeout(() => els.toast.classList.remove("visible"), 3500);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
}

void bootstrap().catch((error) => setStatus(error.message));
