import { rememberSourceOrigin, restoreSourceOrigin } from "./source-return.js";

const byId = (id) => document.getElementById(id);
const ui = Object.fromEntries([
  "mode", "catalog-size", "vehicle", "vehicle-facts", "session",
  "vehicle-popup", "vehicle-options", "vehicle-empty",
  "context", "workarea", "context-summary", "context-title", "context-vehicle", "context-session",
  "edit-context", "collapse-context", "journal-panel", "ask-question",
  "selection", "selection-error", "retry-catalog", "diagnose", "diagnostic", "result",
  "chat-context", "messages", "chat-error", "composer", "question", "send", "suggestions",
  "ticket-panel", "journal", "announcement",
].map((id) => [id, byId(id)]));

const view = {
  catalog: null, pair: null, state: null, generation: 0, busy: null,
  diagnosisError: null, chatError: null, ticketError: null, needsTicketCheck: false,
  pendingQuestion: "", pendingEdits: new Map(), pollTimer: null, editingContext: false,
};
const compactLayout = matchMedia("(max-width: 820px)");
const vehiclePicker = { selected: null, matches: [], activeIndex: -1 };
const plateLetters = { а: "a", в: "b", е: "e", к: "k", м: "m", н: "h", о: "o",
  р: "p", с: "c", т: "t", у: "y", х: "x" };
const sectionNames = {
  signs: "Признаки", causes: "Возможные причины", checks: "Проверки",
  work: "Возможные работы", clarify: "Что уточнить", risk: "Риски",
};
const urgencyNames = { low: "Низкий", medium: "Средний", high: "Высокий" };
const riskLabels = {
  low: ["Низкий риск", "good"], medium: ["Средний риск", "warn"],
  high: ["Высокий риск", "danger"], unassessed: ["Риск требует уточнения", "neutral"],
};
const qualityNames = { good: "Хорошее", warning: "С помехами", bad: "Непригодна" };
const callStatuses = {
  ok: "Успешно", error: "Ошибка", timeout: "Тайм-аут", unknown: "Результат неизвестен", rejected: "Отклонено",
};
const outcomes = {
  normal: "Значимых отклонений не обнаружено",
  anomaly: "Обнаружено отклонение",
  check_failed: "Сбой проверки записи",
};
const date = new Intl.DateTimeFormat("ru-RU", {
  dateStyle: "short", timeStyle: "short", timeZone: "Europe/Moscow",
});
const score = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 3 });

class ApiFault extends Error {
  constructor(code, message, status = 0, state = null) {
    super(message);
    this.code = code;
    this.status = status;
    this.state = state;
  }
}

async function api(path, method = "GET", payload) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 65000);
  try {
    const response = await fetch("/api/" + path, {
      method, cache: "no-store", signal: controller.signal,
      headers: payload ? { "Content-Type": "application/json" } : {},
      body: payload ? JSON.stringify(payload) : undefined,
    });
    const data = await response.json().catch(() => null);
    if (!response.ok || !data) {
      throw new ApiFault(data?.error?.code || "server_error",
        data?.error?.message || "Бэкенд вернул ошибку. Проверьте состояние сервиса.", response.status, data?.state);
    }
    return data;
  } catch (error) {
    if (error instanceof ApiFault) throw error;
    if (controller.signal.aborted) throw new ApiFault("client_timeout", "Сервер не ответил вовремя.");
    throw new ApiFault("offline", "Связь с бэкендом недоступна. Проверьте подключение.");
  } finally {
    clearTimeout(timeout);
  }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function option(value, label) {
  return new Option(label, value);
}

function setAlert(node, message) {
  node.textContent = message || "";
  node.hidden = !message;
}

function alert(text, variant = "") {
  return element("p", "alert " + variant, text);
}

function progress(text) {
  const node = element("p", "progress");
  node.setAttribute("role", "status");
  const spinner = element("span", "spinner");
  spinner.setAttribute("aria-hidden", "true");
  node.append(spinner, document.createTextNode(text));
  return node;
}

function button(text, variant = "secondary", type = "button") {
  const node = element("button", "button " + variant, text);
  node.type = type;
  return node;
}

function showHeading(node, text) {
  node.replaceChildren(element("h2", "", text));
  node.firstElementChild.id = "result-title";
}

function setControls() {
  const editingVehicle = ui.vehicle.value !== selectedVehicleId();
  ui.session.disabled = !vehiclePicker.selected || editingVehicle;
  ui.diagnose.disabled = !view.pair || Boolean(view.busy) || editingVehicle;
  ui.diagnose.textContent = view.busy === "diagnose" ? "Получаем результат…" : "Получить результат диагностики";
  ui.question.disabled = !view.state?.diagnosis || Boolean(view.busy);
  ui["ask-question"].hidden = !view.state?.diagnosis;
  ui["ask-question"].disabled = ui.question.disabled;
  ui.send.disabled = ui.question.disabled;
  ui.send.textContent = view.busy === "chat" ? "Ждём ответ…" : "Спросить";
}

function formatDate(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : date.format(parsed);
}

const manualPageTopics = {
  "yamz236-re": { 19: "нормы давления", 53: "причины падения" },
};

