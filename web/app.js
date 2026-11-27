import { rememberSourceOrigin, restoreSourceOrigin } from "./source-return.js";

const byId = (id) => document.getElementById(id);
const ui = Object.fromEntries([
  "mode", "catalog-size", "vehicle", "vehicle-facts", "session",
  "vehicle-popup", "vehicle-options", "vehicle-empty",
  "workspace", "context", "workarea", "context-summary", "context-title", "context-vehicle", "context-session",
  "edit-context", "collapse-context", "journal-panel", "ask-question",
  "selection", "selection-hint", "selection-error", "retry-catalog", "diagnostic", "result",
  "chat", "chat-context", "messages", "chat-error", "composer", "question", "send", "suggestions",
  "pressure-helper", "pressure-form", "pressure-fields",
  "ticket-panel", "journal", "announcement", "fleet-toggle", "fleet-panel", "vehicle-history", "history-content",
  "recording-help", "quick-replies", "reply-options",
].map((id) => [id, byId(id)]));

const view = {
  catalog: null, pair: null, state: null, generation: 0, busy: null,
  diagnosisError: null, chatError: null, ticketError: null, needsTicketCheck: false,
  pendingQuestion: "", pendingEdits: new Map(), consents: null, pollTimer: null, editingContext: false,
  fleet: null, fleetError: null, fleetOpen: false, fleetLoading: false, fleetFilter: null,
  history: null, historyVehicle: null, historyError: null, historyRequest: 0,
};
const compactLayout = matchMedia("(max-width: 820px)");
const vehiclePicker = { selected: null, matches: [], activeIndex: -1 };
const plateLetters = { а: "a", в: "b", е: "e", к: "k", м: "m", н: "h", о: "o",
  р: "p", с: "c", т: "t", у: "y", х: "x" };
const sectionNames = {
  checks: "Проверки по руководству", clarify: "Ограничения и уточнения",
  risk: "Риски · гипотезы", causes: "Возможные причины · гипотезы",
  signs: "Наблюдения ML", work: "Возможные работы",
};
const urgencyNames = { low: "Низкий", medium: "Средний", high: "Высокий" };
const urgencyDescriptions = { high: "Дефектовка до выпуска в рейс", medium: "Ближайшее ТО с работой под наблюдением", low: "Проверка узла на следующей проверке" };
const riskLabels = {
  low: ["Низкий риск", "good"], medium: ["Средний риск", "warn"],
  high: ["Высокий риск", "danger"], unassessed: ["Риск требует уточнения", "neutral"],
};
const qualityNames = { good: "Хорошее", warning: "С помехами", bad: "Непригодна" };
const callStatuses = {
  ok: "Успешно", error: "Ошибка", timeout: "Тайм-аут", unknown: "Результат неизвестен", rejected: "Отклонено",
};
const outcomes = {
  normal: "Отклонений по записи не обнаружено",
  anomaly: "Обнаружено отклонение",
  check_failed: "Запись не удалось проверить",
};
const date = new Intl.DateTimeFormat("ru-RU", {
  dateStyle: "short", timeStyle: "short", timeZone: "Europe/Moscow",
});
const sessionDate = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Moscow",
});
const score = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 3 });

class ApiFault extends Error {
  constructor(code, message, status = 0, state = null, admission = null) {
    super(message);
    this.code = code;
    this.status = status;
    this.state = state;
    this.admission = admission;
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
        data?.error?.message || "Бэкенд вернул ошибку. Проверьте состояние сервиса.", response.status, data?.state, data?.admission);
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
  ui.question.disabled = !view.state?.diagnosis || Boolean(view.busy);
  ui["ask-question"].disabled = ui.question.disabled;
  ui["pressure-fields"].disabled = ui.question.disabled;
  setChatSubmit();
  ui.send.textContent = view.busy === "chat" ? "Ждём ответ…" : "Отправить";
}

function setChatSubmit() {
  ui.send.disabled = ui.question.disabled || !ui.question.value.trim();
}

function formatDate(value, formatter = date) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : formatter.format(parsed);
}

