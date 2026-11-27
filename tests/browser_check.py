"""Приёмка в чистых контекстах Chromium и запись непрерывного демо."""

import argparse
import json
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import perf_counter, sleep
from urllib.parse import urljoin

import httpx
from playwright.sync_api import expect, sync_playwright
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
OIL = ("E549YT198", "S-0011")
NORMAL = ("E549YT198", "S-0010")
FAILED = ("P456AT178", "S-0003")
WARNING = ("M739BA198", "S-0103")
CHAIN = ("Y815BM178", "S-0152")
MISMATCH = ("Y652CK98", "S-0132")
OIL_QUESTION = "На каких оборотах замеряют давление масла и из-за чего оно падает?"


@contextmanager
def stand(port, directory, **overrides):
    if port == 0:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "PYTHONUTF8": "1", "MECHANIC_DB": str(directory / f"{port}.sqlite3"),
           "TEAM_DB_PATH": str(directory / f"{port}-team.sqlite3"), **overrides}
    python = PYTHON
    if os.name == "nt":
        python = sys._base_executable
        env["PYTHONPATH"] = str(ROOT / ".venv" / "Lib" / "site-packages")
    log_path = directory / f"{port}.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [str(python), "-m", "uvicorn", "mechanic.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--no-access-log"], cwd=ROOT, env=env, stdout=log, stderr=log,
        )
        try:
            deadline = perf_counter() + 15
            while process.poll() is None and perf_counter() < deadline:
                try:
                    response = httpx.get(base + "/health", timeout=0.4, trust_env=False)
                    if response.is_success:
                        break
                except httpx.RequestError:
                    sleep(0.2)
            else:
                raise RuntimeError(log_path.read_text(encoding="utf-8"))
            yield base
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


