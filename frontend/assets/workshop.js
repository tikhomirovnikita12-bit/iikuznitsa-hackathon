"use strict";

const get = (id) => document.getElementById(id);
const defaultPair = { vehicle_id: "E549YT198", session_id: "S-0011" };
const severities = {
  normal: ["Норма", "good", "check"],
  anomaly: ["Отклонение", "warning", "alert"],
  check_failed: ["Сбой записи", "error", "alert"],
};
const ticketStates = {
  draft: ["Черновик", ""],
  sending: ["Отправляется", "warning"],
  sent: ["Отправлена", "good"],
  delivery_failed: ["Ошибка доставки", "error"],
  delivery_unknown: ["Нужна сверка", "warning"],
};
const urgencyLabels = { low: "Низкий", medium: "Средний", high: "Высокий" };
const quality = { good: "Хорошее", warning: "Есть помехи", bad: "Плохое" };
const dateFormat = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  timeZone: "Europe/Moscow",
});
const percentFormat = new Intl.NumberFormat("ru-RU", { style: "percent", maximumFractionDigits: 0 });
const state = {
  vehicles: [], sessions: [], vehicle: null, current: null, contexts: new Map(),
  loadingSessions: false, selection: 0, library: null, editor: null, reconciliation: null,
};

function node(tag, className = "", text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS(svg.namespaceURI, "use");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}

function button(text, tone, onClick, iconName) {
  const control = node("button", "button button-" + tone);
  control.type = "button";
  if (iconName) control.append(icon(iconName));
  control.append(node("span", "", text));
  control.addEventListener("click", onClick);
  return control;
}

function dateLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : dateFormat.format(date);
}

function vehicleName(vehicle) {
  if (!vehicle) return "";
  if (!vehicle.model || vehicle.model.startsWith(vehicle.make)) return vehicle.model || vehicle.make;
  return vehicle.make + " " + vehicle.model;
}

function safeManualUrl(url) {
  return typeof url === "string" && /^\/manuals\/[a-z0-9-]+\.pdf(?:#page=[1-9][0-9]*)?$/.test(url) ? url : null;
}

async function api(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      cache: "no-store",
      ...(body === undefined ? {} : {
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    });
  } catch {
    throw new Error("Не удалось связаться с бэкендом. Проверьте подключение и повторите запрос.");
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok || payload === null) {
    const error = new Error(payload?.error?.message || "Бэкенд вернул ошибку HTTP " + response.status + ".");
    error.code = payload?.error?.code;
    error.draft = payload?.draft;
    throw error;
  }
  return payload;
}

function readPair() {
  try {
    return JSON.parse(localStorage.getItem("mechanic-pair")) || defaultPair;
  } catch {
    return defaultPair;
  }
}

function savePair(pair) {
  try { localStorage.setItem("mechanic-pair", JSON.stringify(pair)); } catch { /* Storage may be disabled. */ }
}

function chatKey(pair) {
  return "mechanic-chat:" + JSON.stringify([pair.vehicle_id, pair.session_id]);
}

function readTurns(pair) {
  try {
    const turns = JSON.parse(localStorage.getItem(chatKey(pair)));
    return Array.isArray(turns) ? turns.slice(-6) : [];
  } catch {
    return [];
  }
}

function setOnline(online) {
  get("system-status").className = "system-status " + (online ? "online" : "offline");
  get("system-label").textContent = online ? "API на связи" : "API недоступен";
}

function showCatalogError(message, retry) {
  const target = get("catalog-error");
  target.hidden = !message;
  target.replaceChildren();
  if (message) target.append(node("span", "", message), button("Повторить", "light", retry));
}

function showHero() {
  const vehicle = state.vehicle;
  get("hero-id").textContent = vehicle?.vehicle_id || "Выберите ТС";
  get("hero-make").textContent = vehicleName(vehicle) || "Каталог автопарка";
  get("hero-engine").hidden = !vehicle;
  get("hero-engine").textContent = vehicle?.engine_model || "";
}

function fillSessions() {
  const target = get("session-select");
  target.replaceChildren();
  if (!state.sessions.length) {
    target.append(new Option(state.loadingSessions ? "Загрузка сессий…" : "Нет доступных сессий", ""));
  } else {
    for (const session of state.sessions) {
      target.append(new Option(session.session_id + " · " + dateLabel(session.checked_at), session.session_id));
    }
  }
  target.disabled = state.loadingSessions || !state.sessions.length;
}