const manualPageTopics = {
  "yamz236-re": {
    4: "температура охлаждающей жидкости",
    19: "нормы давления",
    23: "ежедневное ТО",
    53: "причины падения",
    54: "перегрев и вода в масле",
  },
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
    group.append(element("span", "ref-title", title));
    if (manual?.manual && manual.manual !== title) {
      group.append(element("span", "ref-designation", "Обозначение: " + manual.manual));
    }
    group.append(links);
    for (const page of [...pages].sort((a, b) => a - b)) {
      const topic = manualPageTopics[manual?.slug]?.[page];
      const label = "PDF, стр. " + page + (topic ? " — " + topic : "");
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

function analysisSections(items, refs) {
  return (items || []).map((item) => ({
    items: [item.text],
    references: (refs || []).filter((ref) => ref.fragment_id === item.fragment_id),
  }));
}

function riskSummary(brief, generatorCheck) {
  const [label, theme] = riskLabels[brief?.risk] || riskLabels.unassessed;
  const box = element("section", "risk-summary");
  box.dataset.tone = theme;
  if (generatorCheck) {
    box.id = "generator-risk";
    box.tabIndex = -1;
    box.setAttribute("aria-label", "Оценка риска");
  }
  box.append(element("span", "flag " + theme, generatorCheck?.value === "discharge" ? "Высокий риск для эксплуатации" : label),
             element("p", "", brief?.rationale || "Уточните симптомы и результаты контрольной проверки."));
  const note = generatorCheck
    ? (generatorCheck.value === "discharge" ? "Предварительная оценка. Решение о выпуске принимают после дефектовки." : "")
    : (brief?.risk === "unassessed"
      ? "По одной записи степень риска не установлена. Нужна очная проверка."
      : "Предварительная оценка по записи и руководству. Степень риска уточнят после очной проверки.");
  if (note) box.append(element("p", "risk-note", note));
  return box;
}

function generatorReadingControls(check) {
  const controls = element("div", "generator-reading");
  controls.id = "generator-reading";
  controls.setAttribute("role", "group");
  const label = "Амперметр на номинальных оборотах";
  controls.setAttribute("aria-label", label);
  if (check.value && check.value !== "unknown") controls.append(element("p", "risk-note", label));
  for (const [value, text] of [["discharge", "Есть разряд"], ["no_discharge", "Разряда нет"], ["unknown", "Не проверено"]]) {
    const choice = button(text);
    const selected = value === (check.value ?? "unknown");
    choice.disabled = Boolean(view.busy) || selected;
    choice.setAttribute("aria-pressed", String(selected));
    choice.addEventListener("click", async () => {
      const result = await action("chat", "chat", "POST", {
        message: text, generator_reading: { value, diagnosis_revision: check.diagnosis_revision },
      });
      if (result) byId("generator-risk")?.focus();
      else if (!ui["chat-error"].hidden) ui["chat-error"].scrollIntoView({ block: "nearest" });
    });
    controls.append(choice);
  }
  return controls;
}

function verdictMeta(diagnosis) {
  const parts = [];
  if (diagnosis.anomaly_score != null) parts.push("Балл отклонения " + score.format(diagnosis.anomaly_score));
  if (diagnosis.confidence != null) parts.push("Уверенность ML " + score.format(diagnosis.confidence));
  if (diagnosis.signal_quality) parts.push("Качество записи: " + qualityNames[diagnosis.signal_quality].toLowerCase());
  return element("p", "verdict-meta", parts.join(" · "));
}

function renderDiagnosis() {
  const wasExpanded = Boolean(ui.result.querySelector(".diagnosis-details")?.open);
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
      diagnosis_needed: "Сначала получите результат диагностики",
      diagnosis_changed: "Результат проверки изменился",
      draft_unavailable: "Черновик пока недоступен", draft_locked: "Черновик заблокирован",
      stale_draft: "Черновик изменился", engine_mismatch: "Проверьте модификацию двигателя",
      source_not_applicable: "Ссылка не подтверждена для двигателя",
      manual_search_no_match: "Поиск не нашёл нужную страницу",
      ml_timeout: "Тайм-аут диагностики", client_timeout: "Тайм-аут запроса", team_timeout: "Тайм-аут бэкенда",
      ml_unavailable: "Сервис диагностики недоступен", ml_invalid_response: "Некорректный ответ диагностики",
      team_unavailable: "Бэкенд команды недоступен", offline: "Связь потеряна",
      agent_timeout: "Агент не ответил вовремя", agent_unavailable: "Агент недоступен",
      agent_invalid_response: "Некорректный ответ агента", agent_invalid_draft: "Некорректный черновик агента",
      priority_unconfirmed: "Подтвердите приоритет", reconcile_unavailable: "Сверка номера недоступна",
      invalid_request: "Проверьте поля запроса", server_error: "Ошибка сервера",
    };
    ui.diagnostic.dataset.status = "error";
    showHeading(result, titles[view.diagnosisError.code] || "Запрос завершился с ошибкой");
    result.append(alert(view.diagnosisError.message, "error"));
    if (view.pair) {
      const retry = button("Повторить получение отчёта");
      retry.id = "retry-diagnosis";
      retry.disabled = Boolean(view.busy);
      retry.addEventListener("click", () => choose(view.pair.vehicle_id, view.pair.session_id));
      result.append(retry);
    }
    if (!diagnosis) return;
    result.append(element("p", "section-label", "Последний сохранённый результат"));
  } else if (!diagnosis) {
    if (view.pair) {
      showHeading(result, "Подготавливаем отчёт");
      result.append(element("p", "context-key", view.pair.vehicle_id + " / " + view.pair.session_id));
    } else {
      showHeading(result, vehiclePicker.selected ? "Выберите запись диагностики" : "Выберите машину и запись");
      result.append(element("p", "empty-note", vehiclePicker.selected
        ? "Отчёт по записи откроется автоматически."
        : "Найдите машину по госномеру или двигателю. Здесь появится отчёт по выбранной записи."));
    }
    return;
  }

  const vehicle = vehiclePicker.selected;
  const session = vehicle?.sessions.find((item) => item.session_id === diagnosis.session_id);
  const context = element("div", "result-context");
  context.append(element("span", "result-vehicle", diagnosis.vehicle_id));
  if (session) {
    const checkedAt = element("time", "", formatDate(session.checked_at) + " МСК");
    checkedAt.dateTime = session.checked_at;
    context.append(checkedAt);
  }
  if (!view.diagnosisError) result.replaceChildren(context);
  else result.append(context);
  const finding = element("div", "ml-finding");
  const abnormal = diagnosis.status === "anomaly";
  const label = diagnosis.status === "check_failed" ? "Качество записи" : abnormal ? "Отклонение ML" : "Результат ML";
  const caption = element("p", "ml-caption", label);
  if (abnormal && diagnosis.anomaly_type) {
    caption.append(element("span", "ml-code", diagnosis.anomaly_type));
  }
  const title = (abnormal && (diagnosis.anomaly_title || view.catalog?.fault_types?.[diagnosis.anomaly_type]))
    || outcomes[diagnosis.status];
  const heading = element(view.diagnosisError ? "h3" : "h2", "", title);
  heading.tabIndex = -1;
  if (!view.diagnosisError) heading.id = "result-title";
  finding.append(caption, heading, element("p", "diagnosis-description", diagnosis.description));
  const meta = verdictMeta(diagnosis);
  if (meta.textContent) finding.append(meta);
  result.append(finding);
  const brief = view.state.brief;
  const generatorCheck = view.state.generator_check;
  if (diagnosis.status === "anomaly") result.append(riskSummary(brief, generatorCheck));
  const admission = view.state.admission;
  const blocked = admission?.block_reason;
  if (blocked && !admission.can_prepare && diagnosis.status !== "normal" && !view.state.draft) {
    result.append(admissionNotice(admission));
  }
  const recordingNotes = [];
  if (diagnosis.signal_quality === "warning") recordingNotes.push("Запись с помехами.");
  if (diagnosis.status !== "check_failed" && blocked?.code !== "confidence_too_low"
      && diagnosis.confidence != null && diagnosis.confidence < view.catalog.confidence_threshold) {
    recordingNotes.push("Низкая уверенность ML (" + score.format(diagnosis.confidence) + ").");
  }
  if (recordingNotes.length) result.append(alert(recordingNotes.join(" ") + " Сверьте результат повторной проверкой.", "recording-note"));
  if (view.state.brief_error) result.append(alert(view.state.brief_error, "error"));

  const next = element("section", "first-check");
  next.dataset.sourceContext = "";
  const nextTitles = { normal: "Дальнейшее действие", check_failed: "Нужна новая запись", anomaly: "Первая проверка" };
  next.append(element("h3", "", nextTitles[diagnosis.status]));
  const defaultCheck = {
    normal: "Продолжайте эксплуатацию и штатный контроль двигателя.",
    check_failed: "Повторите запись. По этой записи вывод о двигателе невозможен.",
    anomaly: "Уточните симптомы и фактические измерения для дефектовки.",
  };
  // A failed recording must never inherit a procedure or engine verdict.
  next.append(element("p", "action", diagnosis.status === "anomaly"
    ? brief?.first_check || defaultCheck.anomaly : defaultCheck[diagnosis.status]));
  if (diagnosis.status === "anomaly" && brief?.references?.length) next.append(references(brief.references));
  if (generatorCheck) next.append(generatorReadingControls(generatorCheck));
  result.append(next);

  const analysis = brief?.analysis;
  if (diagnosis.status === "anomaly" && analysis?.possible_causes?.length) {
    result.append(answerBlock("causes", analysisSections(analysis.possible_causes, analysis.references)));
  }
  if (diagnosis.status === "anomaly" && analysis?.checks?.length) {
    const firstKey = answerTextKey(brief?.first_check || "");
    const further = analysis.checks.filter((item) => answerTextKey(item.text) !== firstKey);
    if (further.length) {
      result.append(answerBlock("checks", analysisSections(further, analysis.references),
        "Дальнейшие проверки по руководству"));
    }
  }
  if (diagnosis.status === "anomaly" && analysis?.repair_actions?.length) {
    result.append(answerBlock("work", analysisSections(analysis.repair_actions, analysis.references)));
  }

  const controls = element("div", "diagnosis-actions");
  if (diagnosis.status === "anomaly") {
    const check = button("Проверки в чате", "quiet");
    check.id = "diagnosis-checks";
    check.disabled = Boolean(view.busy);
    check.addEventListener("click", () => {
      const cards = ui.messages.querySelectorAll(".check-card");
      if (cards.length) cards[cards.length - 1].closest(".bubble").scrollIntoView({ block: "start" });
      else sendQuestion("Что проверить сначала?");
    });
    controls.append(check);
  } else {
    const select = button(diagnosis.status === "check_failed" ? "Выбрать новую запись" : "Выбрать следующую запись", "primary");
    select.disabled = Boolean(view.busy);
    select.addEventListener("click", () => {
      view.editingContext = true;
      renderContext();
      ui["recording-help"].hidden = diagnosis.status !== "check_failed";
      ui.session.focus();
      ui.selection.scrollIntoView({ block: "start" });
    });
    controls.append(select);
  }
  if (diagnosis.status === "anomaly" && (view.state.draft || (!view.diagnosisError && view.state.draft_available))) {
    const draftButton = button(view.state.draft ? "Открыть заявку"
      : view.busy === "prepare" ? "Готовим черновик…" : "Подготовить заявку", "primary");
    draftButton.id = "diagnosis-draft";
    draftButton.setAttribute("aria-controls", "ticket-panel");
    draftButton.disabled = Boolean(view.busy);
    draftButton.addEventListener("click", view.state.draft ? revealTicket : prepareDraft);
    controls.prepend(draftButton);
    if (!view.state.draft && view.ticketError) {
      const error = alert(view.ticketError, "error");
      error.setAttribute("role", "alert");
      controls.append(error);
    }
  }
  result.append(controls);
  if (diagnosis.status === "anomaly" && !view.state.draft && view.state.draft_unavailable_reason && !view.state.admission?.block_reason) {
    const notice = alert(view.state.draft_unavailable_reason);
    notice.id = "draft-unavailable-reason";
    notice.setAttribute("role", "status");
    result.append(notice);
  }

  const details = element("details", "diagnosis-details");
  details.open = wasExpanded;
  details.append(element("summary", "", "Технические данные"));
  const facts = element("dl", "technical-facts");
  for (const [label, value] of [
    ["Сессия", diagnosis.session_id],
    ["Балл отклонения · 0–1", diagnosis.anomaly_score == null ? "Нет значения" : score.format(diagnosis.anomaly_score)],
    ["Уверенность ML · 0–1", diagnosis.confidence == null ? "Нет значения" : score.format(diagnosis.confidence)],
    ["Качество записи", qualityNames[diagnosis.signal_quality]],
    ["Версия модели", diagnosis.model_version],
  ]) {
    const row = element("div");
    row.append(element("dt", "", label), element("dd", "", value));
    facts.append(row);
  }
  details.append(facts, element("p", "muted", "Балл отклонения и уверенность ML не являются вероятностью поломки."));
  if (brief?.origin === "fixture") details.append(element("p", "context-key", "Пояснение из тестового сценария"));
  result.append(details);
}

