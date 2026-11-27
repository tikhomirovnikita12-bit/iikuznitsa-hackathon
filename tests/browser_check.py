"""Приёмка в чистых контекстах Chromium и запись непрерывного демо."""

import argparse
import json
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, sleep
from urllib.parse import urljoin
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
OIL = ("E549YT198", "S-0011")
NORMAL = ("E549YT198", "S-0010")
FAILED = ("P456AT178", "S-0003")
LOW_CONFIDENCE = ("M739BA198", "S-0103")
WARNING = ("O658MX98", "S-0063")
FUEL = ("M333YK47", "S-0005")
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
    token = f"{port}-{uuid4().hex[:8]}"
    env = {**os.environ, "PYTHONUTF8": "1", "MECHANIC_DB": str(directory / f"{token}.sqlite3"),
           "TEAM_DB_PATH": str(directory / f"{token}-team.sqlite3"), **overrides}
    python = PYTHON
    if os.name == "nt":
        python = sys._base_executable
        env["PYTHONPATH"] = str(ROOT / ".venv" / "Lib" / "site-packages")
    log_path = directory / f"{token}.log"
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


def vehicle_option(page, vehicle):
    return page.locator("#vehicle-options").get_by_role("option", name=vehicle)


def pick(page, vehicle, session, *, touch=False):
    if page.locator("#selection").is_hidden():
        page.locator("#edit-context").click()
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
    if page.locator("img.source-image").count():
        expect(page.locator("img.source-image")).to_be_visible()
        expect(page.locator("img.source-image")).to_have_js_property("complete", True)
    else:
        expect(page.locator("#source-preview-note")).to_be_visible()
    link = page.get_by_role("link", name="Открыть оригинал PDF").get_attribute("href")
    assert link.endswith(f"#page={expected_page}")
    if original:
        response = page.request.get(urljoin(page.url, link))
        assert response.ok and "application/pdf" in response.headers["content-type"]
        pdf = PdfReader(BytesIO(response.body()))
        assert len(pdf.pages) >= expected_page
        if phrase:
            assert phrase in " ".join(pdf.pages[expected_page - 1].extract_text().split())
    partial = page.get_by_role("link", name=f"Открыть только страницу {expected_page}").get_attribute("href")
    response = page.request.get(urljoin(page.url, partial))
    reader = PdfReader(BytesIO(response.body()))
    assert len(reader.pages) == 1
    if phrase:
        assert phrase in " ".join(reader.pages[0].extract_text().split())
    page.get_by_role("link", name="К проверке", exact=True).click()
    page.wait_for_url(origin)
    expect(page.locator("#question")).to_be_enabled()
    expect(source).to_be_focused()
    expect(source).to_be_in_viewport()
    assert abs(source.bounding_box()["y"] - offset) <= 8


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


def page_state(page, base, pair):
    response = page.request.get(base + "/api/state", params=dict(zip(("vehicle_id", "session_id"), pair)))
    assert response.ok, response.text()
    return response.json()


def open_pair(page, base, pair):
    page.goto(base + "/?" + "&".join(f"{key}={value}" for key, value in zip(("vehicle_id", "session_id"), pair)))
    title = "Запись не удалось проверить" if pair == FAILED else "Обнаружено отклонение"
    diagnose(page, title)