function references(items) {
  const list = element("div", "refs");
  const byManual = new Map();
  for (const reference of items || []) {
    const manual = view.catalog?.manuals?.find((doc) => [doc.manual, doc.filename].includes(reference.manual));
    const key = manual || reference.manual;
    if (!byManual.has(key)) {
      byManual.set(key, { manual, title: manual?.label || reference.manual, pages: new Set() });
    }
    byManual.get(key).pages.add(Number(reference.pdf_page));
  }

  for (const { manual, title, pages } of byManual.values()) {
    const group = element("div", "ref-manual");
    const links = element("div", "ref-pages");
    group.append(element("span", "ref-title", title), links);
    for (const page of [...pages].sort((a, b) => a - b)) {
      const topic = manualPageTopics[manual?.slug]?.[page];
      const label = "стр. " + page + (topic ? " — " + topic : "");
      const link = element(manual ? "a" : "span", manual ? "ref" : "ref-unavailable", label);
      if (manual) {
        link.href = "/sources/" + encodeURIComponent(manual.slug) + "/" + page;
        link.setAttribute("aria-label", label + " · " + title);
      }
      links.append(link);
    }
    list.append(group);
  }
  return list;
}

function renderDiagnosis() {
  const result = ui.result;
  const diagnosis = view.state?.diagnosis;
  ui.diagnostic.dataset.status = diagnosis?.status || "";
  if (view.busy === "diagnose" || view.busy === "load") {
    showHeading(result, view.busy === "diagnose" ? "Запрашиваем диагностику" : "Открываем сессию");
    result.append(progress(view.busy === "diagnose" ? "Ожидаем ответ сервиса" : "Восстанавливаем диалог и заявку"));
    return;
  }
  if (view.diagnosisError) {
    const titles = {
      invalid_pair: "Проверьте пару ТС и сессии", unknown_vehicle: "Транспорт не найден",
      ml_timeout: "Тайм-аут диагностики", client_timeout: "Тайм-аут запроса", team_timeout: "Тайм-аут бэкенда",
      ml_unavailable: "Сервис диагностики недоступен", ml_invalid_response: "Некорректный ответ диагностики",
      team_unavailable: "Бэкенд команды недоступен", offline: "Связь потеряна",
    };
    ui.diagnostic.dataset.status = "error";
    showHeading(result, titles[view.diagnosisError.code] || "Запрос завершился с ошибкой");
    result.append(alert(view.diagnosisError.message, "error"));
    if (!diagnosis) return;
    result.append(element("p", "section-label", "Последний сохранённый результат"));
  } else if (!diagnosis) {
    showHeading(result, view.pair ? "Диагностика ещё не запрошена" : "Выберите транспорт и сессию");
    if (view.pair) result.append(element("p", "context-key", view.pair.vehicle_id + " / " + view.pair.session_id));
    return;
  }

  const heading = element(view.diagnosisError ? "h3" : "h2", "", outcomes[diagnosis.status]);
  if (!view.diagnosisError) {
    heading.id = "result-title";
    result.replaceChildren(heading);
  } else {
    result.append(heading);
  }
  const meta = element("div", "result-meta");
  meta.append(element("span", "context-key",
    diagnosis.vehicle_id + " / " + diagnosis.session_id + " · " + diagnosis.model_version));
  result.append(meta);
  if (diagnosis.anomaly_type) {
    result.append(element("p", "diagnosis-type",
      (diagnosis.anomaly_title || view.catalog?.fault_types?.[diagnosis.anomaly_type] || "Возможное отклонение")
      + " · " + diagnosis.anomaly_type));
  }
  result.append(element("p", "diagnosis-description", diagnosis.description));
  const brief = view.state.brief;
  const next = element("div", "first-check");
  next.append(element("h3", "", "Первое действие"));
  const defaultCheck = {
    normal: "Продолжайте штатный контроль двигателя.",
    check_failed: "Повторите запись. По этой записи вывод о двигателе невозможен.",
    anomaly: "Уточните симптомы и фактические измерения для дефектовки.",
  };
  next.append(element("p", "action", brief?.first_check || defaultCheck[diagnosis.status]));
  if (brief?.references?.length) next.append(references(brief.references));
  result.append(next);

  if (view.state.draft || (!view.diagnosisError && view.state.draft_available)) {
    const controls = element("div", "diagnosis-actions");
    const draftButton = button(view.state.draft ? "Открыть заявку"
      : view.busy === "prepare" ? "Готовим черновик…" : "Подготовить заявку");
    draftButton.id = "diagnosis-draft";
    draftButton.setAttribute("aria-controls", "ticket-panel");
    draftButton.disabled = Boolean(view.busy);
    draftButton.addEventListener("click", view.state.draft ? revealTicket : prepareDraft);
    controls.append(draftButton);
    if (!view.state.draft && view.ticketError) {
      const error = alert(view.ticketError, "error");
      error.setAttribute("role", "alert");
      controls.append(error);
    }
    result.append(controls);
  }

  if (brief && diagnosis.status === "anomaly") {
    const risk = element("div", "risk-line");
    const [label, theme] = riskLabels[brief.risk] || riskLabels.unassessed;
    risk.append(element("span", "flag " + theme, label), element("p", "", brief.rationale));
    result.append(risk);
    if (brief.origin === "fixture") result.append(element("p", "context-key", "Тестовая рекомендация"));
  }
  if (brief?.analysis?.possible_causes?.length) {
    const details = element("details", "answer-block");
    details.append(element("summary", "", "Возможные причины"));
    for (const item of brief.analysis.possible_causes) {
      details.append(element("p", "", item.text));
      details.append(references(brief.analysis.references.filter(ref => ref.fragment_id === item.fragment_id)));
    }
    result.append(details);
  }
  if (view.state.brief_error) result.append(alert(view.state.brief_error, "error"));

  const metrics = element("dl", "metrics");
  for (const [label, value] of [
    ["Уверенность · 0–1", diagnosis.confidence],
    ["Балл отклонения · 0–1", diagnosis.anomaly_score],
    ["Качество записи", qualityNames[diagnosis.signal_quality]],
  ]) {
    const cell = element("div", "metric");
    cell.append(element("dt", "", label), element("dd", "",
      value == null ? "Нет значения" : typeof value === "number" ? score.format(value) : value));
    metrics.append(cell);
  }
  result.append(metrics);
  const threshold = view.catalog.confidence_threshold;
  if (diagnosis.confidence != null && diagnosis.confidence < threshold) {
    result.append(alert("Низкая уверенность (" + score.format(diagnosis.confidence)
      + "). Порог предупреждения " + score.format(threshold) + ". Подтвердите результат контрольной проверкой."));
  }
  if (diagnosis.signal_quality === "warning") {
    result.append(alert("Запись с помехами. Повторите запись для проверки результата."));
  }
}

