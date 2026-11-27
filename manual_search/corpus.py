"""Build a page-linked index from the original PDFs using Poppler."""

import argparse
import json
import re
import subprocess
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "manuals"
SHARED_ZMZ = "zmz-40904-40905-rk"
VERIFIED_FILES = ("yamz-236m2-oil-pressure.json", "zmz-timing-chain.json", "yamz-236m2-valves.json",
                  "yamz-536-oil-pressure.json", "yamz-236m2-repair.json", "yamz-236m2-coolant.json",
                  "work-evidence.json", "reviewed-procedures.json")
YAMZ_MODEL = re.compile(r"ЯМЗ\s*[-–]\s*(\d{3}(?:[А-Яа-яЁё]*\d*)?)", re.I)
ZMZ_MODEL = re.compile(r"(?<!\d)(4090[45])(?:\.10(?!\d)|\.1000400-\d+)")
CONTINUATION = re.compile(r"^(?:продолжение\s+табл|продолжение\s+рис)", re.I)
TABLE_COLUMNS = re.compile(r"(?:Причина|Наименование|Операция).* {3,}\S", re.I)


def load_catalog():
    return json.loads((DATA_DIR / "catalog.json").read_text(encoding="utf-8"))


def read_pages(path, expected):
    result = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    pages = result.stdout.removesuffix("\f").split("\f")
    if len(pages) != expected:
        raise ValueError(f"{path.name}: expected {expected} pages, got {len(pages)}")
    return pages


def strip_footer(page, number):
    lines = page.rstrip().splitlines()
    if lines and lines[-1].strip() == str(number):
        lines.pop()
    return "\n".join(lines).rstrip()


def make_fragment(manual, page, line, section, models, text):
    return correct_extracted_fragment({
        "fragment_id": f"{manual['manual_id']}-p{page:03d}-l{line:03d}",
        "manual_id": manual["manual_id"],
        "manual": manual["manual"],
        "source_file": manual["source_file"],
        "pdf_page": page,
        "section": section,
        "applicable_engine_models": models,
        "text": text.strip(),
    })


def correct_extracted_fragment(entry):
    # Only this table uses a superscript zero that Poppler appends to 100.
    if entry["manual_id"] == "yamz-536-01-02-rk" and entry["pdf_page"] == 13:
        corrected = re.sub(r"80-1000\s+[СC](?=, кПа)", "80–100 °C", entry["text"])
        if corrected != entry["text"]:
            return {**entry, "text": corrected}
    return entry


def page_blocks(page_text):
    """Keep adjacent lines together and preserve the PDF table's spacing."""
    blocks = []
    for line_number, line in enumerate(page_text.splitlines(), 1):
        if not line.strip():
            if blocks and blocks[-1][1][-1]:
                blocks[-1][1].append("")
            continue
        if not blocks or not blocks[-1][1][-1]:
            blocks.append((line_number, []))
        blocks[-1][1].append(line)
    return [(start, "\n".join(lines).rstrip()) for start, lines in blocks]


def zmz_blocks(page, text, rules, models):
    lines = text.splitlines()
    scoped_models = {
        index: [rule["engine_model"]]
        for rule in rules["page_ranges"]
        if rule["first"] <= page <= rule["last"]
        for index in range(len(lines))
    }
    for rule in rules["blocks"]:
        if rule["page"] != page:
            continue
        start = 0
        if "start" in rule:
            start = next(
                (i for i, line in enumerate(lines) if line.strip().startswith(rule["start"])), None
            )
        if start is None:
            raise ValueError(f"PDF page {page}: missing scope start {rule['start']!r}")
        stop = len(lines)
        if "end" in rule:
            stop = next(
                (i for i in range(start + 1, len(lines)) if lines[i].strip().startswith(rule["end"])),
                None,
            )
            if stop is None:
                raise ValueError(f"PDF page {page}: missing scope end {rule['end']!r}")
        for i in range(start, stop):
            scoped_models[i] = [rule["engine_model"]]

    cuts = {0, len(lines)}
    for i, line in enumerate(lines):
        if i and scoped_models.get(i) != scoped_models.get(i - 1):
            cuts.add(i)
        if not line.strip():
            cuts.add(i + 1)
        # Poppler preserves first-line indentation; don't split aligned table columns.
        if i and re.match(r"^ {4,8}\S", line) and re.match(r"^\S", lines[i - 1].strip()):
            if not re.search(r"\S {3,}\S", line):
                cuts.add(i)
    boundaries = sorted(cuts)
    for start, stop in zip(boundaries, boundaries[1:]):
        block = "\n".join(lines[start:stop]).strip()
        if not block:
            continue
        explicit = {f"ЗМЗ-{model}.10" for model in ZMZ_MODEL.findall(block)}
        block_models = scoped_models.get(start) or [model for model in models if model in explicit] or models
        yield start + 1, block, block_models