@contextmanager
def slow_mock():
    requests = []
    receipts = []

    class Delay(BaseHTTPRequestHandler):
        def do_POST(self):
            port = {"/diagnose": 8001, "/tickets": 8002}[self.path]
            body = self.rfile.read(int(self.headers["Content-Length"]))
            result = httpx.post(f"http://127.0.0.1:{port}{self.path}", content=body,
                                headers={"Content-Type": "application/json"}, trust_env=False)
            requests.append((self.path, result.status_code))
            if self.path == "/tickets" and result.status_code == 201:
                receipts.append(result.json()["ticket_id"])
            sleep(1)
            try:
                self.send_response(result.status_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(result.content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Delay) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests, receipts
        finally:
            server.shutdown()
            thread.join(5)


def vehicle_option(page, vehicle):
    return page.locator("#vehicle-options").get_by_role("option", name=vehicle)


def pick(page, vehicle, session, *, touch=False):
    field = page.locator("#vehicle")
    field.fill(vehicle)
    match = vehicle_option(page, vehicle)
    if touch:
        match.tap()
    else:
        match.click()
    expect(field).to_have_value(vehicle)
    expect(field).to_have_attribute("aria-expanded", "false")
    page.locator("#session").select_option(session)
    expect(page.locator("#session")).to_have_value(session)


def diagnose(page, title):
    """Selection starts the request; this helper only waits for its result."""
    expect(page.locator("#diagnose")).to_have_count(0)
    expect(page.locator("#result-title")).to_have_text(title)
    if title in {"Обнаружено отклонение", "Отклонений по записи не обнаружено", "Запись не удалось проверить"}:
        expect(page.locator("#chat-context")).to_contain_text(page.locator("#session").input_value())


def verdict_entry(page, status, summary):
    entry = page.locator("#messages .diagnosis-message")
    expect(entry).to_have_count(1)
    expect(entry).to_have_attribute("data-status", status)
    expect(entry).to_contain_text(summary)
    return entry


def ask(page, text, expected, keyboard=False):
    message = page.locator("#question")
    message.fill(text)
    if keyboard:
        message.press("Enter")
    else:
        page.locator("#send").click()
    expect(page.locator("#messages .bubble:not(.user)").last).to_contain_text(expected)


def prepare_ticket_button(page):
    return page.locator("#result").get_by_role("button", name="Подготовить заявку", exact=True)


def draft(page, *, from_result=False):
    if from_result:
        prepare_ticket_button(page).click()
        expect(page.locator("#ticket-title")).to_be_focused()
    else:
        ask(page, "Создай заявку", "Черновик подготовлен")
    expect(page.locator("#ticket-title")).to_have_text("Черновик дефектовки")
    expect(page.get_by_role("button", name="Подтвердить и отправить")).to_be_disabled()


def submit(page):
    page.get_by_label("Подтверждаю выбранный приоритет").check()
    page.get_by_role("button", name="Подтвердить и отправить").click()


def copy_ticket_number(page, base, number, *, touch=False):
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
    button = page.get_by_role("button", name="Скопировать номер", exact=True)
    if touch:
        button.tap()
    else:
        button.click()
    expect(page.locator("#copy-ticket-status")).to_have_text("Номер скопирован")
    assert page.evaluate("navigator.clipboard.readText()") == number


def pdf_source(page, path, expected_page, phrase, *, original=False, section="#messages .bubble:not(.diagnosis-message)"):
    source = page.locator(section).locator(f'a[href="{path}"]').first
    origin = page.url
    source.scroll_into_view_if_needed()
    offset = source.bounding_box()["y"]
    source.click()
    expect(page.locator(".source-position")).to_contain_text(f"Страница PDF {expected_page} из")
    expect(page.locator("img.source-image")).to_be_visible()
    expect(page.locator("img.source-image")).to_have_js_property("complete", True)
    link = page.get_by_role("link", name="Открыть оригинал PDF").get_attribute("href")
    assert link.endswith(f"#page={expected_page}")
    if original:
        response = page.request.get(urljoin(page.url, link))
        assert response.ok and "application/pdf" in response.headers["content-type"]
        pdf = PdfReader(BytesIO(response.body()))
        assert phrase in " ".join(pdf.pages[expected_page - 1].extract_text().split())
    partial = page.get_by_role("link", name=f"Открыть только страницу {expected_page}").get_attribute("href")
    response = page.request.get(urljoin(page.url, partial))
    reader = PdfReader(BytesIO(response.body()))
    assert len(reader.pages) == 1 and phrase in " ".join(reader.pages[0].extract_text().split())
    page.get_by_role("link", name="К проверке", exact=True).click()
    page.wait_for_url(origin)
    expect(page.locator("#question")).to_be_enabled()
    expect(source).to_be_focused()
    expect(source).to_be_in_viewport()
    assert abs(source.bounding_box()["y"] - offset) <= 8


def mechanic_ux_checks(page, results):
    risk = page.locator("#result .risk-line")
    checks = page.locator("#diagnosis-checks")
    expect(risk).to_be_visible()
    expect(checks).to_be_visible()
    assert risk.bounding_box()["y"] < checks.bounding_box()["y"]

    requests = []

    def chat_request(request):
        if request.method == "POST" and request.url.endswith("/api/chat"):
            requests.append(request)

    page.on("request", chat_request)
    question = page.locator("#question")
    try:
        note = "Манометр ещё не подключал."
        question.fill(note)
        page.locator("#reply-options").get_by_role("button", name="После прогрева", exact=True).click()
        expect(question).to_have_value(note + "\nДвигатель прогрет.")
        page.locator("#reply-options").get_by_role("button", name="На холодном", exact=True).click()
        expect(question).to_have_value(note + "\nДвигатель холодный.")
        assert not requests, "Быстрый ответ не должен отправляться без подтверждения механика"

        ask(page, OIL_QUESTION, "400–700 кПа", keyboard=True)
        assert len(requests) == 1
        card = page.locator("#messages .bubble:not(.user)").last.locator(".check-card").filter(has_text="400–700 кПа")
        expect(card.locator(".check-instructions")).to_contain_text("На прогретом двигателе")
        expect(card.locator(".check-instructions")).to_contain_text("2100 об/мин")
        expect(card.locator(".check-instructions")).to_contain_text("После длительной эксплуатации")
        question.fill("Снял показание: 350 кПа.")
        card.get_by_role("button", name="Сообщить результат", exact=True).click()
        composed = question.input_value()
        assert composed.startswith("Снял показание: 350 кПа.\n")
        assert "PDF, стр. 19" in composed
        assert "400" not in composed and "700" not in composed and "100" not in composed
        assert len(requests) == 1, "Подстановка результата не должна вызывать агента"
        pdf_source(page, "/sources/yamz236-re/19", 19, "400-700 кПа", original=True,
                   section="#messages .check-card")
        expect(question).to_have_value(composed)
        question.fill("")
    finally:
        page.remove_listener("request", chat_request)
    results.append({"pair": OIL, "step": "Риск перед действием, проверяемые быстрые ответы и возврат из источника",
                    "result": "passed"})


def auto_report_checks(browser, directory, results):
    """One request for a fresh pair; navigation restores saved reports."""
    with stand(0, directory) as base:
        context = browser.new_context(viewport={"width": 1280, "height": 850})
        page = context.new_page()
        requests = []
        page.on("request", lambda request: requests.append(request.post_data_json)
                if request.method == "POST" and request.url.endswith("/api/diagnose") else None)
        page.goto(base)
        expect(page.locator("#diagnose")).to_have_count(0)
        page.locator("#vehicle").fill(OIL[0])
        vehicle_option(page, OIL[0]).click()
        expect(page.locator("#session")).to_be_enabled()
        assert not requests, "Выбор только машины не должен запускать диагностику"
        page.locator("#session").select_option(OIL[1])
        diagnose(page, "Обнаружено отклонение")
        assert requests == [{"vehicle_id": OIL[0], "session_id": OIL[1]}]
        verdict_entry(page, "anomaly", "Обнаружено отклонение")

        page.reload()
        diagnose(page, "Обнаружено отклонение")
        assert len(requests) == 1, "Обновление страницы должно восстановить отчёт без нового ML-вызова"
        page.locator("#session").select_option(NORMAL[1])
        diagnose(page, "Отклонений по записи не обнаружено")
        assert len(requests) == 2
        page.locator("#session").select_option(OIL[1])
        diagnose(page, "Обнаружено отклонение")
        assert len(requests) == 2, "Возврат к сохранённой сессии не должен повторять диагностику"

        page.goto(base + f"/?vehicle_id={FAILED[0]}&session_id={FAILED[1]}")
        diagnose(page, "Запись не удалось проверить")
        assert len(requests) == 3, "Свежая пара из URL должна сформировать отчёт автоматически"
        expect(prepare_ticket_button(page)).to_have_count(0)
        context.close()
    results.append({"pair": OIL, "step": "Автоотчёт при выборе и из URL, без повторного ML при восстановлении", "result": "passed"})


def desktop_checks(browser, base, out, results):
    context = browser.new_context(viewport={"width": 1280, "height": 850}, locale="ru-RU")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base)
    field = page.get_by_role("combobox", name="Транспортное средство")
    field.fill("ямз 236")
    expect(vehicle_option(page, OIL[0])).to_contain_text("ЯМЗ-236М2")
    expect(vehicle_option(page, CHAIN[0])).to_have_count(0)
    page.screenshot(path=out / "vehicle-picker.png")
    field.fill("нет такого тс")
    expect(page.get_by_text("ТС не найдены")).to_be_visible()
    expect(page.locator("#session")).to_be_disabled()
    field.fill("Е549")
    field.press("ArrowDown")
    expect(vehicle_option(page, OIL[0])).to_have_attribute("aria-selected", "true")
    field.press("Enter")
    expect(field).to_have_value(OIL[0])
    expect(field).to_have_attribute("aria-expanded", "false")
    page.locator("#session").select_option(OIL[1])
    expect(page.locator("#session")).to_be_enabled()
    diagnose(page, "Обнаружено отклонение")
    expect(page.locator("#context > #journal-panel")).to_be_visible()
    field.fill("P456")
    expect(vehicle_option(page, FAILED[0])).to_be_visible()
    expect(page.locator("#session")).to_be_disabled()
    expect(page.locator("#chat-context")).to_contain_text(OIL[1])
    field.press("Escape")
    expect(field).to_have_value(OIL[0])
    expect(page.locator("#session")).to_have_value(OIL[1])
    expect(page.locator("#session")).to_be_enabled()
    results.append({"pair": OIL, "step": "Поиск ТС по двигателю и номеру, выбор клавиатурой и отмена", "result": "passed"})
    expect(page.locator("#result")).to_contain_text("Высокий риск")
    expect(page.locator("#result")).to_contain_text("0,84")
    expect(page.locator("#result")).to_contain_text("F02")
    entry = verdict_entry(page, "anomaly", "Обнаружено отклонение")
    expect(entry).to_contain_text("Проверьте количество масла")
    expect(entry.locator('a[href="/sources/yamz236-re/19"]')).to_be_visible()
    page.reload()
    verdict_entry(page, "anomaly", "Обнаружено отклонение")
    manual = page.locator("#result .ref-manual")
    expect(manual).to_have_count(1)
    expect(manual.locator(".ref-title")).to_have_text("ЯМЗ-236. Руководство по эксплуатации")
    expect(manual.locator('a[href="/sources/yamz236-re/19"]')).to_have_text("PDF, стр. 19 — нормы давления")
    expect(manual.locator('a[href="/sources/yamz236-re/53"]')).to_have_text("PDF, стр. 53 — причины падения")
    page.screenshot(path=out / "desktop.png")
    mechanic_ux_checks(page, results)
    expect(page.locator("#messages")).to_contain_text("После длительной эксплуатации")
    page.get_by_text("Возможные работы", exact=True).click()
    expect(page.get_by_text("Промыть заборник.", exact=False)).to_be_visible()
    pdf_source(page, "/sources/yamz236-re/53", 53, "Пониженное давление масла в системе смазки",
               original=True, section="#result")
    ask(page, "Можно ли своим ходом перегнать машину до бокса?", "Данных для допуска к перегону")
    expect(page.locator("#messages .bubble:not(.user)").last).to_contain_text("2100 об/мин")
    ask(page, "Манометр показал 90 кПа. Это допустимо?", "уточните обороты")
    ask(page, "Где взять прошивку для магнитолы?", "нет подтверждённой страницы")
    results.append({"pair": OIL, "step": "Источники, перегон и вопрос вне базы", "result": "passed"})

    draft(page, from_result=True)
    expect(page.locator(".ticket-badge")).to_have_count(0)
    expect(page.locator("#copy-ticket-number")).to_have_count(0)
    page.locator("#urgency").select_option("medium")
    note = page.locator("#conclusion").input_value() + " Замер запланирован."
    page.locator("#conclusion").fill(note)
    expect(page.locator("#draft-save-status")).to_have_text("Есть несохранённые правки")
    pdf_source(page, "/sources/yamz236-re/19", 19, "400-700 кПа", section="#ticket-panel")
    expect(page.locator("#conclusion")).to_have_value(note)
    expect(page.locator("#urgency")).to_have_value("medium")
    expect(page.locator("#draft-save-status")).to_have_text("Есть несохранённые правки")
    page.get_by_role("button", name="Сохранить изменения").click()
    expect(page.locator("#draft-save-status")).to_have_text("Сохранено")
    page.reload()
    expect(page.locator("#conclusion")).to_have_value(note)
    expect(page.locator("#urgency")).to_have_value("medium")
    expect(page.locator("#draft-save-status")).to_have_text("Сохранено")
    submit(page)
    ticket = page.locator(".ticket-badge")
    expect(ticket).to_be_visible()
    number = ticket.inner_text()
    page.reload()
    expect(ticket).to_have_text(number)
    page.locator("#result").get_by_role("button", name="Открыть заявку", exact=True).click()
    expect(page.locator("#ticket-title")).to_be_focused()
    assert not page.get_by_role("button", name="Подтвердить и отправить").count()
    copy_ticket_number(page, base, number)
    query = {"vehicle_id": OIL[0], "session_id": OIL[1]}
    stored = page.request.get(base + "/api/state", params=query).json()
    edits = {key: stored["draft"][key] for key in ("conclusion", "urgency", "operations", "spare_parts")}
    resend = page.request.post(base + "/api/ticket", data={**query, "edits": edits,
                               "draft_id": stored["draft_id"], "revision": stored["draft_revision"],
                               "priority_confirmed": True}).json()
    assert resend["ticket"]["ticket_id"] == number
    assert sum(item["tool"] == "mock_toir.tickets" for item in resend["calls"]) == 1
    results.append({"pair": OIL, "step": "Редактирование, подтверждение, восстановление и копирование номера",
                    "result": number})

    page.locator("#session").select_option(NORMAL[1])
    diagnose(page, "Отклонений по записи не обнаружено")
    page.reload()
    expect(verdict_entry(page, "normal", "Значимых отклонений не обнаружено")).to_contain_text(
        "Продолжайте штатный контроль двигателя")
    expect(prepare_ticket_button(page)).to_have_count(0)
    ask(page, "Что проверить сначала?", "S-0010")
    assert not page.locator("#messages").get_by_text("низкое давление", exact=False).count()
    page.locator("#session").select_option(OIL[1])
    expect(ticket).to_have_text(number)
    results.append({"pair": NORMAL, "step": "Смена сессии и отдельный диалог", "result": "passed"})

    pick(page, *FAILED)
    diagnose(page, "Запись не удалось проверить")
    page.reload()
    failed = verdict_entry(page, "check_failed", "Сбой проверки записи")
    expect(failed).to_contain_text("перегрузка микрофона")
    expect(failed).to_contain_text("Повторите запись")
    expect(prepare_ticket_button(page)).to_have_count(0)
    expect(page.locator("#result")).to_contain_text("перегрузка микрофона")
    expect(page.locator("#result")).to_contain_text("Повторите запись")
    assert page.locator("#result").get_by_text("Нет значения", exact=True).count() == 2
    results.append({"pair": FAILED, "step": "Перегрузка микрофона и пропуски оценок", "result": "passed"})

    pick(page, *WARNING)
    diagnose(page, "Обнаружено отклонение")
    expect(page.locator("#result .alert")).to_have_count(2)
    expect(page.locator("#result .alert").first).to_contain_text("Низкая уверенность (0,48)")
    expect(page.locator("#result .alert").last).to_contain_text("Запись с помехами")
    results.append({"pair": WARNING, "step": "Раздельные предупреждения", "result": "passed"})
    pick(page, "P854BT78", "S-0021")
    diagnose(page, "Обнаружено отклонение")
    expect(page.locator("#result .alert")).to_have_count(1)
    expect(page.locator("#result .alert")).to_contain_text("Запись с помехами")
    results.append({"pair": ("P854BT78", "S-0021"), "step": "Помехи при уверенности 0,66", "result": "passed"})

    pick(page, *CHAIN)
    diagnose(page, "Обнаружено отклонение")
    ask(page, "Ровный шум спереди на средних оборотах. Как различить цепь и гидронатяжитель?", "удлинение")
    expect(page.locator("#messages")).to_contain_text("отдельная возможная причина")
    pdf_source(page, "/sources/zmz409/107", 107, "неисправен гидронатяжитель цепи", original=True)
    results.append({"pair": CHAIN, "step": "Цепь и натяжитель, оригинал стр. 107", "result": "passed"})

    pick(page, *MISMATCH)
    diagnose(page, "Обнаружено отклонение")
    expect(prepare_ticket_button(page)).to_have_count(0)
    expect(page.locator("#result")).to_contain_text("указан как бензиновый")
    ask(page, "Есть ли смысл проверять дизельную топливоподачу?", "Уточните фактическую модификацию")
    ask(page, "Создай заявку", "указан как бензиновый")
    expect(page.locator("#ticket-panel")).to_be_hidden()
    page.goto(base + "/?vehicle_id=E549YT198&session_id=S-0003")
    expect(page.locator("#result-title")).to_have_text("Проверьте пару ТС и сессии")
    results.append({"pair": MISMATCH, "step": "Несовпадение топлива и неверная пара из URL", "result": "passed"})
    viewer = context.new_page()
    viewer.goto(base + "/sources/yamz236-re/19")
    direct_return = viewer.get_by_role("link", name="К проверке", exact=True)
    expect(direct_return).to_have_attribute("href", "/")
    direct_return.click()
    viewer.wait_for_url(base + "/")
    expect(viewer.locator("#vehicle")).to_have_value("")
    viewer.close()
    assert not errors, errors
    context.close()