function answerBlock(section) {
  const expandable = section.kind === "work";
  const block = element(expandable ? "details" : "section", "answer-block");
  block.append(element(expandable ? "summary" : "h3", "", sectionNames[section.kind]));
  const list = element("ul");
  for (const item of section.items) list.append(element("li", "", item));
  block.append(list);
  if (section.references?.length) block.append(references(section.references));
  return block;
}

function userMessage(text) {
  const bubble = element("article", "bubble user");
  bubble.append(element("p", "message-meta", "Вы"), element("p", "", text));
  return bubble;
}

function diagnosisMessage(message) {
  const entry = element("article", "bubble diagnosis-message");
  entry.dataset.status = message.status;
  entry.append(element("p", "message-meta", "Результат диагностики"),
    element("p", "answer-title", message.summary),
    element("p", "", message.description),
    element("p", "diagnosis-next-step", message.next_step));
  if (message.origin === "fixture") entry.append(element("p", "context-key", "Тестовая рекомендация"));
  if (message.references?.length) entry.append(references(message.references));
  return entry;
}

function renderChat() {
  ui["chat-context"].textContent = view.state?.diagnosis ? view.pair.vehicle_id + " / " + view.pair.session_id
    : "Доступен после получения результата";
  const openDetails = [...ui.messages.querySelectorAll("details")].map((node) => node.open);
  const fragment = document.createDocumentFragment();
  for (const message of view.state?.messages || []) {
    if (message.role === "diagnosis") {
      fragment.append(diagnosisMessage(message));
      continue;
    }
    if (message.role === "user") {
      fragment.append(userMessage(message.text));
      continue;
    }
    const bubble = element("article", "bubble");
    bubble.append(element("p", "message-meta", message.origin === "fixture" ? "Механик · тестовый ответ" : "Механик"),
      element("p", "answer-title", message.summary));
    for (const section of message.sections || []) bubble.append(answerBlock(section));
    fragment.append(bubble);
  }
  if (view.pendingQuestion) fragment.append(userMessage(view.pendingQuestion));
  if (view.busy === "chat") fragment.append(progress("Подготавливаем ответ"));
  ui.messages.replaceChildren(fragment);
  [...ui.messages.querySelectorAll("details")].forEach((node, index) => { node.open = Boolean(openDetails[index]); });
  setAlert(ui["chat-error"], view.chatError);

  const suggestions = ui.suggestions;
  suggestions.replaceChildren();
  const diagnosis = view.state?.diagnosis;
  suggestions.hidden = !diagnosis;
  if (!diagnosis) return;
  const prompts = ["Что проверить сначала?", "Можно ли своим ходом перегнать машину?"];
  if (diagnosis.status === "anomaly") {
    if (diagnosis.anomaly_type === "F02") prompts.unshift("Когда замерять давление и почему оно падает?");
  }
  for (const prompt of prompts) {
    const chip = element("button", "", prompt);
    chip.type = "button";
    chip.disabled = Boolean(view.busy);
    chip.addEventListener("click", () => sendQuestion(prompt));
    suggestions.append(chip);
  }
}

function draftKey() {
  return JSON.stringify(view.pair);
}

function draftVersion() {
  return { draft_id: view.state.draft_id ?? undefined, revision: view.state.draft_revision ?? undefined };
}

function sameDraftVersion(left, right) {
  return left.draft_id === right.draft_id && left.revision === right.revision;
}

function field(id, label, control) {
  const container = element("div", "field");
  const title = element("label", "", label);
  title.htmlFor = id;
  control.id = id;
  container.append(title, control);
  return container;
}

function textarea(value, rows, required = false) {
  const input = element("textarea");
  input.value = value;
  input.rows = rows;
  input.maxLength = 4000;
  input.required = required;
  return input;
}

function listItems(value) {
  return value.split("\n").map((line) => line.trim()).filter(Boolean);
}

const draftEditFields = [
  { key: "conclusion", id: "conclusion", label: "Заключение", rows: 3 },
  { key: "urgency", id: "urgency", label: "Приоритет", rows: 1 },
  { key: "operations", id: "operations", label: "Работы", rows: 4 },
  { key: "spare_parts", id: "parts", label: "Запчасти", rows: 3 },
];

function draftFieldText(draft, key) {
  const value = draft[key];
  return Array.isArray(value) ? value.join("\n") : value ?? "";
}

function changedDraftFields(edits, saved) {
  return draftEditFields.filter(({ key }) => {
    const normalize = Array.isArray(saved[key]) ? (value) => listItems(value).join("\n") : (value) => value.trim();
    return normalize(draftFieldText(edits, key)) !== normalize(draftFieldText(saved, key));
  });
}