function answerTextKey(text) {
  return text.trim().replace(/\s+/g, " ");
}

function uniqueAnswerParts(sections) {
  const byText = new Map();
  for (const section of sections) {
    for (const text of section.items) {
      const key = answerTextKey(text);
      if (!byText.has(key)) byText.set(key, { text, sources: new Map() });
      const { sources } = byText.get(key);
      for (const source of section.references || []) {
        sources.set(JSON.stringify([source.manual, source.pdf_page]), source);
      }
    }
  }
  const parts = [];
  let previousSources;
  for (const { text, sources } of byText.values()) {
    const sourceKey = JSON.stringify([...sources.keys()].sort());
    if (sourceKey === previousSources) parts.at(-1).items.push(text);
    else parts.push({ items: [text], references: [...sources.values()] });
    previousSources = sourceKey;
  }
  return parts;
}

function answerBlock(kind, sections, title = sectionNames[kind] || "Рекомендации", reportInDiagnosis = false) {
  const expandable = kind === "work";
  const block = element(expandable ? "details" : "section", "answer-block");
  block.dataset.kind = kind;
  block.dataset.sourceContext = "";
  block.append(element(expandable ? "summary" : "h3", "", title));
  const parts = uniqueAnswerParts(sections);
  const numbered = kind === "checks" && parts.reduce((count, part) => count + part.items.length, 0) > 1;
  let number = 1;
  for (const section of parts) {
    const part = element("div", "answer-part");
    if (!numbered && section.items.length === 1) {
      part.append(element("p", "", section.items[0]));
    } else {
      const list = element(numbered ? "ol" : "ul");
      if (numbered) list.start = number;
      for (const item of section.items) list.append(element("li", "", item));
      number += section.items.length;
      part.append(list);
    }
    if (section.references?.length) part.append(references(section.references));
    block.append(part);
  }
  if (kind === "checks") {
    const pages = new Set(parts.flatMap((part) => part.references.map((item) => item.pdf_page)));
    const report = button("Сообщить результат");
    report.disabled = Boolean(view.busy);
    report.addEventListener("click", () => {
      if (reportInDiagnosis) {
        byId("generator-reading")?.querySelector("button:not(:disabled)")?.focus();
        return;
      }
      composeAnswer(pages.size
        ? "Результат проверки по руководству (PDF, стр. " + [...pages].join(", ") + "):\n"
        : "Результат проверки:\n");
    });
    block.append(report);
  }
  return block;
}

function composeAnswer(text, alternatives = []) {
  let current = ui.question.value;
  for (const alternative of alternatives) current = current.replace(alternative, "");
  const combined = current.trimEnd() + (current.trim() ? "\n" : "") + text;
  if (combined.length > ui.question.maxLength) {
    announce("Текст слишком длинный. Сократите сообщение перед добавлением результата.");
    ui.question.focus();
    return;
  }
  ui.question.value = combined;
  ui.question.dispatchEvent(new Event("input"));
  ui.question.focus();
  ui.question.scrollIntoView({ block: "nearest" });
}

function userMessage(text) {
  const bubble = element("article", "bubble user");
  bubble.append(element("p", "message-meta", "Вы"), element("p", "", text));
  return bubble;
}

