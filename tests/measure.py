"""Сравнение двух автоматизированных проходов по готовому результату ML."""

import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from urllib.parse import urljoin

from playwright.sync_api import expect, sync_playwright
from pypdf import PdfReader

from browser_check import CHAIN, OIL, OIL_QUESTION, RUNTIME, ask, diagnose, draft, pick, stand


CASES = [
    (OIL, "yamz236-re", "236-3902150-Б РЭ", {19: "400-700 кПа",
     53: "Пониженное давление масла в системе смазки"}, OIL_QUESTION,
     "Подозрение на низкое давление масла. Нужен замер.", "high",
     "Проверить количество масла и охлаждающей жидкости. Измерить давление на прогретом двигателе."),
    (CHAIN, "zmz409",
     "Руководство по эксплуатации, техническому обслуживанию и ремонту двигателей ЗМЗ-40904.10 и ЗМЗ-40905.10",
     {107: "неисправен гидронатяжитель цепи"}, "Что проверить, если цепь шумит на средних оборотах?",
     "Ровный шум спереди. Проверить цепи ГРМ и гидронатяжитель.", "medium",
     "Проверить увеличение длины цепей и исправность гидронатяжителя."),
]


def base_fields(diagnosis):
    return {key: diagnosis[key] for key in ("vehicle_id", "session_id", "anomaly_type")}


def direct_search(page, base, case, diagnosis):
    _, slug, manual, needles, _, conclusion, urgency, operations = case
    page.set_content("""
        <h1>Черновик дефектовки</h1>
        <textarea id="conclusion"></textarea><input id="urgency">
        <textarea id="operations"></textarea><textarea id="references"></textarea>
    """)
    response = page.request.get(base + f"/manuals/{slug}.pdf")
    assert response.ok
    reader = PdfReader(BytesIO(response.body()))
    matches = {}
    for number, pdf_page in enumerate(reader.pages, 1):
        text = " ".join(pdf_page.extract_text().split()).casefold()
        for needle in needles.values():
            if needle.casefold() in text:
                matches.setdefault(needle, number)
        if len(matches) == len(needles):
            break
    assert matches == {needle: number for number, needle in needles.items()}, matches
    refs = [{"manual": manual, "pdf_page": number} for number in sorted(matches.values())]
    for selector, value in [
        ("#conclusion", conclusion), ("#urgency", urgency), ("#operations", operations),
        ("#references", json.dumps(refs, ensure_ascii=False)),
    ]:
        page.locator(selector).fill(value)
    return {**base_fields(diagnosis), "conclusion": conclusion, "urgency": urgency,
            "operations": [operations], "spare_parts": [], "references": json.loads(page.locator("#references").input_value())}


def assistant_search(page, case, diagnosis):
    _, slug, _, needles, question, *_ = case
    ask(page, question, "Проверки", keyboard=True)
    for number in needles:
        page.locator(f'#messages a[href="/sources/{slug}/{number}"]').first.click()
        expect(page.locator("img.source-image")).to_be_visible()
        page.go_back()
        expect(page.locator("#question")).to_be_enabled()
    draft(page)
    params = {key: diagnosis[key] for key in ("vehicle_id", "session_id")}
    response = page.request.get(urljoin(page.url, "/api/state"), params=params)
    return response.json()["draft"]


def measure(browser, base, case, order):
    pair, _, _, needles, *_ = case
    measurements = {}
    for mode in order:
        context = browser.new_context()
        page = context.new_page()
        page.goto(base)
        pick(page, *pair)
        diagnose(page, "Обнаружено отклонение")
        result = page.request.get(base + "/api/state",
                                  params=dict(zip(("vehicle_id", "session_id"), pair))).json()["diagnosis"]
        started = perf_counter()
        if mode == "pdf":
            ticket = direct_search(page, base, case, result)
        else:
            ticket = assistant_search(page, case, result)
        end = perf_counter()
        assert base_fields(ticket) == base_fields(result)
        assert ticket["conclusion"] and ticket["operations"] and ticket["spare_parts"] == []
        assert {reference["pdf_page"] for reference in ticket["references"]} == needles.keys()
        measurements[mode] = round(end - started, 3)
        context.close()
    return {"pair": pair, **measurements, "pdf_minus_assistant": round(measurements["pdf"] - measurements["assistant"], 3)}


def main():
    rows = []
    with sync_playwright() as playwright, TemporaryDirectory(dir=RUNTIME, prefix="measure-", ignore_cleanup_errors=True) as scratch:
        browser = playwright.chromium.launch(channel="chromium", headless=True)
        for repeat, order in enumerate((("pdf", "assistant"), ("assistant", "pdf")), 1):
            directory = Path(scratch) / str(repeat)
            directory.mkdir()
            with stand(0, directory) as base:
                for case in CASES:
                    row = {"run": repeat, **measure(browser, base, case, order)}
                    rows.append(row)
                    print(row, flush=True)
        browser.close()
    (RUNTIME / "qa" / "measurements.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