function setDraftSaveStatus(status, dirty) {
  const label = dirty ? "Есть несохранённые правки" : "Сохранено";
  if (status.textContent !== label) status.textContent = label;
  status.dataset.dirty = String(dirty);
}

function draftSaveStatus(dirty) {
  const status = element("p", "draft-save-status");
  status.id = "draft-save-status";
  status.setAttribute("role", "status");
  setDraftSaveStatus(status, dirty);
  return status;
}

function fieldSummary(draft) {
  const summary = element("dl", "ticket-review");
  for (const [label, value] of [
    ["Заключение", draft.conclusion], ["Приоритет", urgencyNames[draft.urgency]],
    ["Работы", draft.operations], ["Запчасти", draft.spare_parts], ["Источники", references(draft.references)],
  ]) {
    const row = element("div");
    const detail = element("dd");
    if (value instanceof Node) {
      detail.append(value);
    } else if (Array.isArray(value) && value.length) {
      const list = element("ul");
      for (const item of value) list.append(element("li", "", item));
      detail.append(list);
    } else {
      detail.textContent = Array.isArray(value) ? "Не определены" : value;
    }
    row.append(element("dt", "", label), detail);
    summary.append(row);
  }
  return summary;
}

function editableDraft(draft, pending) {
  const key = draftKey();
  const version = pending?.version ?? draftVersion();
  const edits = pending?.edits ?? draft;
  const form = element("form", "ticket-form");
  const conclusion = textarea(edits.conclusion, 3, true);
  const operations = textarea(edits.operations.join("\n"), 4, true);
  const parts = textarea(edits.spare_parts.join("\n"), 2);
  const priority = element("select");
  priority.required = true;
  priority.add(option("", "Выберите приоритет"));
  for (const [value, label] of Object.entries(urgencyNames)) priority.add(option(value, label));
  priority.value = edits.urgency || "";
  form.append(field("conclusion", "Заключение", conclusion), field("urgency", "Приоритет", priority),
    field("operations", "Работы, по одной на строку", operations), field("parts", "Запчасти, по одной на строку", parts));
  parts.placeholder = "Не определены";
  form.append(element("h3", "field-hint", "Источники"), references(draft.references));

  const confirm = element("label", "priority-confirm");
  const checkbox = element("input");
  checkbox.type = "checkbox";
  confirm.append(checkbox, document.createTextNode("Подтверждаю выбранный приоритет"));
  const actions = element("div", "ticket-actions");
  const status = draftSaveStatus(changedDraftFields(edits, draft).length > 0);
  const save = button(view.busy === "save" ? "Сохраняем…" : "Сохранить изменения");
  const submit = button("Подтвердить и отправить", "primary", "submit");
  save.disabled = Boolean(view.busy);
  submit.disabled = true;
  for (const control of [conclusion, operations, parts, priority, checkbox]) control.disabled = Boolean(view.busy);
  actions.append(status, submit, save);
  form.append(confirm, actions);

  function values(trim) {
    const lines = trim ? listItems : (text) => text.split("\n");
    return { conclusion: trim ? conclusion.value.trim() : conclusion.value, urgency: priority.value,
      operations: lines(operations.value), spare_parts: lines(parts.value) };
  }

  for (const control of [conclusion, operations, parts, priority]) {
    control.addEventListener("input", () => {
      const edits = values(false);
      const dirty = changedDraftFields(edits, draft).length > 0;
      if (dirty) view.pendingEdits.set(key, { edits, version });
      else view.pendingEdits.delete(key);
      setDraftSaveStatus(status, dirty);
      checkbox.checked = false;
      submit.disabled = true;
    });
  }
  checkbox.addEventListener("change", () => { submit.disabled = !checkbox.checked; });
  save.addEventListener("click", () => {
    if (form.reportValidity()) updateDraft(values(true), version, false);
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (checkbox.checked) updateDraft(values(true), version, true);
  });
  return form;
}

function renderDraftConflict(panel, draft, pending) {
  const changes = changedDraftFields(pending.edits, draft);
  const description = changes.length
    ? "Скопируйте нужные локальные правки перед загрузкой сохранённого черновика."
    : "Загрузите сохранённую версию.";
  const notice = alert("Черновик или его статус изменился. " + description, "info");
  notice.id = "draft-conflict";
  notice.setAttribute("role", "alert");
  const comparison = element("div", "draft-comparison");
  comparison.id = "draft-comparison";
  for (const { key, id, label, rows } of changes) {
    const format = (state) => key === "urgency" ? urgencyNames[state.urgency] || "" : draftFieldText(state, key);
    const group = element("fieldset", "draft-conflict-field");
    group.dataset.field = key;
    const local = textarea(format(pending.edits), rows);
    local.readOnly = true;
    local.placeholder = "Пусто";
    const saved = element("div", "field");
    saved.append(element("p", "draft-conflict-label", "Сохранённый вариант"),
      element("p", "draft-conflict-saved", format(draft) || "Пусто"));
    const versions = element("div", "draft-conflict-versions");
    versions.append(field(id, "Ваш вариант", local), saved);
    group.append(element("legend", "", label), versions);
    comparison.append(group);
  }
  const load = button("Загрузить сохранённый черновик");
  load.id = "use-saved-draft";
  load.addEventListener("click", () => {
    view.pendingEdits.delete(draftKey());
    view.ticketError = null;
    renderTicket();
    (byId("conclusion") || byId("ticket-title"))?.focus();
  });
  const actions = element("div", "ticket-actions");
  actions.append(draftSaveStatus(changes.length > 0), load);
  panel.append(notice);
  if (changes.length) panel.append(comparison);
  panel.append(actions);
}

