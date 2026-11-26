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

    class Delay(BaseHTTPRequestHandler):
        def do_POST(self):
            port = {"/diagnose": 8001, "/tickets": 8002}[self.path]
            body = self.rfile.read(int(self.headers["Content-Length"]))
            result = httpx.post(f"http://127.0.0.1:{port}{self.path}", content=body,
                                headers={"Content-Type": "application/json"}, trust_env=False)
            requests.append((self.path, result.status_code))
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
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join(5)


def pick(page, vehicle, session):
    page.locator("#vehicle").select_option(vehicle)
    page.locator("#session").select_option(session)
    expect(page.locator("#diagnose")).to_be_enabled()


def diagnose(page, title):
    page.locator("#diagnose").click()
    expect(page.locator("#result-title")).to_have_text(title)


def ask(page, text, expected, keyboard=False):
    message = page.locator("#question")
    message.fill(text)
    if keyboard:
        message.press("Enter")
    else:
        page.locator("#send").click()
    expect(page.locator("#messages .bubble:not(.user)").last).to_contain_text(expected)


def draft(page):
    ask(page, "Создай заявку", "Черновик подготовлен")
    expect(page.locator("#ticket-title")).to_have_text("Черновик дефектовки")
    expect(page.get_by_role("button", name="Подтвердить и отправить")).to_be_disabled()


def submit(page):
    page.get_by_label("Подтверждаю выбранный приоритет").check()
    page.get_by_role("button", name="Подтвердить и отправить").click()


def pdf_source(page, path, expected_page, phrase, original=False, in_chat=True):
    where = page.locator("#messages" if in_chat else "#result")
    source = where.locator(f'a[href="{path}"]').first
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
    page.go_back()
    expect(page.locator("#question")).to_be_enabled()


def desktop_checks(browser, base, out, results):
    context = browser.new_context(viewport={"width": 1280, "height": 850}, locale="ru-RU")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base)
    pick(page, *OIL)
    diagnose(page, "Обнаружено отклонение")
    expect(page.locator("#result")).to_contain_text("Высокий риск")
    expect(page.locator("#result")).to_contain_text("0,84")
    expect(page.locator("#result")).to_contain_text("F02")
    page.screenshot(path=out / "desktop.png")
    ask(page, OIL_QUESTION, "400–700 кПа", keyboard=True)
    expect(page.locator("#messages")).to_contain_text("После длительной эксплуатации")
    page.get_by_text("Возможные работы", exact=True).click()
    expect(page.get_by_text("Промыть заборник.", exact=False)).to_be_visible()
    pdf_source(page, "/sources/yamz236-re/19", 19, "400-700 кПа", original=True)
    pdf_source(page, "/sources/yamz236-re/53", 53, "Пониженное давление масла в системе смазки", original=True)
    ask(page, "Можно ли своим ходом перегнать машину до бокса?", "Данных для допуска к перегону")
    expect(page.locator("#messages .bubble:not(.user)").last).to_contain_text("2100 об/мин")
    ask(page, "Манометр показал 90 кПа. Это допустимо?", "уточните обороты")
    ask(page, "Где взять прошивку для магнитолы?", "нет подтверждённой страницы")
    results.append({"pair": OIL, "step": "Источники, перегон и вопрос вне базы", "result": "passed"})

    draft(page)
    page.locator("#urgency").select_option("medium")
    note = page.locator("#conclusion").input_value() + " Замер запланирован."
    page.locator("#conclusion").fill(note)
    page.get_by_role("button", name="Сохранить изменения").click()
    expect(page.get_by_text("Изменения сохранены", exact=True)).to_be_visible()
    page.reload()
    expect(page.locator("#conclusion")).to_have_value(note)
    expect(page.locator("#urgency")).to_have_value("medium")
    submit(page)
    ticket = page.locator(".ticket-badge")
    expect(ticket).to_be_visible()
    number = ticket.inner_text()
    page.reload()
    expect(ticket).to_have_text(number)
    assert not page.get_by_role("button", name="Подтвердить и отправить").count()
    query = {"vehicle_id": OIL[0], "session_id": OIL[1]}
    stored = page.request.get(base + "/api/state", params=query).json()
    edits = {key: stored["draft"][key] for key in ("conclusion", "urgency", "operations", "spare_parts")}
    resend = page.request.post(base + "/api/ticket", data={**query, "edits": edits,
                               "revision": stored["draft_revision"], "priority_confirmed": True}).json()
    assert resend["ticket"]["ticket_id"] == number
    assert sum(item["tool"] == "mock_toir.tickets" for item in resend["calls"]) == 1
    results.append({"pair": OIL, "step": "Редактирование, подтверждение, восстановление", "result": number})

    page.locator("#session").select_option(NORMAL[1])
    diagnose(page, "Значимых отклонений не обнаружено")
    ask(page, "Что проверить сначала?", "S-0010")
    assert not page.locator("#messages").get_by_text("низкое давление", exact=False).count()
    page.locator("#session").select_option(OIL[1])
    expect(ticket).to_have_text(number)
    results.append({"pair": NORMAL, "step": "Смена сессии и отдельный диалог", "result": "passed"})

    pick(page, *FAILED)
    diagnose(page, "Сбой проверки записи")
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
    expect(page.locator("#result")).to_contain_text("указан как бензиновый")
    ask(page, "Есть ли смысл проверять дизельную топливоподачу?", "Уточните фактическую модификацию")
    ask(page, "Создай заявку", "указан как бензиновый")
    expect(page.locator("#ticket-panel")).to_be_hidden()
    page.goto(base + "/?vehicle_id=E549YT198&session_id=S-0003")
    expect(page.locator("#result-title")).to_have_text("Проверьте пару ТС и сессии")
    results.append({"pair": MISMATCH, "step": "Несовпадение топлива и неверная пара из URL", "result": "passed"})
    assert not errors, errors
    context.close()