def mobile_and_race(playwright, browser, base, directory, out, results):
    mobile = browser.new_context(**playwright.devices["iPhone 13"])
    page = mobile.new_page()
    with stand(0, directory) as mobile_base:
        page.goto(mobile_base)
        expect(page.locator("#context-summary")).to_be_hidden()
        pick(page, *OIL, touch=True)
        diagnose(page, "Обнаружено отклонение")
        expect(page.locator("#context-summary")).to_be_visible()
        expect(page.locator("#context-summary")).to_contain_text(OIL[0])
        expect(page.locator("#context-summary")).to_contain_text("ЯМЗ-236М2")
        expect(page.locator("#selection")).to_be_hidden()
        expect(page.locator("#workarea > #journal-panel:last-child")).to_be_visible()
        page.screenshot(path=out / "mobile.png")
        assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
        page.locator("#edit-context").tap()
        expect(page.locator("#selection")).to_be_visible()
        expect(page.locator("#vehicle")).to_have_value(OIL[0])
        expect(page.locator("#session")).to_have_value(OIL[1])
        page.locator("#collapse-context").tap()
        expect(page.locator("#selection")).to_be_hidden()
        ask(page, OIL_QUESTION, "400–700 кПа")
        page.locator("#ask-question").tap()
        expect(page.locator("#question")).to_be_focused()
        expect(page.locator("#question")).to_be_in_viewport()
        pdf_source(page, "/sources/yamz236-re/19", 19, "400-700 кПа")
        pdf_source(page, "/sources/yamz236-re/53", 53, "Пониженное давление масла в системе смазки")
        draft(page, from_result=True)
        submit(page)
        ticket = page.locator(".ticket-badge")
        expect(ticket).to_be_visible()
        copy_ticket_number(page, mobile_base, ticket.inner_text(), touch=True)
        results.append({"pair": OIL, "step": "390 CSS px, выбор ТС, переход к вопросу, ссылки и копирование номера",
                        "result": "passed"})
    mobile.close()

    # A fresh state is essential: the desktop scenario already cached OIL.
    with stand(0, directory) as race_base:
        context = browser.new_context()
        page = context.new_page()
        delayed = []

        def delay_oil(route):
            if route.request.post_data_json["session_id"] == OIL[1]:
                delayed.append(route)
            else:
                route.continue_()

        page.route("**/api/diagnose", delay_oil)
        page.goto(race_base)
        pick(page, *OIL)
        expect(page.locator("#result-title")).to_have_text("Запрашиваем диагностику")
        page.locator("#session").select_option(NORMAL[1])
        diagnose(page, "Отклонений по записи не обнаружено")
        assert len(delayed) == 1
        with page.expect_response(lambda response: response.url.endswith("/api/diagnose") and
                                  response.request.post_data_json["session_id"] == OIL[1]):
            delayed.pop().continue_()
        page.wait_for_timeout(150)
        expect(page.locator("#result-title")).to_have_text("Отклонений по записи не обнаружено")
        expect(page.locator("#chat-context")).to_contain_text(NORMAL[1])
        assert OIL[1] not in page.locator("#result").inner_text()
        results.append({"pair": OIL, "step": "Опоздавший автоматический HTTP-ответ после перехода к S-0010", "result": "passed"})
        context.close()