async function start() {
  showCatalogError("");
  try {
    state.vehicles = await api("/api/vehicles");
    setOnline(true);
    const select = get("vehicle-select");
    select.replaceChildren();
    for (const item of state.vehicles) {
      select.append(new Option(item.vehicle_id + " · " + vehicleName(item), item.vehicle_id));
    }
    select.disabled = !state.vehicles.length;
    if (!state.vehicles.length) {
      select.append(new Option("Каталог пуст", ""));
      showHero();
      render();
      return;
    }
    const saved = readPair();
    const vehicle = state.vehicles.find((item) => item.vehicle_id === saved.vehicle_id) || state.vehicles[0];
    await selectVehicle(vehicle.vehicle_id, saved.session_id);
  } catch (error) {
    setOnline(false);
    showCatalogError(error.message, start);
    showHero();
  }
}

async function selectVehicle(id, sessionId) {
  const selected = state.vehicles.find((item) => item.vehicle_id === id);
  if (!selected) return;
  const selection = ++state.selection;
  state.vehicle = selected;
  state.sessions = [];
  state.current = null;
  state.loadingSessions = true;
  get("vehicle-select").value = id;
  showHero();
  clearChatInput();
  showCatalogError("");
  fillSessions();
  render();
  try {
    const sessions = await api("/api/vehicles/" + encodeURIComponent(id) + "/sessions");
    if (selection !== state.selection) return;
    state.loadingSessions = false;
    state.sessions = sessions.sort((a, b) => Date.parse(b.checked_at) - Date.parse(a.checked_at));
    fillSessions();
    selectSession(state.sessions.find((session) => session.session_id === sessionId)?.session_id
      || state.sessions[0]?.session_id);
  } catch (error) {
    if (selection !== state.selection) return;
    state.loadingSessions = false;
    showCatalogError(error.message, () => selectVehicle(id, sessionId));
    fillSessions();
    render();
  }
}

function selectSession(id) {
  const session = state.sessions.find((item) => item.session_id === id);
  if (!session) {
    state.current = null;
    render();
    return;
  }
  const pair = { vehicle_id: state.vehicle.vehicle_id, session_id: id };
  const key = pair.vehicle_id + "/" + id;
  let context = state.contexts.get(key);
  if (!context) {
    const turns = readTurns(pair);
    context = {
      pair, session, engine: state.vehicle.engine_model, result: null, drafts: [], turns,
      messages: turns.flatMap((turn) => [{ text: turn.question, me: true },
        { text: turn.answer, evidence: turn.evidence }]),
      loading: true, diagnosing: false, asking: false, ticketBusy: false, diagnoseError: "",
      ticketError: "", loaded: false, poll: null,
    };
    state.contexts.set(key, context);
  }
  state.current = context;
  get("session-select").value = id;
  savePair(pair);
  clearChatInput();
  render();
  if (!context.loaded) restore(context);
  else scheduleDraftPoll(context);
}

async function restore(context) {
  if (context.restoring) return;
  context.restoring = true;
  const query = new URLSearchParams(context.pair);
  const [diagnosis, drafts] = await Promise.allSettled([
    api("/api/diagnosis?" + query),
    api("/api/drafts?" + query),
  ]);
  if (diagnosis.status === "fulfilled") {
    context.result = diagnosis.value;
  } else if (diagnosis.reason.code !== "diagnosis_required") {
    context.diagnoseError = diagnosis.reason.message;
  }
  if (drafts.status === "fulfilled") context.drafts = drafts.value;
  else context.ticketError = drafts.reason.message;
  context.loaded = true;
  context.loading = false;
  context.restoring = false;
  renderIfCurrent(context);
  scheduleDraftPoll(context);
}

function renderIfCurrent(context) {
  if (state.current === context) render();
}

async function diagnose() {
  const context = state.current;
  if (!context || context.loading || context.diagnosing) return;
  context.diagnosing = true;
  context.diagnoseError = "";
  render();
  try {
    context.result = await api("/api/diagnose", { method: "POST", body: context.pair });
  } catch (error) {
    context.diagnoseError = error.message;
  } finally {
    context.diagnosing = false;
    renderIfCurrent(context);
  }
}

function statusPill(element, text, kind = "") {
  element.className = "pill" + (kind ? " " + kind : "");
  element.textContent = text;
  return element;
}