def admission_checks(browser, base, out, results, *, mobile=False):
    """Current policy: reviewed work, explicit warning and scoped human consent."""
    context = browser.new_context(viewport={"width": 390 if mobile else 1280, "height": 844},
                                  locale="ru-RU", is_mobile=mobile, has_touch=mobile)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        # F02 remains a documentation case; bad/low confidence/mismatch never open a ticket form.
        for pair, code in ((OIL, "procedure_unavailable"), (LOW_CONFIDENCE, "confidence_too_low"),
                           (FAILED, "recording_invalid"), (MISMATCH, "fault_engine_mismatch")):
            open_pair(page, base, pair)
            state = page_state(page, base, pair)
            assert state["admission"]["block_reason"]["code"] == code
            expect(page.locator("#result .admission-notice")).to_contain_text(state["admission"]["block_reason"]["message"])
            expect(prepare_ticket_button(page)).to_have_count(0)
            assert not any(call["tool"] == "mock_toir.tickets" for call in state["calls"])
            if pair == LOW_CONFIDENCE:
                expect(page.locator("#result")).to_contain_text("Запись с помехами")
            if pair == FAILED:
                expect(page.locator("#result")).to_contain_text("вывод о двигателе невозможен")
        results.append({"step": "Причины отказа: F02 без процедуры, confidence 0.48, bad и несовместимый F08", "mobile": mobile, "result": "passed"})

        # F08 has a real but conditional startup operation; choosing a priority isn't enough.
        open_pair(page, base, FUEL)
        draft(page, from_result=True)
        expect(page.locator("#operations")).to_have_attribute("readonly", "")
        condition = page.get_by_label("Подтверждаю условие:", exact=False)
        expect(condition).to_have_count(1)
        priority = page.get_by_label("Подтверждаю выбранный приоритет", exact=True)
        priority.check()
        expect(page.get_by_role("button", name="Подтвердить и отправить", exact=True)).to_be_disabled()
        condition.check()
        expect(priority).not_to_be_checked()
        priority.check()
        expect(page.get_by_role("button", name="Подтвердить и отправить", exact=True)).to_be_enabled()
        condition.uncheck()
        expect(page.get_by_role("button", name="Подтвердить и отправить", exact=True)).to_be_disabled()
        assert not any(call["tool"] == "mock_toir.tickets" for call in page_state(page, base, FUEL)["calls"])
        results.append({"pair": FUEL, "step": "Условие первичного пуска/длительной стоянки отдельно от приоритета", "mobile": mobile, "result": "passed"})

        # The accepted warning case has a complete reviewed procedure with four PDF pages.
        open_pair(page, base, WARNING)
        draft(page, from_result=True)
        expect(page.locator("#operations")).to_have_attribute("readonly", "")
        expect(page.locator(".priority-explanation")).to_contain_text("Предложен: Средний")
        for number in (173, 174, 175, 176):
            expect(page.locator(f'#ticket-panel a[href="/sources/yamz536-re/{number}"]').first).to_be_visible()
        priority = page.get_by_label("Подтверждаю выбранный приоритет", exact=True)
        warning = page.locator("#warning-acknowledged")
        priority.check()
        expect(page.get_by_role("button", name="Подтвердить и отправить", exact=True)).to_be_disabled()
        warning.check()
        expect(page.get_by_role("button", name="Подтвердить и отправить", exact=True)).to_be_enabled()
        page.locator("#question").fill("Уточню фактический зазор после измерения.")
        pdf_source(page, "/sources/yamz536-re/175", 175, None, original=True, section="#ticket-panel")
        expect(priority).to_be_checked()
        expect(warning).to_be_checked()
        expect(page.locator("#question")).to_have_value("Уточню фактический зазор после измерения.")
        warning.uncheck()
        expect(page.get_by_role("button", name="Подтвердить и отправить", exact=True)).to_be_disabled()
        warning.check()
        page.get_by_role("button", name="Подтвердить и отправить", exact=True).click()
        expect(page.locator(".ticket-badge")).to_be_visible()
        number = page.locator(".ticket-badge").inner_text()
        page.reload()
        expect(page.locator(".ticket-badge")).to_have_text(number)
        state = page_state(page, base, WARNING)
        assert len([call for call in state["calls"] if call["tool"] == "mock_toir.tickets" and call["status"] == "ok"]) == 1
        results.append({"pair": WARNING, "step": "Warning отдельно от приоритета, возврат из PDF с согласиями, ticket_id после подтверждения", "mobile": mobile, "result": number})

        # F14 is the default end-to-end scenario after reviewing the evidence.
        open_pair(page, base, CHAIN)
        ask(page, "Как проверить гидронатяжитель?", "гидронатяж")
        draft(page, from_result=True)
        expect(page.locator('#ticket-panel a[href="/sources/zmz409/143"]').first).to_be_visible()
        pdf_source(page, "/sources/zmz409/143", 143, None, original=True, section="#ticket-panel")
        submit(page)
        expect(page.locator(".ticket-badge")).to_be_visible()
        state = page_state(page, base, CHAIN)
        assert state["ticket"]["ticket_id"] and len([call for call in state["calls"] if call["tool"] == "mock_toir.tickets"]) == 1
        copy_ticket_number(page, base, state["ticket"]["ticket_id"], touch=mobile)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=out / ("admission-mobile.png" if mobile else "admission-desktop.png"))
        results.append({"pair": CHAIN, "step": "Проверенная работа F14, PDF 143, подтверждение и номер", "mobile": mobile, "result": state["ticket"]["ticket_id"]})
        assert not errors, errors
    finally:
        context.close()


def late_response_check(browser, directory, results):
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
        page.locator("#vehicle").fill(CHAIN[0])
        until(3)
        vehicle_option(page, CHAIN[0]).click()
        until(4.5)
        page.locator("#session").select_option(CHAIN[1])
        until(6)
        checkpoint("launch")
        expect(page.locator("#result-title")).to_have_text("Обнаружено отклонение")
        checkpoint("anomaly")
        until(10)
        page.locator("#question").press_sequentially("Как проверить гидронатяжитель?", delay=23)
        page.locator("#send").click()
        expect(page.locator("#messages")).to_contain_text("гидронатяж")
        checkpoint("answer")
        until(16)
        draft(page, from_result=True)
        checkpoint("draft")
        until(17.5)
        page.locator('#ticket-panel a[href="/sources/zmz409/143"]').first.click()
        expect(page.locator(".source-position")).to_contain_text("Страница PDF 143")
        checkpoint("page_143")
        until(20)
        page.evaluate("window.scrollTo(0, 390)")
        until(24)
        page.get_by_role("link", name="К проверке", exact=True).click()
        expect(page.locator("#question")).to_be_enabled()
        until(27)
        page.locator("#ticket-title").scroll_into_view_if_needed()
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
    assert times["launch"] <= 10 and times["page_143"] <= 30 and times["ticket"] <= 47
    assert times["repeat_recording"] <= 60 and times["capture_elapsed"] <= 60
    return video.path(), times


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--expect-team", action="store_true")
    args = parser.parse_args()
    expect.set_options(timeout=30000)
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
                admission_checks(browser, base, out, results)
                with stand(0, directory) as mobile_base:
                    admission_checks(browser, mobile_base, out, results, mobile=True)
                late_response_check(browser, directory, results)
                (out / "browser-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"{len(results)} browser checks passed", flush=True)
        browser.close()


if __name__ == "__main__":
    main()