function renderChat() {
  const diagnosis = view.state?.diagnosis;
  const engine = vehiclePicker.selected?.engine_model;
  const history = view.state?.messages || [];
  const report = history.find((message) => message.role === "diagnosis");
  const reportTexts = new Set([report?.summary, diagnosis?.description, diagnosis?.anomaly_title]
    .filter(Boolean).map(answerTextKey));
  if (diagnosis?.status === "anomaly" && diagnosis.anomaly_title) {
    reportTexts.add(`${outcomes.anomaly}: ${diagnosis.anomaly_title}.`);
  }
  const messages = history.filter((message) => message.role !== "diagnosis");
  ui.chat.hidden = !diagnosis;
  ui["chat-context"].textContent = diagnosis ? "Сессия " + view.pair.session_id : "";
  ui["ask-question"].hidden = messages.length <= 2;
  const openDetails = [...ui.messages.querySelectorAll("details")].map((node) => node.open);
  const fragment = document.createDocumentFragment();
  for (const message of messages) {
    if (message.role === "user") {
      fragment.append(userMessage(message.text));
      continue;
    }
    const bubble = element("article", "bubble");
    bubble.tabIndex = -1;
    bubble.append(element("p", "message-meta", message.origin === "fixture" ? "Механик · тестовый ответ" : "Механик"));
    const parts = (message.sections || []).flatMap((part) => {
      if (part.kind !== "signs" || part.references?.length) return [part];
      const items = part.items.filter((item) => !reportTexts.has(answerTextKey(item)));
      return items.length ? [{ ...part, items }] : [];
    });
    const summary = answerTextKey(message.summary);
    if (reportTexts.has(summary)) {
      if (!parts.length) {
        const link = element("a", "", "К результату проверки");
        link.href = "#result-title";
        bubble.append(link);
      }
    } else if (!parts.some((part) => part.kind !== "work" && part.items.some((item) => answerTextKey(item) === summary))) {
      bubble.append(element("p", "answer-title", message.summary));
    }
    const sections = new Map();
    for (const part of parts) {
      if (!sections.has(part.kind)) sections.set(part.kind, []);
      sections.get(part.kind).push(part);
    }
    const measured = Object.keys(message.analysis?.pressure_measurement || {}).length > 0;
    const measurementTitle = measured ? "Сверка с нормативом"
      : message.analysis?.intent === "measurement" ? "Условия замера" : undefined;
    const reportInDiagnosis = Boolean(view.state.generator_check && message.origin === "rule"
      && [null, "unknown"].includes(message.analysis?.generator_reading));
    for (const kind of new Set([...Object.keys(sectionNames), ...sections.keys()])) {
      if (sections.has(kind)) bubble.append(answerBlock(kind, sections.get(kind),
        kind === "checks" ? measurementTitle : undefined, reportInDiagnosis));
    }
    fragment.append(bubble);
  }
  if (view.pendingQuestion) fragment.append(userMessage(view.pendingQuestion));
  if (view.busy === "chat") fragment.append(progress("Подготавливаем ответ"));
  ui.messages.replaceChildren(fragment);
  [...ui.messages.querySelectorAll("details")].forEach((node, index) => { node.open = Boolean(openDetails[index]); });
  setAlert(ui["chat-error"], view.chatError);
  renderChatActions(diagnosis, engine);
}

function renderChatActions(diagnosis, engine) {
  const suggestions = ui.suggestions;
  suggestions.replaceChildren();
  const status = diagnosis?.status;
  const oil = status === "anomaly" && diagnosis.anomaly_type === "F02" && engine === "ЯМЗ-236М2";
  const coolant = status === "anomaly" && diagnosis.anomaly_type === "F03" && engine === "ЯМЗ-236М2";
  suggestions.hidden = !diagnosis;
  const quickReplies = ui["quick-replies"];
  quickReplies.hidden = status !== "anomaly";
  if (quickReplies.hidden) quickReplies.open = false;
  const pressureHelper = ui["pressure-helper"];
  const showPressure = oil && view.catalog?.pressure_comparison;
  if (!showPressure && !pressureHelper.hidden) {
    pressureHelper.open = false;
    ui["pressure-form"].reset();
  }
  pressureHelper.hidden = !showPressure;
  ui.question.placeholder = {
    anomaly: "Узел, симптом или замер",
    normal: "Например, стук на холодном пуске",
    check_failed: "Например, почему не прошла проверка?",
  }[status] || "Например, что проверить сначала?";

  if (status === "check_failed") {
    const select = button("Выбрать другую сессию");
    select.disabled = Boolean(view.busy);
    select.addEventListener("click", () => {
      view.editingContext = true;
      renderContext();
      ui.session.focus({ preventScroll: true });
      ui.session.scrollIntoView({ block: "nearest" });
    });
    suggestions.append(select);
  }
  if (!diagnosis) return;

  const prompts = status === "check_failed" ? [["Как повторить запись?"]] : [["Что проверить сначала?"]];
  if (status === "anomaly") {
    if (oil) prompts.push(["Условия замера давления", "На каких оборотах замеряют давление масла?"]);
    else prompts.push(["Причины отклонения", "Какие причины отклонения указаны в руководстве?"]);
    prompts.push(["Риск перегона", "Можно ли своим ходом перегнать машину?"]);
    if (coolant) {
      prompts.push(["Возможные работы", "Какие работы предусмотрены руководством?"]);
      prompts.push(["Норматив температуры", "Какая норма температуры охлаждающей жидкости?"]);
    }
  }
  for (const [label, question = label] of prompts) {
    const chip = element("button", "", label);
    chip.type = "button";
    chip.disabled = Boolean(view.busy);
    chip.addEventListener("click", () => sendQuestion(question));
    suggestions.append(chip);
  }
  const replies = ui["reply-options"];
  replies.replaceChildren();
  if (diagnosis.status !== "anomaly") return;
  const groups = [
    [["На холодном", "Двигатель холодный."], ["После прогрева", "Двигатель прогрет."]],
    [["Пока не измерял", "Фактические значения пока не измерял."]],
  ];
  const lastReply = [...(view.state.messages || [])].reverse().find((message) => message.role === "assistant");
  const clarification = (lastReply?.sections || []).filter((section) => section.kind === "clarify")
    .flatMap((section) => section.items).join(" ");
  if (/дым/i.test(clarification) || ["F08", "F09", "F13"].includes(diagnosis.anomaly_type)) {
    groups.push([["Есть дым", "Наблюдаю дым из выхлопной трубы."], ["Дыма нет", "Дыма из выхлопной трубы не наблюдаю."]]);
  }
  for (const group of groups) {
    for (const [label, text] of group) {
      const chip = button(label);
      chip.disabled = Boolean(view.busy);
      chip.addEventListener("click", () => composeAnswer(text, group.map(([, value]) => value)));
      replies.append(chip);
    }
  }
}

function draftKey() {
  return JSON.stringify([view.pair, view.state?.draft?.anomaly_type]);
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

function admissionNotice(admission) {
  const reason = admission.block_reason;
  const notice = element("div", "alert admission-notice");
  notice.setAttribute("role", "status");
  notice.append(element("strong", "", reason.message), element("p", "", reason.next_action));
  const text = (view.pair ? view.pair.vehicle_id + " / " + view.pair.session_id + "\n" : "")
    + reason.message + "\n" + reason.next_action;
  const actions = element("div", "admission-actions");
  const handoff = button("Вставить в чат", "primary");
  handoff.title = "Текст появится в поле сообщения; отправьте его после проверки.";
  handoff.addEventListener("click", () => {
    composeAnswer("Передаю на очную дефектовку:\n" + text);
    announce("Текст добавлен в поле сообщения. Проверьте и отправьте.");
  });
  const copy = button("Скопировать текст");
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(text); announce("Текст скопирован"); }
    catch { announce(copyThroughSelection(text) ? "Текст скопирован" : "Выделите текст рекомендации и скопируйте вручную."); }
  });
  actions.append(handoff, copy);
  notice.append(actions);
  return notice;
}