function comfort(title, text, glyph) {
  const block = node("div", "comfort-state");
  const copy = node("div");
  copy.append(node("strong", "", title), node("p", "", text));
  block.append(icon(glyph), copy);
  return block;
}

function sourceList(items) {
  const list = node("ul", "sources");
  const seen = new Set();
  for (const item of items || []) {
    const source = item.source;
    const key = source.manual + "/" + source.pdf_page;
    if (seen.has(key)) continue;
    seen.add(key);
    const url = safeManualUrl(source.url);
    const row = node("li", "source-line");
    const link = node(url ? "a" : "span");
    if (url) { link.href = url; link.target = "_blank"; link.rel = "noopener"; }
    link.append(icon(url ? "open" : "book"), node("b", "", source.manual + " · стр. " + source.pdf_page));
    row.append(link);
    list.append(row);
  }
  return list;
}

function resultDetail(target, title, value) {
  if (!value || !value.length) return;
  const block = node("div", "result-detail");
  block.append(node("h3", "", title));
  if (typeof value === "string") block.append(node("p", "", value));
  else {
    const list = node("ul");
    for (const item of value) list.append(node("li", "", item));
    block.append(list);
  }
  target.append(block);
}

function renderDiagnosis(context) {
  const target = get("diagnosis-result");
  const status = get("result-status");
  const subtitle = get("result-subtitle");
  target.replaceChildren();
  subtitle.textContent = context ? context.pair.session_id + " · " + dateLabel(context.session.checked_at)
    : "Акустическая проверка НейроДВС";

  if (context?.diagnoseError) {
    const note = node("p", "notice notice-error", context.diagnoseError);
    note.setAttribute("role", "alert");
    target.append(note);
  }
  if (!context?.result) {
    statusPill(status, context?.diagnosing || context?.loading ? "Загрузка…" : "Ожидает запроса");
    target.append(comfort(
      context?.diagnosing ? "Запрашиваем результат" : "Нет результата по этой сессии",
      context?.diagnosing ? "НейроДВС готовит ответ по выбранной записи."
        : "Выберите сессию и нажмите «Получить результат».",
      "wave",
    ));
    return;
  }

  const { diagnosis, assessment } = context.result;
  const [label, tone] = severities[diagnosis.status];
  statusPill(status, label, tone);
  if (context.diagnosing || context.diagnoseError) {
    target.append(node("p", "diagnosis-meta", "Показан сохранённый результат."));
  }
  const lead = node("div", "result-lead " + tone);
  const title = diagnosis.anomaly_title || (diagnosis.status === "normal" ? "Значимых отклонений нет" : "Запись не прошла проверку");
  lead.append(node("h3", "", title), node("p", "", diagnosis.description));
  target.append(lead);

  const metrics = node("div", "measurements");
  for (const [name, value] of [
    ["Оценка аномалии", diagnosis.anomaly_score], ["Уверенность ML", diagnosis.confidence],
    ["Качество записи", quality[diagnosis.signal_quality]],
  ]) {
    const display = typeof value === "number" ? percentFormat.format(value) : value ?? "—";
    const item = node("div", "measurement");
    item.append(node("strong", "", display), node("span", "", name));
    metrics.append(item);
  }
  target.append(metrics);

  if (diagnosis.status === "anomaly") resultDetail(target, "Заключение", assessment.conclusion);
  resultDetail(target, "Что делать", assessment.next_step);
  resultDetail(target, "Оценка риска", assessment.risk);
  resultDetail(target, "Возможные причины", assessment.possible_causes);
  resultDetail(target, "Рекомендуемые проверки", assessment.checks);
  resultDetail(target, "Возможный ремонт", assessment.repair_actions);
  if (assessment.evidence?.length) {
    const evidence = node("div", "result-detail");
    evidence.append(node("h3", "", "Руководства"), sourceList(assessment.evidence));
    target.append(evidence);
  }
  const code = diagnosis.anomaly_type ? " · Код " + diagnosis.anomaly_type : "";
  target.append(node("p", "diagnosis-meta", "Модель " + diagnosis.model_version + code));
}