def mobile_and_race(playwright, browser, base, directory, out, results):
    mobile = browser.new_context(**playwright.devices["iPhone 13"])
    page = mobile.new_page()
    with stand(0, directory) as mobile_base:
        page.goto(mobile_base)
        page.locator("#vehicle-filter").fill("E549")
        page.locator("#vehicle-filter").press("Enter")
        expect(page.locator("#vehicle")).to_be_focused()
        pick(page, *OIL)
        diagnose(page, "Обнаружено отклонение")
        page.screenshot(path=out / "mobile.png")
        assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
        ask(page, OIL_QUESTION, "400–700 кПа")
        pdf_source(page, "/sources/yamz236-re/19", 19, "400-700 кПа")
        pdf_source(page, "/sources/yamz236-re/53", 53, "Пониженное давление масла в системе смазки")
        draft(page)
        submit(page)
        expect(page.locator(".ticket-badge")).to_be_visible()
        results.append({"pair": OIL, "step": "390 CSS px, клавиатура, ссылки и номер", "result": "passed"})
    mobile.close()

    context = browser.new_context()
    page = context.new_page()
    page.goto(base)
    pick(page, *OIL)
    delayed = []
    page.route("**/api/diagnose", lambda route: delayed.append(route))
    page.locator("#diagnose").click()
    expect(page.locator("#result-title")).to_have_text("Запрашиваем диагностику")
    page.locator("#session").select_option(NORMAL[1])
    expect(page.locator("#result-title")).to_have_text("Значимых отклонений не обнаружено")
    with page.expect_response("**/api/diagnose"):
        delayed.pop().continue_()
    page.wait_for_timeout(150)
    expect(page.locator("#result-title")).to_have_text("Значимых отклонений не обнаружено")
    expect(page.locator("#chat-context")).to_contain_text(NORMAL[1])
    assert OIL[1] not in page.locator("#result").inner_text()
    results.append({"pair": OIL, "step": "Опоздавший HTTP-ответ после перехода к S-0010", "result": "passed"})
    context.close()


def failure_checks(browser, directory, results):
    with stand(0, directory, MOCK_ML_URL="http://127.0.0.1:65534") as base:
        page = browser.new_page()
        page.goto(base)
        pick(page, *OIL)
        diagnose(page, "Сервис диагностики недоступен")
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
    with slow_mock() as (proxy, calls):
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
            assert calls == [("/diagnose", 200), ("/tickets", 201)], calls
            page.close()
    results.append({"pair": OIL, "step": "Раздельный тайм-аут ML и потеря ответа после 201 от ТОиР", "result": "passed"})


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
        page.locator("#vehicle-filter").fill(OIL[0])
        until(3)
        page.locator("#vehicle").select_option(OIL[0])
        until(4.5)
        page.locator("#session").select_option(OIL[1])
        until(6)
        page.locator("#diagnose").click()
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
        page.go_back()
        expect(page.locator("#question")).to_be_enabled()
        until(27)
        page.locator("#suggestions").get_by_role("button", name="Создай заявку").click()
        expect(page.locator("#ticket-title")).to_have_text("Черновик дефектовки")
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
        page.locator("#vehicle-filter").fill(FAILED[0])
        page.locator("#vehicle").select_option(FAILED[0])
        until(48)
        page.locator("#session").select_option(FAILED[1])
        until(50)
        diagnose(page, "Сбой проверки записи")
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
                desktop_checks(browser, base, out, results)
                mobile_and_race(playwright, browser, base, directory, out, results)
                failure_checks(browser, directory, results)
                (out / "browser-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"{len(results)} browser checks passed", flush=True)
        browser.close()


if __name__ == "__main__":
    main()