function operationEvidence(draft, admission) {
  const group = element("div", "operation-evidence");
  const operations = (admission?.approved_operations || []).filter((row) => draft.operations.includes(row.operation));
  if (!operations.length) group.append(references(draft.references));
  for (const row of operations) {
    const card = element("div", "operation-source");
    card.dataset.sourceContext = "";
    if (operations.length > 1) card.append(element("p", "operation-name", row.operation));
    const evidence = row.evidence || [{ text: row.quote, source: { manual: row.manual, pdf_page: row.pdf_page } }];
    card.append(references(evidence.map((item) => item.source)));
    const full = element("details", "evidence-full");
    full.append(element("summary", "", "Выдержки и условия"));
    for (const condition of row.conditions || []) full.append(element("p", "muted", condition.text));
    for (const item of evidence) {
      full.append(element("p", "ref-title", item.source.manual + " · PDF " + item.source.pdf_page),
                  element("blockquote", "evidence-quote", item.text));
    }
    card.append(full);
    group.append(card);
  }
  return group;
}

function editableDraft(draft, pending, partsExpanded) {
  const key = draftKey();
  const version = pending?.version ?? draftVersion();
  const edits = pending?.edits ?? draft;
  const form = element("form", "ticket-form");
  const admission = view.state.admission;
  const conclusion = textarea(edits.conclusion, 2, true);
  const operations = textarea(edits.operations.join("\n"), 2, true);
  operations.readOnly = true;
  const parts = textarea(edits.spare_parts.join("\n"), 2);
  const priority = element("select");
  priority.required = true;
  priority.add(option("", "Выберите приоритет"));
  for (const [value, label] of Object.entries(urgencyNames)) priority.add(option(value, label));
  priority.value = edits.urgency || "";
  const priorityExplanation = element("p", "priority-explanation");
  priorityExplanation.id = "priority-explanation";
  priority.setAttribute("aria-describedby", priorityExplanation.id);
  const showPriority = () => {
    const suggested = urgencyNames[admission?.suggested_urgency];
    priorityExplanation.textContent = [suggested && "Предложен: " + suggested, urgencyDescriptions[priority.value]]
      .filter(Boolean).join(". ");
  };
  priority.addEventListener("change", showPriority);
  showPriority();
  const work = element("section", "draft-work");
  work.append(field("operations", "Работы", operations), operationEvidence(draft, admission));
  const extras = element("details", "draft-parts");
  extras.open = partsExpanded || edits.spare_parts.some((item) => item.trim());
  extras.append(element("summary", "", "Запчасти"), field("parts", "По одной на строку", parts));
  parts.placeholder = "Не определены";
  form.append(field("conclusion", "Заключение", conclusion), work, field("urgency", "Приоритет", priority),
    priorityExplanation, extras);
  const confirmations = element("div", "draft-confirmations");
  const required = new Map();
  for (const operation of admission?.approved_operations || []) {
    if (!draft.operations.includes(operation.operation)) continue;
    for (const condition of operation.conditions || []) {
      if (condition.requires_confirmation) required.set(condition.id, condition);
    }
  }
  const conditionChecks = [];
  for (const [id, condition] of required) {
    const label = element("label", "priority-confirm");
    const input = element("input");
    input.type = "checkbox";
    input.checked = (view.state.applicability_confirmations || []).includes(id);
    label.append(input, document.createTextNode("Подтверждаю условие: " + condition.text));
    conditionChecks.push({id, input});
    confirmations.append(label);
  }
  const warning = element("input");
  warning.type = "checkbox";
  warning.id = "warning-acknowledged";
  if (admission?.warning_ack_required) {
    const label = element("label", "priority-confirm warning-confirm");
    label.append(warning, document.createTextNode("Запись с помехами. Предупреждение прочитал, сознательно продолжаю."));
    confirmations.append(label);
  }
  const confirm = element("label", "priority-confirm");
  const checkbox = element("input");
  checkbox.type = "checkbox";
  const consentKey = [key, version.draft_id, version.revision, admission?.diagnosis_revision].join("|");
  if (view.consents?.key === consentKey) {
    checkbox.checked = view.consents.priority;
    warning.checked = view.consents.warning;
    for (const row of conditionChecks) row.input.checked = view.consents.conditions.includes(row.id);
  }
  confirm.append(checkbox, document.createTextNode("Подтверждаю выбранный приоритет"));
  const actions = element("div", "ticket-actions");
  const status = draftSaveStatus(changedDraftFields(edits, draft).length > 0);
  const save = button(view.busy === "save" ? "Сохраняем…" : "Сохранить изменения");
  const submit = button("Подтвердить и отправить", "primary", "submit");
  save.disabled = Boolean(view.busy);
  submit.disabled = true;
  for (const control of [conclusion, operations, parts, priority, checkbox, warning, ...conditionChecks.map((row) => row.input)]) control.disabled = Boolean(view.busy);
  actions.append(status, submit, save);
  confirmations.append(confirm);
  form.append(confirmations, actions);

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
      warning.checked = false;
      updateSubmit();
    });
  }
  function acknowledgements() {
    return { warning_acknowledged: warning.checked,
      diagnosis_revision: admission?.diagnosis_revision,
      applicability_confirmations: conditionChecks.filter((row) => row.input.checked).map((row) => row.id) };
  }
  function updateSubmit() {
    view.consents = {key:consentKey, priority:checkbox.checked, warning:warning.checked,
      conditions:conditionChecks.filter((row) => row.input.checked).map((row) => row.id)};
    const conditionalBlock = ["warning_ack_required", "applicability_required"].includes(admission?.block_reason?.code);
    const allowed = admission?.can_submit || (admission?.can_prepare && conditionalBlock);
    submit.disabled = Boolean(view.busy) || !allowed || !checkbox.checked || !priority.value
      || (admission?.warning_ack_required && !warning.checked) || conditionChecks.some((row) => !row.input.checked);
  }
  checkbox.addEventListener("change", updateSubmit);
  warning.addEventListener("change", updateSubmit);
  for (const row of conditionChecks) row.input.addEventListener("change", () => { checkbox.checked = false; updateSubmit(); });
  save.addEventListener("click", () => {
    if (form.reportValidity()) updateDraft(values(true), version, false, acknowledgements());
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!submit.disabled) updateDraft(values(true), version, true, acknowledgements());
  });
  updateSubmit();
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