function renderSessions() {
  const list = get("session-list");
  const count = get("session-count");
  count.hidden = !state.sessions.length;
  count.textContent = state.sessions.length;
  list.replaceChildren();
  if (!state.sessions.length) {
    list.append(node("p", "", state.loadingSessions ? "Загружаем историю…" : "Нет доступных записей."));
    return;
  }
  for (const session of state.sessions) {
    const active = state.current?.pair.session_id === session.session_id;
    const control = node("button", "session-row" + (active ? " selected" : ""));
    control.type = "button";
    control.setAttribute("aria-pressed", String(active));
    control.addEventListener("click", () => {
      const index = Array.from(list.children).indexOf(control);
      selectSession(session.session_id);
      list.children[index]?.focus();
    });
    control.append(node("span", "session-id", session.session_id), node("span", "date", dateLabel(session.checked_at)));
    const result = state.contexts.get(state.vehicle.vehicle_id + "/" + session.session_id)?.result;
    if (result) {
      const [label, style] = severities[result.diagnosis.status];
      control.append(statusPill(node("span"), label, style));
    } else control.append(icon("chevron"));
    list.append(control);
  }
}

function render() {
  const context = state.current;
  get("diagnose-button").disabled = !context || context.loading || context.diagnosing;
  get("diagnose-label").textContent = context?.diagnosing ? "Получаем результат…" : "Получить результат";
  renderDiagnosis(context);
  renderSessions();
  renderChat(context);
  renderTicket(context);
}

function clearChatInput() {
  get("chat-input").value = "";
}

function updateChatControls() {
  const context = state.current;
  get("chat-input").disabled = !context || context.loading || context.asking;
  get("chat-submit").disabled = get("chat-input").disabled || !get("chat-input").value.trim();
}

function renderChat(context) {
  const feed = get("chat-feed");
  const suggestions = get("chat-suggestions");
  feed.replaceChildren();
  suggestions.replaceChildren();
  const messages = context?.messages || [];
  const intro = context?.result
    ? "Задайте вопрос о признаках, проверках или ремонте. В ответе будут ссылки на подходящие страницы."
    : "Запустите диагностику командой «Получи результат диагностики».";
  for (const message of [{ text: intro }, ...messages, ...(context?.asking ? [{ text: "Ищу в руководствах…" }] : [])]) {
    const row = node("div", "message" + (message.me ? " message-me" : "") + (message.error ? " message-error" : ""));
    if (!message.me) {
      const marker = node("div", "chat-icon");
      marker.append(icon(message.error ? "alert" : "spark"));
      row.append(marker);
    }
    const bubble = node("div", "message-content");
    bubble.append(node("p", "", message.text));
    if (message.evidence?.length) bubble.append(sourceList(message.evidence));
    row.append(bubble);
    feed.append(row);
  }
  if (context?.result && !messages.length && !context.asking) {
    const prompts = context.result.diagnosis.anomaly_type === "F02"
      ? ["Что проверить сначала?", "Как измерить давление масла?", "Почему это опасно?"]
      : ["Что проверить сначала?", "Какие возможны причины?"];
    for (const phrase of prompts) {
      const control = node("button", "suggestion", phrase);
      control.type = "button";
      control.addEventListener("click", () => ask(phrase));
      suggestions.append(control);
    }
  }
  suggestions.hidden = !suggestions.children.length;
  feed.scrollTop = feed.scrollHeight;
  updateChatControls();
}

async function ask(question) {
  const context = state.current;
  if (!context || context.loading || context.asking || !question) return;
  const history = context.turns.map(({ question, answer }) => ({ question, answer }));
  context.messages.push({ text: question, me: true });
  context.asking = true;
  clearChatInput();
  renderChat(context);
  let draftToOpen;
  try {
    const answer = await api("/api/chat", { method: "POST", body: { ...context.pair, question, history } });
    context.messages.push({ text: answer.answer, evidence: answer.evidence });
    context.turns = [...context.turns, {
      question, answer: answer.answer.trim().slice(0, 2000),
      evidence: answer.evidence.map(({ source }) => ({ source })),
    }].slice(-6);
    try { localStorage.setItem(chatKey(context.pair), JSON.stringify(context.turns)); } catch { /* Storage may be disabled. */ }
    if (answer.diagnostic_result) {
      context.result = answer.diagnostic_result;
      context.diagnoseError = "";
    }
    if (answer.draft) {
      mergeDraft(context, answer.draft);
      if (["draft", "delivery_failed"].includes(answer.draft.state)) draftToOpen = answer.draft;
    }
  } catch (error) {
    context.messages.push({ text: error.message, error: true });
    if (state.current === context) get("chat-input").value = question;
  } finally {
    context.asking = false;
    if (state.current === context) {
      render();
      scheduleDraftPoll(context);
      if (draftToOpen && !document.querySelector("dialog[open]")) openDraft(context, draftToOpen);
    }
  }
}