function reconcileForm() {
  const form = element("form", "reconcile-form");
  const ticketId = element("input", "ticket-number");
  ticketId.type = "text";
  ticketId.required = true;
  ticketId.pattern = "TKT-[0-9]{4,}";
  ticketId.autocomplete = "off";
  ticketId.maxLength = 40;
  ticketId.placeholder = "TKT-0001";
  ticketId.disabled = Boolean(view.busy);
  form.append(element("h3", "", "Сверка с ТОиР"), field("reconcile-ticket-id", "Номер заявки в ТОиР", ticketId));

  const confirmation = element("label", "priority-confirm");
  const verified = element("input");
  verified.type = "checkbox";
  verified.id = "reconcile-verified";
  verified.disabled = Boolean(view.busy);
  confirmation.append(verified, document.createTextNode("Номер сверил в ТОиР"));
  const submit = button(view.busy === "reconcile" ? "Сохраняем номер…" : "Сохранить номер", "primary", "submit");
  submit.disabled = true;
  verified.addEventListener("change", () => { submit.disabled = !verified.checked; });
  const actions = element("div", "ticket-actions");
  actions.append(submit);
  form.append(confirmation, actions);

  const version = draftVersion();
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (verified.checked) reconcileDraft(ticketId.value.trim(), version);
  });
  return form;
}

function copyThroughSelection(value) {
  const focused = document.activeElement;
  const selection = element("textarea", "clipboard-selection");
  selection.value = value;
  selection.readOnly = true;
  selection.tabIndex = -1;
  document.body.append(selection);
  try {
    selection.focus({ preventScroll: true });
    selection.select();
    selection.setSelectionRange(0, value.length);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    selection.remove();
    focused?.focus({ preventScroll: true });
  }
}

function ticketReceipt(ticketId) {
  const receipt = element("div", "ticket-receipt");
  const badge = element("strong", "ticket-badge", ticketId);
  const copy = button("Скопировать номер");
  copy.id = "copy-ticket-number";
  const feedback = element("span", "copy-ticket-status");
  feedback.id = "copy-ticket-status";
  feedback.setAttribute("role", "status");
  copy.addEventListener("click", async () => {
    feedback.textContent = "";
    let copied = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(ticketId);
        copied = true;
      }
    } catch {
      // Some browsers require copying a selection after denying clipboard access.
    }
    if (!copy.isConnected) return;
    if (!copied) copied = copyThroughSelection(ticketId);
    feedback.textContent = copied ? "Номер скопирован" : "Выделите номер и скопируйте вручную.";
  });
  receipt.append(badge, copy, feedback);
  return receipt;
}

function renderTicket() {
  const panel = ui["ticket-panel"];
  const draft = view.state?.draft;
  const ticket = view.state?.ticket;
  panel.hidden = !draft;
  if (!draft || !ticket) {
    panel.replaceChildren();
    return;
  }
  const title = element("h2", "", ticket.status === "created" ? "Заявка на дефектовку" : "Черновик дефектовки");
  title.id = "ticket-title";
  title.tabIndex = -1;
  const header = element("div", "ticket-head");
  header.append(title);
  if (ticket.ticket_id) header.append(ticketReceipt(ticket.ticket_id));
  panel.replaceChildren(header);
  if (view.state.draft_origin === "fixture") panel.append(element("p", "origin", "Тестовый черновик"));

  const ids = element("div", "ticket-ids");
  for (const text of ["ТС " + draft.vehicle_id, "Сессия " + draft.session_id, "Код ML " + draft.anomaly_type]) {
    ids.append(element("span", "", text));
  }
  panel.append(ids);

  if (view.ticketError) panel.append(alert(view.ticketError, "error"));
  if (ticket.status === "created") panel.append(alert("Заявка зарегистрирована в ТОиР.", "success"));
  if (ticket.message) panel.append(alert(ticket.message, ticket.status === "error" ? "error" : ""));
  const sending = ticket.status === "sending" || view.busy === "submit";
  if (sending) panel.append(progress("Отправляем заявку в ТОиР"));

  if (view.needsTicketCheck) {
    const check = button("Проверить статус");
    check.disabled = Boolean(view.busy);
    check.addEventListener("click", () => refreshTicket(true));
    const actions = element("div", "ticket-actions");
    actions.append(check);
    panel.append(actions);
  }

  const canEdit = !view.needsTicketCheck && !sending && ["draft", "error"].includes(ticket.status);
  const pending = view.pendingEdits.get(draftKey());
  const conflict = Boolean(pending && (!sameDraftVersion(pending.version, draftVersion()) ||
    ["sending", "created", "unknown"].includes(ticket.status)));
  if (conflict) renderDraftConflict(panel, draft, pending);
  else panel.append(canEdit ? editableDraft(draft, pending) : fieldSummary(draft));
  if (!conflict && !view.needsTicketCheck && ticket.status === "unknown" && view.state.reconcile_available) {
    panel.append(reconcileForm());
  }
}

function renderJournal() {
  const calls = view.state?.calls;
  if (!calls?.length) {
    ui.journal.replaceChildren(element("p", "muted", view.pair ? "Вызовов пока нет."
      : "Выберите сессию, чтобы увидеть её вызовы."));
    return;
  }
  const list = element("ol", "calls");
  for (const call of calls) {
    const row = element("li");
    const parts = [callStatuses[call.status] || call.status, call.duration_ms + " мс"];
    if (call.model) parts.push(call.model);
    if (call.pages?.length) parts.push(call.pages.map(([manual, page]) => manual + ", стр. " + page).join(" · "));
    if (call.input_tokens != null || call.output_tokens != null) {
      parts.push("вход " + (call.input_tokens ?? "—") + " / выход " + (call.output_tokens ?? "—"));
    }
    row.append(element("strong", "", call.tool), element("span", "", parts.join(" · ")));
    list.append(row);
  }
  ui.journal.replaceChildren(list);
}