def stale_draft_across_tabs(browser, directory, results):
    with stand(0, directory) as base:
        context = browser.new_context()
        editor = context.new_page()
        editor.goto(base)
        pick(editor, *OIL)
        diagnose(editor, "Обнаружено отклонение")
        draft(editor, from_result=True)

        peer = context.new_page()
        peer.goto(editor.url)
        expect(peer.locator("#operations")).to_be_editable()
        initial = editor.locator("#operations").input_value()
        local = initial + "\nНесохранённая пометка из первой вкладки."
        saved = initial + "\nЗамер из второй вкладки."
        editor.locator("#operations").fill(local)
        expect(editor.locator("#draft-save-status")).to_have_text("Есть несохранённые правки")
        editor.locator("#operations").fill(initial)
        expect(editor.locator("#draft-save-status")).to_have_text("Сохранено")
        editor.locator("#operations").fill(local)
        peer.locator("#operations").fill(saved)
        peer.get_by_role("button", name="Сохранить изменения").click()
        expect(peer.locator("#draft-save-status")).to_have_text("Сохранено")

        editor.locator("#session").select_option(NORMAL[1])
        expect(editor.locator("#ticket-panel")).to_be_hidden()
        editor.locator("#session").select_option(OIL[1])
        expect(editor.locator("#draft-conflict")).to_be_visible()
        comparison = editor.locator("#draft-comparison")
        expect(comparison.locator("[data-field]")).to_have_count(1)
        works = comparison.locator('[data-field="operations"]')
        expect(works).to_contain_text("Ваш вариант")
        expect(works).to_contain_text("Сохранённый вариант")
        expect(works.locator("#operations")).to_have_value(local)
        expect(works.locator("#operations")).not_to_be_editable()
        expect(works.locator(".draft-conflict-saved")).to_contain_text("Замер из второй вкладки.")
        expect(editor.get_by_role("button", name="Сохранить изменения")).to_have_count(0)
        expect(editor.get_by_role("button", name="Подтвердить и отправить")).to_have_count(0)
        editor.locator("#use-saved-draft").click()
        expect(editor.locator("#draft-conflict")).to_be_hidden()
        expect(editor.locator("#operations")).to_have_value(saved)
        expect(editor.locator("#draft-save-status")).to_have_text("Сохранено")

        pair = {"vehicle_id": OIL[0], "session_id": OIL[1]}
        base_version = editor.request.get(base + "/api/state", params=pair).json()
        editor.locator("#operations").fill(local)
        latest = saved + "\nПодтверждён новый план."
        peer.locator("#operations").fill(latest)
        peer.get_by_role("button", name="Сохранить изменения").click()
        expect(peer.locator("#draft-save-status")).to_have_text("Сохранено")

        deliveries = []
        editor.on("request", lambda req: deliveries.append(req) if req.url.endswith("/api/ticket") else None)
        with editor.expect_response("**/api/ticket") as response:
            submit(editor)
        conflict = response.value
        assert conflict.status == 409
        assert conflict.json()["error"]["code"] == "stale_draft"
        submission = conflict.request.post_data_json
        assert submission["draft_id"] == base_version["draft_id"]
        assert submission["revision"] == base_version["draft_revision"]
        expect(editor.locator("#draft-conflict")).to_be_visible()
        expect(editor.locator("#operations")).to_have_value(local)
        expect(works.locator(".draft-conflict-saved")).to_contain_text("Подтверждён новый план.")
        expect(editor.get_by_role("button", name="Подтвердить и отправить")).to_have_count(0)
        editor.locator("#use-saved-draft").click()
        expect(editor.locator("#operations")).to_have_value(latest)
        expect(editor.locator(".ticket-badge")).to_have_count(0)
        assert len(deliveries) == 1
        current = editor.request.get(base + "/api/state", params=pair).json()
        assert current["draft"]["operations"] == latest.splitlines()
        assert current["ticket"]["status"] == "draft"
        assert not any(call["tool"] == "mock_toir.tickets" for call in current["calls"])
        context.close()
    results.append({"pair": OIL, "step": "Конфликт черновика между вкладками и при смене сессии",
                    "result": "passed"})