function currentDraft(context) {
  if (!context) return null;
  const code = context.result?.diagnosis.anomaly_type;
  return code ? context.drafts.find((draft) => draft.anomaly_type === code) : context.drafts.at(-1);
}

function mergeDraft(context, draft) {
  const index = context.drafts.findIndex((item) => item.draft_id === draft.draft_id);
  if (index === -1) context.drafts.push(draft);
  else context.drafts[index] = draft;
}

function renderTicket(context) {
  const target = get("ticket-body");
  target.replaceChildren();
  if (context?.ticketError) {
    const note = node("p", "notice notice-error", context.ticketError);
    note.setAttribute("role", "alert");
    target.append(note);
  }
  if (!context || context.loading) {
    target.append(node("p", "", context?.loading ? "Загружаем состояние заявки…" : "Выберите сессию проверки."));
    return;
  }
  const draft = currentDraft(context);
  const diagnostic = context.result?.diagnosis;
  const canCreate = diagnostic?.status === "anomaly";
  if (!draft) {
    target.append(node("p", "", canCreate ? "Соберите работы и источники в черновик для согласования."
      : "Черновик доступен после подтверждённого результата с отклонением."));
    if (canCreate) {
      const create = button(context.ticketBusy ? "Готовим черновик…" : "Создать черновик", "light",
        () => newDraft(context), "plus");
      create.disabled = context.ticketBusy;
      target.append(create);
    }
  } else {
    const [label, tone] = ticketStates[draft.state];
    const wrap = node("div", "ticket-state");
    wrap.append(statusPill(node("span"), label, tone));
    if (draft.ticket_id) wrap.append(node("strong", "ticket-number", draft.ticket_id));
    target.append(wrap);
    if (draft.urgency) target.append(node("p", "", "Приоритет: " + urgencyLabels[draft.urgency]));
    if (draft.conclusion) target.append(node("p", "", draft.conclusion));

    if (draft.delivery_error) {
      target.append(node("p", "notice " + (draft.state === "delivery_unknown" ? "warning" : "notice-error"),
        draft.delivery_error.message));
    }
    if (draft.state === "delivery_unknown") {
      target.append(reconciliationButton(context, draft));
    }
    if (draft.state === "sending") {
      const refresh = button("Проверить статус", "light", () => refreshDraft(context, draft.draft_id));
      refresh.disabled = context.ticketBusy;
      target.append(refresh);
    }
    if (canCreate && ["draft", "delivery_failed"].includes(draft.state)) {
      const edit = button("Открыть черновик", "light", () => openDraft(context, draft), "file");
      edit.disabled = context.ticketBusy;
      target.append(edit);
    }
    if (!canCreate && ["draft", "delivery_failed"].includes(draft.state)) {
      target.append(node("p", "", "Для отправки нужен результат диагностики с тем же отклонением."));
    }
  }
  const others = context.drafts.filter((item) => item.draft_id !== draft?.draft_id);
  if (others.length) {
    const details = node("details", "ticket-details");
    details.append(node("summary", "", "Другие заявки этой сессии"));
    for (const item of others) {
      details.append(node("p", "", [item.anomaly_type, ticketStates[item.state][0], item.ticket_id].filter(Boolean).join(" · ")));
      if (item.state === "delivery_unknown") details.append(reconciliationButton(context, item));
    }
    target.append(details);
  }
}

function reconciliationButton(context, draft) {
  const control = button("Внести номер из ТОиР", "light", () => {
    get("reconcile-form").reset();
    get("reconcile-error").hidden = true;
    get("reconcile-context").textContent = [draft.vehicle_id, draft.session_id, draft.anomaly_type].join(" · ");
    state.reconciliation = { context, draft };
    get("reconcile-dialog").showModal();
  }, "file");
  control.disabled = context.ticketBusy;
  return control;
}

