"""Prepare the customer's local mocks, manuals, index, and page previews."""

import argparse
import json
import shutil
import subprocess
from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from manual_search.corpus import build_corpus

from .config import CORPUS, MATERIALS, MOCKS, PREVIEWS
from .sources import BY_SLUG, MANUALS


CUSTOMER_ARCHIVE = "neurodvs_hackathon_mock_2026-11-16.zip"
MOCK_FILES = (
    "README.md", ".env.example", "requirements.txt", "openapi_ml.json", "openapi_toir.json",
    "data/vehicles.json", "data/fault_types.json", "mock_ml/__init__.py", "mock_ml/app.py",
    "mock_toir/__init__.py", "mock_toir/app.py", "tests/test_mocks.py",
)
PREVIEW_PAGES = {"yamz236-re": (19, 53), "zmz409": (107,), "zmz5234": (20, 21, 56)}


def names(member):
    original = member.filename.replace("\\", "/")
    yield original
    if not member.flag_bits & 0x800:
        try:
            yield original.encode("cp437").decode("cp866")
        except UnicodeEncodeError:
            pass


def find(archive: ZipFile, path: str, basename=False):
    def matches_path(name):
        if basename:
            return PurePosixPath(name).name == path
        return name == path or name.endswith("/" + path)

    matches = [
        member for member in archive.infolist()
        if not member.is_dir() and any(matches_path(name) for name in names(member))
    ]
    if len(matches) != 1:
        raise ValueError(f"В {archive.filename or CUSTOMER_ARCHIVE} нужен один файл {path}")
    return matches[0]


def extract(archive, member, destination: Path):
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        with archive.open(member) as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def unpack(path: Path):
    with ZipFile(path) as archive:
        with ZipFile(BytesIO(archive.read(find(archive, CUSTOMER_ARCHIVE, basename=True)))) as mocks:
            for filename in MOCK_FILES:
                extract(mocks, find(mocks, filename), MOCKS / filename)
        for manual in MANUALS:
            extract(archive, find(archive, manual.filename, basename=True), MATERIALS / manual.filename)


def main():
    parser = argparse.ArgumentParser(description="Подготовка локальных материалов «Цифрового механика»")
    parser.add_argument("archive", type=Path, nargs="?", help="Путь к внешнему ZIP заказчика")
    args = parser.parse_args()
    if not shutil.which("pdftotext") or not shutil.which("pdftoppm"):
        parser.error("Нужны утилиты pdftotext и pdftoppm из Poppler в PATH.")
    if args.archive:
        unpack(args.archive)
    missing = [manual.filename for manual in MANUALS if not (MATERIALS / manual.filename).is_file()]
    if missing:
        parser.error("Не найдены руководства в " + str(MATERIALS) + ". Укажите ZIP заказчика.")

    index = build_corpus(MATERIALS)
    CORPUS.parent.mkdir(parents=True, exist_ok=True)
    CORPUS.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    for slug, pages in PREVIEW_PAGES.items():
        source = MATERIALS / BY_SLUG[slug].filename
        for page in pages:
            subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-scale-to", "1350",
                            "-singlefile", "-png", str(source), str(PREVIEWS / f"{slug}-{page}")], check=True)
    print(f"Материалы готовы. В индексе {len(index['fragments'])} фрагментов.")


if __name__ == "__main__":
    main()