function reconcileForm(version = draftVersion(), fieldId = "reconcile-ticket-id") {
  const form = element("form", "reconcile-form");
  const ticketId = element("input", "ticket-number");
  ticketId.type = "text";
  ticketId.required = true;
  ticketId.pattern = "TKT-[0-9]{4,}";
  ticketId.autocomplete = "off";
  ticketId.maxLength = 40;
  ticketId.placeholder = "TKT-0001";
  ticketId.disabled = Boolean(view.busy);
  form.append(element("h3", "", "Сверка с ТОиР"), field(fieldId, "Номер заявки в ТОиР", ticketId));

  const confirmation = element("label", "priority-confirm");
  const verified = element("input");
  verified.type = "checkbox";
  verified.id = fieldId === "reconcile-ticket-id" ? "reconcile-verified" : fieldId + "-verified";
  verified.disabled = Boolean(view.busy);
  confirmation.append(verified, document.createTextNode("Номер сверил в ТОиР"));
  const submit = button(view.busy === "reconcile" ? "Сохраняем номер…" : "Сохранить номер", "primary", "submit");
  submit.disabled = true;
  verified.addEventListener("change", () => { submit.disabled = !verified.checked; });
  const actions = element("div", "ticket-actions");
  actions.append(submit);
  form.append(confirmation, actions);

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (verified.checked) reconcileDraft(ticketId.value.trim(), version, fieldId);
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
  const previous = view.state?.previous_drafts ?? [];
  const partsExpanded = panel.dataset.draftId === view.state?.draft_id && Boolean(panel.querySelector(".draft-parts")?.open);
  panel.hidden = !draft && !previous.length;
  if (!draft || !ticket) {
    delete panel.dataset.draftId;
    panel.replaceChildren();
    if (view.ticketError && previous.length) panel.append(alert(view.ticketError, "error"));
    renderPreviousDrafts(panel, previous);
    return;
  }
  const title = element("h2", "", ticket.status === "created" ? "Заявка на дефектовку" : "Черновик дефектовки");
  title.id = "ticket-title";
  title.tabIndex = -1;
  const header = element("div", "ticket-head");
  header.append(title);
  if (ticket.ticket_id) header.append(ticketReceipt(ticket.ticket_id));
  panel.replaceChildren(header);
  panel.dataset.draftId = view.state.draft_id;

  const ids = element("div", "ticket-ids");
  for (const text of ["ТС " + draft.vehicle_id, "Сессия " + draft.session_id, "Код ML " + draft.anomaly_type]) {
    ids.append(element("span", "", text));
  }
  panel.append(ids);
  const admission = view.state.admission;
  const awaitingConfirmation = ["warning_ack_required", "applicability_required"].includes(admission?.block_reason?.code);
  if (admission?.block_reason && !awaitingConfirmation && !["created", "sending", "unknown"].includes(ticket.status)) {
    panel.append(admissionNotice(admission));
  }
  if (view.state.confirmed_urgency) panel.append(element("p", "context-key", "Подтверждённый приоритет — " + urgencyNames[view.state.confirmed_urgency]));

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
  else panel.append(canEdit ? editableDraft(draft, pending, partsExpanded) : fieldSummary(draft));
  if (!conflict && !view.needsTicketCheck && ticket.status === "unknown" && view.state.reconcile_available) {
    panel.append(reconcileForm());
  }
  renderPreviousDrafts(panel, previous);
}

function renderPreviousDrafts(panel, drafts) {
  if (!drafts.length) return;
  const section = element("section", "previous-drafts");
  section.append(element("h3", "", "Заявки по предыдущим отклонениям"));
  const list = element("ul", "history-list");
  for (const draft of drafts) {
    const row = element("li");
    row.append(element("strong", "", draft.anomaly_type),
      element("span", "", ticketStatusNames[draft.status]));
    if (draft.ticket_id) row.append(element("span", "muted", draft.ticket_id));
    if (draft.status === "unknown") {
      row.append(reconcileForm(draft, "reconcile-" + draft.draft_id));
    }
    list.append(row);
  }
  section.append(list);
  panel.append(section);
}

function renderJournal() {
  const calls = view.state?.calls;
  ui["journal-panel"].hidden = !calls?.length;
  if (!calls?.length) {
    ui["journal-panel"].open = false;
    ui.journal.replaceChildren();
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
    ui["context-session"].textContent = [view.pair.session_id, vehiclePicker.selected?.engine_model].filter(Boolean).join(" · ");
  }
  ui["context-summary"].hidden = !collapsed;
  ui.selection.hidden = collapsed;
  ui["collapse-context"].hidden = !collapsible || collapsed;
  ui["selection-hint"].hidden = !vehiclePicker.selected || Boolean(view.pair);
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

async function loadHistory(vehicleId, force = false) {
  if (!vehicleId || (!force && view.historyVehicle === vehicleId)) return;
  const request = ++view.historyRequest;
  const generation = view.generation;
  const isCurrent = () => request === view.historyRequest && generation === view.generation;
  view.historyVehicle = vehicleId;
  view.historyError = null;
  try {
    const history = await api("history?" + new URLSearchParams({ vehicle_id: vehicleId }));
    if (!isCurrent()) return;
    view.history = history;
  } catch (error) {
    if (!isCurrent()) return;
    view.historyError = error;
  }
  renderHistory();
}

function renderHistory() {
  const panel = ui["vehicle-history"];
  const content = ui["history-content"];
  const vehicleId = view.pair?.vehicle_id;
  const loaded = view.history?.vehicle_id === vehicleId;
  panel.hidden = !(view.state?.diagnosis && (loaded || view.historyError));
  if (panel.hidden) {
    panel.open = false;
    content.replaceChildren();
    return;
  }
  const nodes = [];
  if (view.historyError) {
    nodes.push(alert("Историю не удалось загрузить. " + view.historyError.message, "error"));
  } else {
    const sessions = view.history.sessions;
    const trend = element("div", "history-trend");
    const values = sessions.map((step) => step.anomaly_score == null ? "—" : score.format(step.anomaly_score));
    trend.append(fleetSparkline(sessions), element("span", "muted", values.join(" → ")));
    nodes.push(trend);
    nodes.push(element("p", "trend-note", historyTrend(sessions, view.pair?.session_id)));
    const list = element("ul", "history-list");
    for (const step of sessions) {
      const row = element("li", step.session_id === view.pair.session_id ? "current" : "");
      const verdict = step.anomaly_title || outcomes[step.status];
      const checkedAt = element("time", "", formatDate(step.checked_at));
      checkedAt.dateTime = step.checked_at;
      row.append(checkedAt, element("span", "", verdict));
      if (step.ticket.status !== "idle") {
        row.append(element("span", "muted", step.ticket.ticket_id || ticketStatusNames[step.ticket.status] || ""));
      }
      list.append(row);
    }
    nodes.push(list);
  }
  content.replaceChildren(...nodes);
}

function historyTrend(sessions, currentId) {
  const index = sessions.findIndex((step) => step.session_id === currentId);
  const end = index >= 0 ? index : sessions.length - 1;
  if (end < 1) return "Недостаточно данных для оценки динамики.";
  const current = sessions[end].anomaly_score;
  const previous = sessions[end - 1].anomaly_score;
  if (current == null || previous == null) {
    return "В одной из проверок балл не определён; динамику оценить нельзя.";
  }
  const delta = current - previous;
  if (delta >= 0.2) return "Резкий рост аномальности к текущей проверке — приоритет высокий.";
  if (delta <= -0.2) return "Аномальность заметно снизилась к текущей проверке.";
  if (Math.abs(delta) < 0.05) return "Аномальность без выраженной динамики.";
  return delta > 0 ? "Аномальность умеренно растёт." : "Аномальность умеренно снижается.";
}

const fleetLabels = {
  quarantine: "Карантин", attention: "Требует внимания",
  recheck: "Повторить запись", ready: "Готово к работе",
};
const fleetTones = { quarantine: "danger", attention: "warn", recheck: "warn", ready: "good" };
const ticketStatusNames = {
  idle: "Заявки нет", draft: "Есть черновик", sending: "Отправляется",
  created: "Заявка зарегистрирована", unknown: "Нужна сверка номера", error: "Ошибка отправки",
};

function fleetSparkline(history) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 90 24");
  svg.setAttribute("class", "sparkline");
  svg.setAttribute("aria-hidden", "true");
  const line = document.createElementNS(ns, "path");
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "currentColor");
  line.setAttribute("stroke-width", "2");
  svg.append(line);
  const path = [];
  let command = "M";
  for (const [index, { anomaly_score: value }] of history.entries()) {
    if (value == null) {
      command = "M";
      continue;
    }
    const x = (3 + index * 84 / Math.max(history.length - 1, 1)).toFixed(1);
    const y = (21 - value * 18).toFixed(1);
    path.push(command + x + " " + y);
    command = "L";
    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("cx", x);
    dot.setAttribute("cy", y);
    dot.setAttribute("r", "2");
    dot.setAttribute("fill", "currentColor");
    svg.append(dot);
  }
  line.setAttribute("d", path.join(" "));
  return svg;
}