function renderContext() {
  const collapsible = compactLayout.matches && view.pair && view.state?.diagnosis && !view.diagnosisError;
  const collapsed = Boolean(collapsible && !view.editingContext);
  if (collapsed) {
    ui["context-vehicle"].textContent = view.pair.vehicle_id;
    ui["context-session"].textContent = vehiclePicker.selected.engine_model + " · " + view.pair.session_id;
  }
  ui["context-summary"].hidden = !collapsed;
  ui.selection.hidden = collapsed;
  ui["collapse-context"].hidden = !collapsible || collapsed;
}

function updateLayout() {
  const destination = compactLayout.matches ? ui.workarea : ui.context;
  const journal = ui["journal-panel"];
  if (journal.parentElement !== destination) {
    const focused = document.activeElement;
    destination.append(journal);
    if (journal.contains(focused)) focused.focus({ preventScroll: true });
  }
  renderContext();
}

function render() {
  setControls();
  renderContext();
  renderDiagnosis();
  renderChat();
  renderTicket();
  renderJournal();
}

function stateRoute() {
  return "state?" + new URLSearchParams(view.pair);
}

function announce(text) {
  ui.announcement.textContent = text;
}

function selectedVehicleId() {
  return vehiclePicker.selected?.vehicle_id || "";
}

function searchText(value) {
  return value.toLowerCase().replace(/[\s.-]+/g, "");
}

function showVehicleChoices(text) {
  const query = searchText(text);
  const plate = query.replace(/[авекмнорстух]/g, (letter) => plateLetters[letter]);
  vehiclePicker.matches = view.catalog.vehicles.filter((vehicle) =>
    searchText(vehicle.vehicle_id).includes(plate) || searchText(vehicle.engine_model).includes(query));
  vehiclePicker.activeIndex = -1;
  ui.vehicle.removeAttribute("aria-activedescendant");
  ui["vehicle-options"].replaceChildren(...vehiclePicker.matches.map((vehicle, index) => {
    const row = element("li", "vehicle-option");
    row.id = "vehicle-option-" + index;
    row.dataset.index = index;
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", "false");
    row.append(element("strong", "", vehicle.vehicle_id), element("small", "", vehicle.engine_model));
    return row;
  }));
  ui["vehicle-empty"].hidden = Boolean(vehiclePicker.matches.length);
  ui["vehicle-popup"].hidden = false;
  ui.vehicle.setAttribute("aria-expanded", "true");
}

function openVehicleChoices() {
  const text = ui.vehicle.value;
  showVehicleChoices(text === selectedVehicleId() ? "" : text);
}

function restoreVehicleInput() {
  ui.vehicle.value = selectedVehicleId();
  ui["vehicle-popup"].hidden = true;
  ui.vehicle.setAttribute("aria-expanded", "false");
  ui.vehicle.removeAttribute("aria-activedescendant");
  setControls();
}

function selectVehicle(vehicle) {
  const vehicleId = vehicle?.vehicle_id || "";
  const changed = vehicleId !== selectedVehicleId();
  vehiclePicker.selected = vehicle;
  restoreVehicleInput();
  if (changed) {
    showSessions(vehicle);
    choose(vehicleId, "");
  }
}

function moveVehicleOption(direction) {
  if (ui["vehicle-popup"].hidden) openVehicleChoices();
  const options = ui["vehicle-options"].children;
  if (!options.length) return;
  const previous = vehiclePicker.activeIndex;
  const index = previous === -1 && direction < 0
    ? options.length - 1 : Math.max(0, Math.min(options.length - 1, previous + direction));
  options[previous]?.setAttribute("aria-selected", "false");
  const active = options[index];
  active.setAttribute("aria-selected", "true");
  vehiclePicker.activeIndex = index;
  ui.vehicle.setAttribute("aria-activedescendant", active.id);
  active.scrollIntoView({ block: "nearest" });
}

function vehicleKeydown(event) {
  if (event.isComposing) return;
  const isOpen = !ui["vehicle-popup"].hidden;
  switch (event.key) {
    case "ArrowDown":
    case "ArrowUp":
      event.preventDefault();
      moveVehicleOption(event.key === "ArrowDown" ? 1 : -1);
      break;
    case "Enter": {
      event.preventDefault();
      if (!isOpen) break;
      const { matches, activeIndex } = vehiclePicker;
      const vehicle = matches[activeIndex] || (matches.length === 1 ? matches[0] : null);
      if (vehicle) selectVehicle(vehicle);
      break;
    }
    case "Escape":
      if (isOpen) {
        event.preventDefault();
        restoreVehicleInput();
      }
      break;
  }
}

function showSessions(vehicle, selected = "") {
  const placeholder = vehicle ? "Выберите сессию" : "Сначала выберите ТС";
  ui.session.replaceChildren(option("", placeholder));
  ui["vehicle-facts"].hidden = !vehicle;
  if (!vehicle) return;
  ui["vehicle-facts"].replaceChildren(element("strong", "", vehicle.engine_model),
    document.createTextNode([vehicle.model || vehicle.make, vehicle.fuel].filter(Boolean).join(" · ")));
  for (const session of [...vehicle.sessions].reverse()) {
    ui.session.add(option(session.session_id, session.session_id + " · " + formatDate(session.checked_at)));
  }
  ui.session.value = selected;
}