def heading(block):
    first = block.splitlines()[0].strip()
    if 4 <= len(first) <= 110 and sum(c.isalpha() for c in first) >= 4 and first.isupper():
        return first
    return None


def extract_manual(manual, directory, rules):
    pages = read_pages(directory / manual["source_file"], manual["page_count"])
    models = manual["applicable_engine_models"]
    section = manual["manual"]
    previous = []
    fragments = []
    issues = []
    for page, extracted in enumerate(pages, 1):
        text = strip_footer(extracted, page)
        if len(re.findall(r"[А-Яа-яЁё]", text)) < 30:
            issues.append({
                "manual_id": manual["manual_id"],
                "pdf_page": page,
                "reason": "sparse_text" if text.strip() else "empty_text",
            })
        blocks = page_blocks(text)
        if manual["manual_id"] == SHARED_ZMZ:
            blocks = list(zmz_blocks(page, text, rules, models))
        else:
            blocks = [(line, block, models) for line, block in blocks]

        current = []
        previous_section = section
        for line, block, applicable in blocks:
            if manual["manual_id"].startswith("yamz-"):
                named_models = {model.upper() for model in YAMZ_MODEL.findall(block)}
                # Drop blocks explicitly about another engine, including notes in a shared manual.
                if named_models and not any(
                    name == model.removeprefix("ЯМЗ-") or (
                        name.isdecimal() and model.removeprefix("ЯМЗ-").startswith(name)
                    )
                    for model in applicable for name in named_models
                ):
                    continue
            section = heading(block) or section
            current.append(make_fragment(manual, page, line, section, applicable, block))
        if previous and current and previous_section == current[0]["section"]:
            repeated_header = (
                TABLE_COLUMNS.search(extracted)
                and TABLE_COLUMNS.search(pages[page - 2])
                and TABLE_COLUMNS.search(extracted)[0].split()[:2]
                == TABLE_COLUMNS.search(pages[page - 2])[0].split()[:2]
            )
            starts_mid_sentence = re.match(r"^[а-яё]", text.lstrip())
            if CONTINUATION.match(text.lstrip()) or repeated_header or starts_mid_sentence:
                current[0]["continues_from"] = [previous[-1]["fragment_id"]]
        fragments.extend(current)
        previous = current
    return fragments, issues


def build_corpus(pdf_dir):
    rules = json.loads((DATA_DIR / "zmz-applicability.json").read_text(encoding="utf-8"))
    fragments = []
    issues = []
    catalog = load_catalog()
    for manual in catalog:
        extracted, flagged = extract_manual(manual, pdf_dir, rules)
        fragments.extend(extracted)
        issues.extend(flagged)
    fragments.extend(verified_fragments(catalog))
    conflicts = json.loads((DATA_DIR / "manual-conflicts.json").read_text(encoding="utf-8"))
    return {"fragments": fragments, "conflicts": conflicts, "extraction_issues": issues}


def verified_fragments(catalog=None):
    manuals = {manual["manual_id"]: manual for manual in (catalog or load_catalog())}
    fragments = []
    for filename in VERIFIED_FILES:
        verified = json.loads((DATA_DIR / filename).read_text(encoding="utf-8"))
        for entry in verified:
            manual = manuals[entry["manual_id"]]
            fragments.append({
                **entry,
                "manual": manual["manual"],
                "source_file": manual["source_file"],
                "verified": True,
            })
    return fragments


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_dir", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    corpus = build_corpus(args.pdf_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(corpus['fragments'])} fragments, {len(corpus['extraction_issues'])} flagged pages")


if __name__ == "__main__":
    main()