function renderFleet() {
  const panel = ui["fleet-panel"];
  panel.hidden = !view.fleetOpen;
  ui.context.hidden = view.fleetOpen;
  ui.workarea.hidden = view.fleetOpen;
  ui.workspace.dataset.page = view.fleetOpen ? "fleet" : "diagnosis";
  ui["fleet-toggle"].setAttribute("aria-expanded", String(view.fleetOpen));
  ui["fleet-toggle"].textContent = view.fleetOpen ? "К диагностике" : "Состояние парка";
  if (!view.fleetOpen) return;

  const heading = element("h2", "", "Состояние парка");
  heading.id = "fleet-title";
  const nodes = [heading];
  if (view.fleetLoading) {
    nodes.push(progress("Загружаем сводку по парку…"));
  } else if (view.fleetError) {
    nodes.push(alert("Сводка недоступна. " + view.fleetError.message, "error"));
    const retry = button("Повторить загрузку");
    retry.addEventListener("click", () => loadFleet(true));
    nodes.push(retry);
  } else if (view.fleet) {
    const summary = element("div", "fleet-summary");
    const addChip = (key, label, count, tone) => {
      const chip = element("button", "fleet-chip " + tone);
      chip.type = "button";
      chip.dataset.filter = key || "all";
      chip.setAttribute("aria-pressed", String(view.fleetFilter === key));
      chip.append(element("strong", "", String(count)), document.createTextNode(" " + label));
      chip.addEventListener("click", () => {
        view.fleetFilter = view.fleetFilter === key ? null : key;
        renderFleet();
        panel.querySelector('[data-filter="' + chip.dataset.filter + '"]')?.focus({ preventScroll: true });
      });
      summary.append(chip);
    };
    addChip(null, "Все", view.fleet.summary.total, "");
    for (const key of ["quarantine", "attention", "recheck", "ready"]) {
      addChip(key, fleetLabels[key], view.fleet.summary[key] || 0, fleetTones[key]);
    }
    nodes.push(summary);
    const criteria = element("details", "fleet-criteria");
    criteria.open = Boolean(panel.querySelector(".fleet-criteria")?.open);
    const rules = element("ul");
    for (const description of Object.values(view.fleet.criteria)) rules.append(element("li", "", description));
    criteria.append(element("summary", "", "Как определяются категории"), rules);
    nodes.push(criteria);

    const list = element("div", "fleet-list");
    const rows = view.fleetFilter
      ? view.fleet.vehicles.filter((row) => row.category === view.fleetFilter)
      : view.fleet.vehicles;
    if (!rows.length) list.append(element("p", "muted", "В этой категории пока нет машин."));
    else {
      const headings = element("div", "fleet-columns");
      for (const title of ["Машина", "Состояние", "Последняя проверка", "Балл по записям", "Заявка"]) {
        headings.append(element("span", "", title));
      }
      list.append(headings);
    }
    for (const row of rows) {
      const item = element("article", "fleet-row");
      const name = element("div", "fleet-name");
      const model = row.model?.startsWith(row.make) ? row.model : [row.make, row.model].filter(Boolean).join(" ");
      name.append(element("strong", "", row.vehicle_id),
        element("span", "muted", [model, row.engine_model].filter(Boolean).join(" · ")));
      const category = element("span", "fleet-chip " + fleetTones[row.category], fleetLabels[row.category]);
      const latest = element("div", "fleet-latest");
      latest.append(element("strong", "", row.latest.anomaly_title || outcomes[row.latest.status]),
        element("span", "muted", `${date.format(new Date(row.latest.checked_at))} · ${row.latest.session_id}`));
      const history = element("div", "fleet-history");
      const trend = row.history.map((step) => step.anomaly_score == null ? "—" : score.format(step.anomaly_score)).join(" → ");
      history.append(fleetSparkline(row.history), element("span", "muted", trend));
      const ticket = element("div", "fleet-ticket",
        row.ticket.status === "idle" ? "" : row.ticket.ticket_id || ticketStatusNames[row.ticket.status] || row.ticket.status);
      const open = button("К проверке");
      open.addEventListener("click", () => openFleetVehicle(row));
      item.append(name, category, latest, history, ticket, open);
      list.append(item);
    }
    nodes.push(list);
  }
  panel.replaceChildren(...nodes);
}

async function loadFleet(force = false) {
  if (view.fleetLoading && !force) return;
  view.fleetLoading = true;
  view.fleetError = null;
  if (force) view.fleet = null;
  renderFleet();
  try {
    view.fleet = await api("fleet");
  } catch (error) {
    view.fleetError = error;
  } finally {
    view.fleetLoading = false;
    renderFleet();
  }
}

function toggleFleet() {
  view.fleetOpen = !view.fleetOpen;
  renderFleet();
  window.scrollTo(0, 0);
  // Сводка обновляется при каждом открытии: статусы заявок могли измениться.
  if (view.fleetOpen) loadFleet(true);
}

function openFleetVehicle(row) {
  const vehicle = view.catalog?.vehicles.find((item) => item.vehicle_id === row.vehicle_id);
  view.fleetOpen = false;
  renderFleet();
  if (!vehicle) return;
  selectVehicle(vehicle);
  ui.session.value = row.latest.session_id;
  choose(row.vehicle_id, row.latest.session_id);
  ui.diagnostic.scrollIntoView({ block: "start" });
}