async function reconcileDraft() {
  const { context, draft } = state.reconciliation;
  if (context.ticketBusy) return;
  const fields = get("reconcile-form").querySelectorAll("input, button");
  const note = get("reconcile-error");
  context.ticketBusy = true;
  note.hidden = true;
  fields.forEach((field) => { field.disabled = true; });
  try {
    const saved = await api("/api/drafts/" + encodeURIComponent(draft.draft_id) + "/reconcile", {
      method: "POST", body: {
        revision: draft.revision, ticket_id: get("reconcile-number").value.trim(),
        verified: get("reconcile-verified").checked,
      },
    });
    mergeDraft(context, saved);
    context.ticketError = "";
    get("reconcile-dialog").close();
  } catch (error) {
    if (error.draft) {
      mergeDraft(context, error.draft);
      state.reconciliation.draft = error.draft;
    }
    note.textContent = error.message;
    note.hidden = false;
  } finally {
    context.ticketBusy = false;
    fields.forEach((field) => { field.disabled = false; });
    if (state.current === context) renderTicket(context);
  }
}

async function newDraft(context) {
  if (context.ticketBusy) return;
  context.ticketBusy = true;
  context.ticketError = "";
  renderTicket(context);
  let draft;
  try {
    draft = await api("/api/drafts", { method: "POST", body: context.pair });
    mergeDraft(context, draft);
  } catch (error) {
    context.ticketError = error.message;
  } finally {
    context.ticketBusy = false;
    if (state.current === context) renderTicket(context);
  }
  if (draft && state.current === context && ["draft", "delivery_failed"].includes(draft.state)) openDraft(context, draft);
}

async function refreshDraft(context, id) {
  if (context.ticketBusy) return;
  context.ticketBusy = true;
  try {
    mergeDraft(context, await api("/api/drafts/" + encodeURIComponent(id)));
    context.ticketError = "";
  } catch (error) {
    context.ticketError = error.message;
  } finally {
    context.ticketBusy = false;
    if (state.current === context) renderTicket(context);
    if (!context.ticketError) scheduleDraftPoll(context);
  }
}

function scheduleDraftPoll(context) {
  const draft = currentDraft(context);
  if (context.poll || state.current !== context || draft?.state !== "sending") return;
  context.poll = setTimeout(() => {
    context.poll = null;
    if (state.current === context) refreshDraft(context, draft.draft_id);
  }, 1600);
}

function normalizeSearch(text) {
  const equivalent = { "а":"a", "в":"b", "е":"e", "к":"k", "м":"m", "н":"h", "о":"o", "р":"p", "с":"c", "т":"t", "у":"y", "х":"x" };
  return text.toLowerCase().replace(/[авекмнорстух]/g, (letter) => equivalent[letter]).replace(/\s+/g, "");
}

function showFleet() {
  const query = normalizeSearch(get("fleet-search").value);
  const target = get("fleet-list");
  target.replaceChildren();
  for (const vehicle of state.vehicles) {
    const selection = vehicle.vehicle_id + " " + vehicleName(vehicle) + " " + vehicle.engine_model;
    if (!normalizeSearch(selection).includes(query)) continue;
    const row = node("button", "dialog-row");
    row.type = "button";
    const title = node("span");
    title.append(node("strong", "", vehicle.vehicle_id), node("small", "", vehicleName(vehicle) + " · " + vehicle.engine_model));
    row.append(title, icon("chevron"));
    row.addEventListener("click", () => {
      get("fleet-dialog").close();
      if (state.vehicle?.vehicle_id !== vehicle.vehicle_id) selectVehicle(vehicle.vehicle_id);
    });
    target.append(row);
  }
  if (!target.children.length) target.append(node("p", "", "Транспорт не найден."));
}

function loadManuals() {
  if (!state.library) {
    state.library = api("/api/manuals").catch((error) => { state.library = null; throw error; });
  }
  return state.library;
}

async function showManuals() {
  const dialog = get("manuals-dialog");
  const target = get("manuals-list");
  const engine = state.vehicle?.engine_model;
  get("manuals-caption").textContent = engine ? "Документы для двигателя " + engine : "Все документы";
  target.replaceChildren(node("p", "", "Загружаем список…"));
  dialog.showModal();
  try {
    const library = await loadManuals();
    target.replaceChildren();
    for (const manual of library) {
      if (engine && !manual.applicable_engine_models.includes(engine)) continue;
      const url = safeManualUrl(manual.url);
      const item = node(url ? "a" : "div", "dialog-row");
      if (url) { item.href = url; item.target = "_blank"; item.rel = "noopener"; }
      const text = node("span");
      text.append(node("strong", "", manual.manual), node("small", "", url
        ? "PDF · Страниц: " + manual.page_count : "Оригинал PDF недоступен"));
      item.append(text, icon(url ? "open" : "book"));
      target.append(item);
    }
    if (!target.children.length) target.append(node("p", "", "Для этого двигателя нет документов."));
  } catch (error) {
    target.replaceChildren(node("p", "notice notice-error", error.message));
  }
}