function syncLocation(vehicle, session) {
  const url = new URL(location.href);
  for (const [key, value] of [["vehicle_id", vehicle], ["session_id", session]]) {
    if (value) url.searchParams.set(key, value);
    else url.searchParams.delete(key);
  }
  history.replaceState(null, "", url);
}

function faultForSelection(vehicle, sessionId, vehicleId) {
  if (vehicleId && !vehicle) return new ApiFault("unknown_vehicle", "Выберите ТС из каталога.");
  if (sessionId && !vehicle?.sessions.some((session) => session.session_id === sessionId)) {
    return new ApiFault("invalid_pair", "Сессия не принадлежит выбранному ТС. Выберите его сессию.");
  }
  return null;
}

async function choose(vehicleId, sessionId, restore = false) {
  const generation = ++view.generation;
  clearTimeout(view.pollTimer);
  ui.question.value = "";
  const vehicle = view.catalog.vehicles.find((item) => item.vehicle_id === vehicleId);
  const invalid = faultForSelection(vehicle, sessionId, vehicleId);
  if (!restore) syncLocation(vehicleId, sessionId);
  view.pair = !invalid && vehicle && sessionId ? { vehicle_id: vehicleId, session_id: sessionId } : null;
  Object.assign(view, { state: null, diagnosisError: invalid, chatError: null, ticketError: null,
    needsTicketCheck: false, pendingQuestion: "", busy: view.pair ? "load" : null, editingContext: false });
  setAlert(ui["selection-error"], invalid?.message);
  render();
  if (!view.pair) return;
  try {
    const state = await api(stateRoute());
    if (generation === view.generation) view.state = state;
  } catch (error) {
    if (generation === view.generation) view.diagnosisError = error;
  } finally {
    if (generation === view.generation) {
      view.busy = null;
      render();
      scheduleTicketCheck();
    }
  }
}

async function loadCatalog() {
  ui["retry-catalog"].hidden = true;
  ui["catalog-size"].textContent = "Каталог загружается…";
  setAlert(ui["selection-error"], null);
  try {
    view.catalog = await api("catalog");
    const params = new URLSearchParams(location.search);
    const vehicleId = params.get("vehicle_id") || "";
    const sessionId = params.get("session_id") || "";
    const vehicle = view.catalog.vehicles.find((item) => item.vehicle_id === vehicleId);
    ui["catalog-size"].textContent = view.catalog.vehicles.length + " ТС в каталоге";
    ui.mode.textContent = ({
      demo: "Синтетические данные · тестовый чат",
      hybrid: "Синтетические данные · агент команды",
      team: "Синтетические данные · стенд команды",
    })[view.catalog.mode] || "Синтетические данные";
    vehiclePicker.selected = vehicle || null;
    restoreVehicleInput();
    showSessions(vehicle, vehicle?.sessions.some((item) => item.session_id === sessionId) ? sessionId : "");
    ui.vehicle.disabled = false;
    const pendingDraft = history.state?.sourceOrigin?.pendingDraft;
    const key = JSON.stringify({ vehicle_id: vehicleId, session_id: sessionId });
    if (pendingDraft?.key === key) view.pendingEdits.set(key, pendingDraft.edits);
    await choose(vehicleId, sessionId, true);
    requestAnimationFrame(restoreSourceOrigin);
  } catch (error) {
    ui["catalog-size"].textContent = "Каталог недоступен";
    setAlert(ui["selection-error"], error.message);
    ui["retry-catalog"].hidden = false;
  }
}

function revealTicket() {
  const title = byId("ticket-title");
  if (!title) return;
  title.focus({ preventScroll: true });
  ui["ticket-panel"].scrollIntoView({ block: "start" });
}

async function prepareDraft() {
  const generation = view.generation;
  const state = await action("prepare", "draft", "POST");
  if (generation !== view.generation) return;
  if (state?.draft) revealTicket();
  else byId("diagnosis-draft")?.focus();
}

function scheduleTicketCheck() {
  clearTimeout(view.pollTimer);
  if (view.state?.ticket?.status === "sending" && !view.needsTicketCheck && !view.busy) {
    view.pollTimer = setTimeout(() => refreshTicket(false), 850);
  }
}

async function refreshTicket(explicit) {
  if (view.busy || !view.pair) return;
  const generation = view.generation;
  view.busy = "verify";
  if (explicit) renderTicket();
  try {
    const state = await api(stateRoute());
    if (generation !== view.generation) return;
    view.state = state;
    view.needsTicketCheck = false;
    view.ticketError = null;
  } catch (error) {
    if (generation !== view.generation) return;
    view.needsTicketCheck = true;
    view.ticketError = "Статус заявки пока недоступен. " + error.message;
  } finally {
    if (generation === view.generation) {
      view.busy = null;
      render();
      scheduleTicketCheck();
    }
  }
}