function render() {
  setControls();
  renderContext();
  renderDiagnosis();
  renderChat();
  renderTicket();
  renderJournal();
  renderHistory();
  renderFleet();
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
    ui.session.add(option(session.session_id, session.session_id + " · " + formatDate(session.checked_at, sessionDate)));
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
  ui.question.style.height = "";
  ui["recording-help"].hidden = true;
  const vehicle = view.catalog.vehicles.find((item) => item.vehicle_id === vehicleId);
  const invalid = faultForSelection(vehicle, sessionId, vehicleId);
  if (!restore) {
    syncLocation(vehicleId, sessionId);
    view.consents = null;
  }
  view.pair = !invalid && vehicle && sessionId ? { vehicle_id: vehicleId, session_id: sessionId } : null;
  Object.assign(view, { state: null, diagnosisError: invalid, chatError: null, ticketError: null,
    needsTicketCheck: false, pendingQuestion: "", busy: view.pair ? "load" : null, editingContext: false,
    history: null, historyVehicle: null, historyError: null });
  setAlert(ui["selection-error"], invalid?.message);
  render();
  if (!view.pair) return;
  try {
    const state = await api(stateRoute());
    if (generation === view.generation) {
      view.state = state;
      const pendingDraft = restore ? history.state?.sourceOrigin?.pendingDraft : null;
      if (pendingDraft && (pendingDraft.key === draftKey() ||
          (state?.draft_id && pendingDraft.edits?.version?.draft_id === state.draft_id))) {
        if (pendingDraft.edits) view.pendingEdits.set(draftKey(), pendingDraft.edits);
        view.consents = pendingDraft.consents || null;
      }
      if (state?.diagnosis) loadHistory(vehicleId);
    }
  } catch (error) {
    if (generation === view.generation) view.diagnosisError = error;
  } finally {
    if (generation === view.generation) {
      view.busy = null;
      render();
      scheduleTicketCheck();
    }
  }
  if (generation !== view.generation || view.diagnosisError) return;
  if (view.state && !view.state.diagnosis) await action("diagnose", "diagnose", "POST");
  else if (!restore && compactLayout.matches) ui.diagnostic.scrollIntoView({ block: "start" });
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
    ui.mode.title = view.catalog.mode === "demo" ? "Тестовый чат" : "Агент команды";
    vehiclePicker.selected = vehicle || null;
    restoreVehicleInput();
    showSessions(vehicle, vehicle?.sessions.some((item) => item.session_id === sessionId) ? sessionId : "");
    ui.vehicle.disabled = false;
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
  else if (state) {
    ui.question.focus({ preventScroll: true });
    ui.messages.lastElementChild?.scrollIntoView({ block: "nearest" });
  } else byId("diagnosis-draft")?.focus();
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
  setControls();
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
    if (kind === "diagnose") {
      view.editingContext = false;
      loadHistory(view.pair.vehicle_id, true);
    }
    if (kind === "submit" || kind === "reconcile") loadHistory(view.pair.vehicle_id, true);
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
    if (error.state && (["prepare", "save", "submit"].includes(kind) || payload.generator_reading)) view.state = error.state;
    else if (error.admission && view.state) view.state = {...view.state, admission: error.admission};
    if (kind === "diagnose") view.diagnosisError = error;
    if (kind === "chat") view.chatError = error.message;
    if (kind === "prepare" || kind === "save") view.ticketError = error.message;
    if (kind === "reconcile") {
      if (error.code === "stale_draft" && error.state) view.state = error.state;
      view.ticketError = error.message;
    }
    if (kind === "submit") {
      const uncertain = error.status === 0 || error.status >= 500 ||
        error.status === 409 || ["delivery_unknown", "team_timeout", "client_timeout", "server_error", "offline"].includes(error.code);
      view.needsTicketCheck = uncertain && !error.admission && !error.state;
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

async function sendQuestion(value, fromComposer = false) {
  const message = value.trim();
  if (!message || !view.state?.diagnosis || view.busy) return;
  const generation = view.generation;
  const hadDraft = Boolean(view.state?.draft);
  const result = await action("chat", "chat", "POST", { message });
  if (generation !== view.generation) return;
  if (!result) {
    if (!ui.question.value.trim()) ui.question.value = message;
    ui.question.dispatchEvent(new Event("input"));
    if (fromComposer) ui.question.focus({ preventScroll: true });
    return;
  }
  if (fromComposer && ui.question.value.trim() === message) {
    ui.question.value = "";
    ui.question.dispatchEvent(new Event("input"));
  }
  if (result.draft && (!hadDraft || /заявк|наряд|черновик/i.test(message))) revealTicket();
  else {
    const answer = ui.messages.lastElementChild;
    answer?.focus({ preventScroll: true });
    answer?.scrollIntoView({ block: "start" });
  }
  return result;
}

async function updateDraft(edits, version, submit, acknowledgements = {}) {
  const kind = submit ? "submit" : "save";
  const generation = view.generation;
  const key = draftKey();
  if (!view.pendingEdits.has(key)) view.pendingEdits.set(key, { edits, version });
  const result = await action(kind, submit ? "ticket" : "draft", submit ? "POST" : "PUT",
    submit ? { ...version, edits, ...acknowledgements, priority_confirmed: true }
      : { ...version, edits, applicability_confirmations: acknowledgements.applicability_confirmations || [] });
  if (!result || generation !== view.generation) return;
  if (submit) {
    announce(result.ticket.ticket_id ? "Заявка " + result.ticket.ticket_id + " зарегистрирована."
      : result.ticket.message || "Статус заявки обновлён.");
    revealTicket();
  } else announce("Изменения сохранены.");
}

async function reconcileDraft(ticketId, version, fieldId) {
  const generation = view.generation;
  const state = await action("reconcile", "drafts/" + encodeURIComponent(version.draft_id) + "/reconcile", "POST",
    { revision: version.revision, ticket_id: ticketId, verified: true });
  if (generation !== view.generation) return;
  const current = state?.draft_id === version.draft_id;
  const saved = current ? state.ticket.ticket_id
    : state?.previous_drafts?.find((draft) => draft.draft_id === version.draft_id)?.ticket_id;
  if (saved) {
    announce("Сохранён подтверждённый номер " + saved + ".");
    if (current) revealTicket();
  } else {
    const field = byId(fieldId);
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
ui.selection.addEventListener("submit", (event) => event.preventDefault());
ui.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  sendQuestion(ui.question.value, true);
});
ui.question.addEventListener("input", () => {
  ui.question.style.height = "auto";
  ui.question.style.height = ui.question.scrollHeight + "px";
  setChatSubmit();
});
ui.question.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  sendQuestion(ui.question.value, true);
});
ui["pressure-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  if (ui["pressure-helper"].hidden || view.busy) return;
  const values = new FormData(event.currentTarget);
  const question = values.get("pressure") + " кПа, " + values.get("regime") + ", "
    + values.get("warm") + ", " + values.get("tolerance");
  if (await sendQuestion(question)) ui["pressure-helper"].open = false;
});
ui["retry-catalog"].addEventListener("click", loadCatalog);
ui["fleet-toggle"].addEventListener("click", toggleFleet);

compactLayout.addEventListener("change", updateLayout);
document.addEventListener("click", (event) => {
  const key = draftKey();
  const edits = view.pendingEdits.get(key);
  rememberSourceOrigin(event, { key, edits, consents: view.consents });
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) requestAnimationFrame(restoreSourceOrigin);
});
updateLayout();
loadCatalog();