function showDraftError(message) {
  get("draft-error").textContent = message;
  get("draft-error").hidden = !message;
}

function setEditorControls(editor) {
  for (const item of get("draft-form").querySelectorAll("textarea, select, input, button")) {
    item.disabled = editor.busy || editor.stale;
  }
}

function addReference(editor, reference = {}) {
  const row = node("div", "reference-row");
  const manualField = node("label", "field select-field");
  const select = node("select");
  select.append(new Option("Руководство", ""));
  const options = new Set(editor.manuals.map((manual) => manual.manual));
  if (reference.manual) options.add(reference.manual);
  for (const manual of options) select.append(new Option(manual, manual));
  select.value = reference.manual || "";
  manualField.append(node("span", "", "Документ"), select, icon("down"));

  const pageField = node("label", "field");
  const page = node("input");
  page.type = "number";
  page.min = "1";
  page.step = "1";
  page.value = reference.pdf_page ?? "";
  function limitPage() {
    page.max = editor.manuals.find((manual) => manual.manual === select.value)?.page_count || "";
  }
  limitPage();
  select.addEventListener("change", limitPage);
  pageField.append(node("span", "", "Стр. PDF"), page);
  const remove = node("button", "icon-button");
  remove.type = "button";
  remove.setAttribute("aria-label", "Убрать источник");
  remove.append(icon("close"));
  remove.addEventListener("click", () => row.remove());
  row.append(manualField, pageField, remove);
  get("draft-references").append(row);
}

function openDraft(context, draft) {
  const dialog = get("draft-dialog");
  const editor = { context, draft, manuals: [], busy: false, stale: false };
  state.editor = editor;
  get("draft-context").textContent = [draft.vehicle_id, draft.session_id, context.engine, draft.anomaly_type].join(" · ");
  get("draft-conclusion").value = draft.conclusion ?? "";
  get("draft-operations").value = draft.operations.join("\n");
  get("draft-parts").value = draft.spare_parts.join("\n");
  get("draft-urgency").value = draft.urgency ?? "";
  showDraftError("");
  get("draft-references").replaceChildren();
  for (const reference of draft.references) addReference(editor, reference);
  const assessment = context.result?.assessment;
  get("draft-fill").hidden = !assessment ||
    !((!draft.operations.length && assessment.checks.length) ||
      (!draft.references.length && assessment.evidence.length));
  dialog.showModal();
  setEditorControls(editor);
  // Refresh document choices without overwriting pages or text currently being edited.
  loadManuals().then((library) => {
    if (state.editor !== editor) return;
    editor.manuals = library.filter((manual) => manual.applicable_engine_models.includes(context.engine));
    for (const row of get("draft-references").children) {
      const select = row.querySelector("select");
      const existing = new Set(Array.from(select.options, (option) => option.value));
      for (const manual of editor.manuals) {
        if (!existing.has(manual.manual)) select.append(new Option(manual.manual, manual.manual));
      }
      select.dispatchEvent(new Event("change"));
    }
  }).catch(() => {
    if (state.editor === editor) showDraftError("Список руководств недоступен. Уже указанные источники сохранены в форме.");
  });
}

function collectDraft() {
  const lines = (value) => value.split("\n").map((part) => part.trim()).filter(Boolean);
  const references = [];
  for (const row of get("draft-references").children) {
    const manual = row.querySelector("select").value;
    const input = row.querySelector("input");
    if (!manual && !input.value) continue;
    const pdfPage = input.valueAsNumber;
    if (!manual || !Number.isInteger(pdfPage) || !input.validity.valid) {
      throw new Error("Для каждого источника выберите документ и укажите существующую страницу PDF.");
    }
    references.push({ manual, pdf_page: pdfPage });
  }
  return {
    conclusion: get("draft-conclusion").value.trim() || null,
    operations: lines(get("draft-operations").value),
    spare_parts: lines(get("draft-parts").value),
    urgency: get("draft-urgency").value || null,
    references,
  };
}