def failure_checks(browser, directory, results):
    with stand(0, directory, MOCK_ML_URL="http://127.0.0.1:65534") as base:
        page = browser.new_page()
        failures = []
        page.on("request", lambda request: failures.append(request)
                if request.method == "POST" and request.url.endswith("/api/diagnose") else None)
        page.goto(base)
        pick(page, *OIL)
        diagnose(page, "Сервис диагностики недоступен")
        assert len(failures) == 1
        expect(page.locator("#retry-diagnosis")).to_be_visible()
        with page.expect_response("**/api/diagnose"):
            page.locator("#retry-diagnosis").click()
        diagnose(page, "Сервис диагностики недоступен")
        assert len(failures) == 2, "Повторный запрос выполняется только по нажатию Retry"
        page.close()
    with stand(0, directory, MOCK_TOIR_URL="http://127.0.0.1:65534") as base:
        page = browser.new_page()
        page.goto(base)
        pick(page, *OIL)
        diagnose(page, "Обнаружено отклонение")
        draft(page)
        before = page.locator("#conclusion").input_value() + " Повторный осмотр."
        page.locator("#conclusion").fill(before)
        submit(page)
        expect(page.locator("#ticket-panel .alert")).to_contain_text("ТОиР")
        expect(page.locator("#conclusion")).to_be_editable()
        page.reload()
        expect(page.locator("#conclusion")).to_have_value(before)
        page.close()
    results.append({"pair": OIL, "step": "Недоступны Mock ML и Mock ТОиР, черновик после обновления", "result": "passed"})
    with slow_mock() as (proxy, calls, receipts):
        with stand(0, directory, MOCK_ML_URL=proxy, MOCK_API_TIMEOUT="0.30") as base:
            page = browser.new_page()
            page.goto(base)
            pick(page, *OIL)
            diagnose(page, "Тайм-аут диагностики")
            page.close()
        with stand(0, directory, MOCK_TOIR_URL=proxy, MOCK_API_TIMEOUT="0.30") as base:
            page = browser.new_page()
            page.goto(base)
            pick(page, *OIL)
            diagnose(page, "Обнаружено отклонение")
            draft(page)
            submit(page)
            expect(page.locator("#ticket-panel")).to_contain_text("Результат отправки неизвестен")
            page.reload()
            expect(page.locator("#ticket-panel")).to_contain_text("Результат отправки неизвестен")
            assert not page.get_by_role("button", name="Подтвердить и отправить").count()
            expect(page.locator("#copy-ticket-number")).to_have_count(0)
            catalog = page.request.get(base + "/api/catalog").json()
            if catalog["backend"] == "team":
                number, = receipts
                save_number = page.get_by_role("button", name="Сохранить номер", exact=True)
                expect(save_number).to_be_disabled()
                page.locator("#reconcile-ticket-id").fill(number)
                expect(save_number).to_be_disabled()
                page.locator("#reconcile-verified").check()
                save_number.click()
                expect(page.locator(".ticket-badge")).to_have_text(number)
                page.reload()
                expect(page.locator(".ticket-badge")).to_have_text(number)
                assert not page.get_by_role("button", name="Подтвердить и отправить").count()
                copy_ticket_number(page, base, number)
            assert calls == [("/diagnose", 200), ("/tickets", 201)], calls
            page.close()
    results.append({"pair": OIL, "step": "Тайм-аут ML, потеря ответа ТОиР и сверка номера без повторной отправки",
                    "result": "passed"})