async function action(kind, path, method, payload = {}) {
  if (!view.pair || view.busy) return null;
  clearTimeout(view.pollTimer);
  const generation = view.generation;
  const key = draftKey();
  const pending = view.pendingEdits.get(key);
  const updatesDraft = kind === "save" || kind === "submit";
  view.busy = kind;
  if (kind === "diagnose") view.diagnosisError = null;
  if (kind === "chat") {
    view.chatError = null;
    view.pendingQuestion = payload.message;
  }
  if (["prepare", "save", "submit", "reconcile"].includes(kind)) view.ticketError = null;
  render();
  if (kind === "diagnose" && compactLayout.matches) {
    ui.diagnostic.scrollIntoView({ block: "start" });
  }
  try {
    const state = await api(path, method, { ...view.pair, ...payload });
    if (updatesDraft && view.pendingEdits.get(key) === pending) view.pendingEdits.delete(key);
    if (generation !== view.generation) return null;
    view.state = state;
    if (kind === "diagnose") view.editingContext = false;
    return state;
  } catch (error) {
    if (generation !== view.generation) return null;
    if (updatesDraft && error.code === "stale_draft") {
      if (error.state) {
        view.state = error.state;
      } else {
        view.needsTicketCheck = true;
        view.ticketError = error.message;
      }
      return null;
    }
    if (kind === "diagnose") view.diagnosisError = error;
    if (kind === "chat") view.chatError = error.message;
    if (kind === "prepare" || kind === "save") view.ticketError = error.message;
    if (kind === "reconcile") {
      if (error.code === "stale_draft" && error.state) view.state = error.state;
      view.ticketError = error.message;
    }
    if (kind === "submit") {
      view.needsTicketCheck = error.status === 0 || error.status >= 500 || error.status === 409;
      view.ticketError = view.needsTicketCheck
        ? "Результат отправки неизвестен. Проверьте статус заявки. " + error.message : error.message;
    }
    return null;
  } finally {
    if (generation === view.generation) {
      view.busy = null;
      view.pendingQuestion = "";
      render();
      if (kind === "diagnose" && compactLayout.matches) {
        ui.diagnostic.scrollIntoView({ block: "start" });
      }
      scheduleTicketCheck();
    }
  }
}

async function sendQuestion(value) {
  const message = value.trim();
  if (!message || !view.state?.diagnosis || view.busy) return;
  const generation = view.generation;
  const hadDraft = Boolean(view.state?.draft);
  const result = await action("chat", "chat", "POST", { message });
  if (!result || generation !== view.generation) return;
  ui.question.value = "";
  if (result.draft && (!hadDraft || /заявк/i.test(message))) revealTicket();
  else ui.messages.lastElementChild?.scrollIntoView({ block: "nearest" });
}

async function updateDraft(edits, version, submit) {
  const kind = submit ? "submit" : "save";
  const generation = view.generation;
  const key = draftKey();
  if (!view.pendingEdits.has(key)) view.pendingEdits.set(key, { edits, version });
  const result = await action(kind, submit ? "ticket" : "draft", submit ? "POST" : "PUT",
    submit ? { ...version, edits, priority_confirmed: true } : { ...version, edits });
  if (!result || generation !== view.generation) return;
  if (submit) {
    announce(result.ticket.ticket_id ? "Заявка " + result.ticket.ticket_id + " зарегистрирована."
      : result.ticket.message || "Статус заявки обновлён.");
    revealTicket();
  } else announce("Изменения сохранены.");
}

async function reconcileDraft(ticketId, version) {
  const generation = view.generation;
  const state = await action("reconcile", "drafts/" + encodeURIComponent(version.draft_id) + "/reconcile", "POST",
    { revision: version.revision, ticket_id: ticketId, verified: true });
  if (generation !== view.generation) return;
  if (state?.ticket?.ticket_id) {
    announce("Сохранён подтверждённый номер " + state.ticket.ticket_id + ".");
    revealTicket();
  } else {
    const field = byId("reconcile-ticket-id");
    if (field) {
      field.value = ticketId;
      field.focus();
    }
  }
}

ui.vehicle.addEventListener("focus", () => {
  ui.vehicle.select();
  openVehicleChoices();
});
ui.vehicle.addEventListener("click", () => {
  if (ui.vehicle.value === selectedVehicleId()) ui.vehicle.select();
  if (ui["vehicle-popup"].hidden) openVehicleChoices();
});
ui.vehicle.addEventListener("input", () => {
  showVehicleChoices(ui.vehicle.value);
  setControls();
});
ui.vehicle.addEventListener("keydown", vehicleKeydown);
ui.vehicle.addEventListener("blur", () => {
  if (ui.vehicle.value) restoreVehicleInput();
  else selectVehicle(null);
});
ui["vehicle-options"].addEventListener("mousedown", (event) => event.preventDefault());
ui["vehicle-options"].addEventListener("click", (event) => {
  const row = event.target.closest(".vehicle-option");
  if (row) selectVehicle(vehiclePicker.matches[Number(row.dataset.index)]);
});
ui.session.addEventListener("change", () => choose(selectedVehicleId(), ui.session.value));
ui["edit-context"].addEventListener("click", () => {
  view.editingContext = true;
  renderContext();
  ui["context-title"].focus({ preventScroll: true });
});
ui["collapse-context"].addEventListener("click", () => {
  view.editingContext = false;
  renderContext();
  ui["edit-context"].focus({ preventScroll: true });
});
ui["ask-question"].addEventListener("click", () => {
  ui.question.focus({ preventScroll: true });
  ui.question.scrollIntoView({ block: "center" });
});
ui.selection.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!ui.diagnose.disabled) action("diagnose", "diagnose", "POST");
});
ui.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  sendQuestion(ui.question.value);
});
ui.question.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  sendQuestion(ui.question.value);
});
ui["retry-catalog"].addEventListener("click", loadCatalog);

compactLayout.addEventListener("change", updateLayout);
document.addEventListener("click", (event) => {
  const key = draftKey();
  const edits = view.pendingEdits.get(key);
  rememberSourceOrigin(event, edits && { key, edits });
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) requestAnimationFrame(restoreSourceOrigin);
});
updateLayout();
loadCatalog();