async function saveDraft(send) {
  const editor = state.editor;
  if (!editor || editor.busy || editor.stale) return;
  const { context } = editor;
  let fields;
  try {
    fields = collectDraft();
    if (send && !(fields.conclusion && fields.operations.length && fields.urgency && fields.references.length)) {
      throw new Error("Для отправки нужны заключение, хотя бы одна работа, приоритет и источник.");
    }
  } catch (error) {
    showDraftError(error.message);
    return;
  }
  showDraftError("");
  editor.busy = true;
  context.ticketBusy = true;
  setEditorControls(editor);
  const path = "/api/drafts/" + encodeURIComponent(editor.draft.draft_id);
  try {
    const changed = Object.keys(fields).some((key) => JSON.stringify(fields[key]) !== JSON.stringify(editor.draft[key]));
    if (changed) {
      editor.draft = await api(path, { method: "PATCH", body: { revision: editor.draft.revision, ...fields } });
      mergeDraft(context, editor.draft);
    }
    if (send) {
      editor.draft = await api(path + "/confirm", {
        method: "POST", body: { revision: editor.draft.revision, urgency: fields.urgency, confirmed: true },
      });
      mergeDraft(context, editor.draft);
    }
    context.ticketError = "";
    get("draft-dialog").close();
  } catch (error) {
    if (error.draft) {
      mergeDraft(context, error.draft);
      if (error.draft.state !== "draft" && error.code !== "stale_draft") {
        get("draft-dialog").close();
      }
    }
    if (error.code === "stale_draft") {
      editor.stale = true;
      showDraftError(error.message + " Закройте форму и откройте обновлённый черновик.");
    } else {
      showDraftError(error.message);
    }
    if (!get("draft-dialog").open && !error.draft?.delivery_error) context.ticketError = error.message;
  } finally {
    editor.busy = false;
    context.ticketBusy = false;
    if (state.editor === editor) setEditorControls(editor);
    if (state.current === context) renderTicket(context);
    scheduleDraftPoll(context);
  }
}

get("vehicle-select").addEventListener("change", (event) => selectVehicle(event.target.value));
get("session-select").addEventListener("change", (event) => selectSession(event.target.value));
get("diagnose-button").addEventListener("click", diagnose);
get("chat-input").addEventListener("input", updateChatControls);
get("chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    get("chat-form").requestSubmit();
  }
});
get("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  ask(get("chat-input").value.trim());
});
get("fleet-open").addEventListener("click", () => {
  get("fleet-search").value = "";
  showFleet();
  get("fleet-dialog").showModal();
  get("fleet-search").focus();
});
get("fleet-search").addEventListener("input", showFleet);
get("manuals-open").addEventListener("click", showManuals);
get("draft-add-reference").addEventListener("click", () => {
  if (state.editor) addReference(state.editor);
});
get("draft-fill").addEventListener("click", () => {
  const editor = state.editor;
  const assessment = editor?.context.result?.assessment;
  if (!assessment) return;
  const operations = get("draft-operations");
  if (!operations.value.trim()) operations.value = assessment.checks.join("\n");
  const references = get("draft-references");
  if (!Array.from(references.querySelectorAll("select, input")).some((field) => field.value)) {
    references.replaceChildren();
    const seen = new Set();
    for (const { source } of assessment.evidence) {
      const key = JSON.stringify([source.manual, source.pdf_page]);
      if (seen.has(key)) continue;
      seen.add(key);
      addReference(editor, source);
    }
  }
  get("draft-fill").hidden = true;
});
get("draft-save").addEventListener("click", () => saveDraft(false));
get("draft-form").addEventListener("submit", (event) => {
  event.preventDefault();
  saveDraft(true);
});
get("draft-dialog").addEventListener("cancel", (event) => {
  if (state.editor?.busy) event.preventDefault();
});
get("draft-dialog").addEventListener("close", () => { state.editor = null; });
get("reconcile-form").addEventListener("submit", (event) => {
  event.preventDefault();
  reconcileDraft();
});
get("reconcile-dialog").addEventListener("cancel", (event) => {
  if (state.reconciliation?.context.ticketBusy) event.preventDefault();
});
get("reconcile-dialog").addEventListener("close", () => { state.reconciliation = null; });
for (const control of document.querySelectorAll(".close-dialog")) {
  control.type = "button";
  control.addEventListener("click", () => {
    const dialog = control.closest("dialog");
    if ((dialog.id === "draft-dialog" && state.editor?.busy) ||
        (dialog.id === "reconcile-dialog" && state.reconciliation?.context.ticketBusy)) return;
    dialog.close();
  });
}
render();
start();