def record(browser, base, folder):
    context = browser.new_context(viewport={"width": 1280, "height": 810}, locale="ru-RU",
                                  record_video_dir=folder, record_video_size={"width": 1280, "height": 810})
    page = context.new_page()
    video = page.video
    started = perf_counter()
    times = {}

    def until(seconds):
        page.wait_for_timeout(max(0, (seconds - (perf_counter() - started)) * 1000))

    def checkpoint(name):
        times[name] = round(perf_counter() - started, 2)

    try:
        page.goto(base)
        until(1.7)
        page.locator("#vehicle").fill(OIL[0])
        until(3)
        vehicle_option(page, OIL[0]).click()
        until(4.5)
        page.locator("#session").select_option(OIL[1])
        until(6)
        checkpoint("launch")
        expect(page.locator("#result-title")).to_have_text("Обнаружено отклонение")
        checkpoint("anomaly")
        until(10)
        page.locator("#question").press_sequentially(OIL_QUESTION, delay=23)
        page.locator("#send").click()
        expect(page.locator("#messages")).to_contain_text("400–700 кПа")
        checkpoint("answer")
        until(17.5)
        page.locator('#messages a[href="/sources/yamz236-re/19"]').first.click()
        expect(page.locator("img.source-image")).to_be_visible()
        checkpoint("page_19")
        until(20)
        page.evaluate("window.scrollTo(0, 390)")
        until(24)
        page.get_by_role("link", name="К проверке", exact=True).click()
        expect(page.locator("#question")).to_be_enabled()
        until(27)
        draft(page, from_result=True)
        checkpoint("draft")
        until(34)
        page.get_by_label("Подтверждаю выбранный приоритет").check()
        until(37)
        page.get_by_role("button", name="Подтвердить и отправить").click()
        expect(page.locator(".ticket-badge")).to_be_visible()
        checkpoint("ticket")
        times["ticket_id"] = page.locator(".ticket-badge").inner_text()
        until(46)
        page.evaluate("window.scrollTo(0, 0)")
        page.locator("#vehicle").fill(FAILED[0])
        vehicle_option(page, FAILED[0]).click()
        until(48)
        page.locator("#session").select_option(FAILED[1])
        until(50)
        diagnose(page, "Запись не удалось проверить")
        expect(page.locator("#result")).to_contain_text("Повторите запись")
        checkpoint("repeat_recording")
        until(55)
    finally:
        context.close()
    times["capture_elapsed"] = round(perf_counter() - started, 2)
    assert times["launch"] <= 10 and times["page_19"] <= 30 and times["ticket"] <= 47
    assert times["repeat_recording"] <= 60 and times["capture_elapsed"] <= 60
    return video.path(), times


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--expect-team", action="store_true")
    args = parser.parse_args()
    RUNTIME.mkdir(exist_ok=True)
    out = RUNTIME / "qa"
    out.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=RUNTIME, prefix="browser-") as scratch, sync_playwright() as playwright:
        directory = Path(scratch)
        browser = playwright.chromium.launch(channel="chromium", headless=True)
        with stand(args.port, directory) as base:
            catalog = httpx.get(base + "/api/catalog", trust_env=False).json()
            if args.expect_team:
                assert catalog["backend"] == "team" and catalog["manual_search"] == "team", catalog["backend"]
            print("Backend:", catalog["backend"], "manuals:", catalog["manual_search"], flush=True)
            if args.record:
                video, timings = record(browser, base, directory)
                destination = ROOT / "output"
                destination.mkdir(exist_ok=True)
                ffmpeg = subprocess.check_output(
                    [str(PYTHON), "-c", "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"],
                    text=True,
                ).strip()
                subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-an",
                                "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                                str(destination / "Цифровой механик — демо.mp4")], check=True)
                (out / "video-timings.json").write_text(json.dumps(timings, ensure_ascii=False, indent=2), encoding="utf-8")
                print(timings, flush=True)
            else:
                results = []
                auto_report_checks(browser, directory, results)
                desktop_checks(browser, base, out, results)
                mobile_and_race(playwright, browser, base, directory, out, results)
                stale_draft_across_tabs(browser, directory, results)
                failure_checks(browser, directory, results)
                (out / "browser-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"{len(results)} browser checks passed", flush=True)
        browser.close()


if __name__ == "__main__":
    main()
