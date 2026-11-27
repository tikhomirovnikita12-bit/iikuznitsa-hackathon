"""Prepare the customer's local mocks, manuals, index, and page previews."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from manual_search.corpus import build_corpus

from .config import CORPUS, MATERIALS, MOCKS, PREVIEWS, ROOT
from .sources import BY_SLUG, MANUALS


CUSTOMER_ARCHIVE = "neurodvs_hackathon_mock_2026-11-16.zip"
MOCK_FILES = (
    "README.md", ".env.example", "requirements.txt", "openapi_ml.json", "openapi_toir.json",
    "data/vehicles.json", "data/fault_types.json", "mock_ml/__init__.py", "mock_ml/app.py",
    "mock_toir/__init__.py", "mock_toir/app.py", "tests/test_mocks.py",
)
PREVIEW_PAGES = {"yamz236-re": (19, 53), "zmz409": (107,), "zmz5234": (20, 21, 56)}
ARCHIVE_NAME = "007_хакатон_ИИ.zip"


class PreparationError(Exception):
    """An expected setup problem with instructions for the person running the app."""


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def discover_archive(explicit: Path | None = None) -> Path | None:
    """Find an outer ZIP or an unpacked customer bundle relative to the project."""
    configured = explicit or os.environ.get("CUSTOMER_ARCHIVE_PATH")
    location = Path(configured).expanduser() if configured else ROOT
    if not location.is_absolute():
        location = ROOT / location
    if location.is_file():
        return location
    if not location.is_dir():
        raise PreparationError(
            f"Не найден архив или каталог: {location}\n"
            "Как исправить: проверьте CUSTOMER_ARCHIVE_PATH или передайте python run.py --archive ПУТЬ_К_ZIP."
        )
    preferred = location / ARCHIVE_NAME
    if preferred.is_file():
        return preferred
    for directory in (location, location / Path(ARCHIVE_NAME).stem):
        if directory.is_dir() and all((directory / manual.filename).is_file() for manual in MANUALS):
            return directory
    archives = sorted(path for path in location.iterdir() if path.is_file() and path.suffix.lower() == ".zip")
    if len(archives) > 1:
        raise PreparationError(
            f"В {location} несколько ZIP-архивов: {', '.join(path.name for path in archives)}.\n"
            f"Как исправить: назовите архив заказчика {ARCHIVE_NAME} или укажите python run.py --archive ПУТЬ_К_ZIP."
        )
    return archives[0] if archives else None


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
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        with archive.open(member) as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def unpack(path: Path):
    if path.is_dir():
        nested = path / CUSTOMER_ARCHIVE
        if not nested.is_file():
            raise PreparationError(
                f"В каталоге материалов {path} не найден {CUSTOMER_ARCHIVE}.\n"
                "Как исправить: поместите архив заглушек рядом с шестью PDF или используйте полный ZIP заказчика."
            )
        with ZipFile(nested) as mocks:
            members = [(find(mocks, name), MOCKS / name) for name in MOCK_FILES]
            for manual in MANUALS:
                destination = MATERIALS / manual.filename
                source = path / manual.filename
                if source.resolve() == destination.resolve():
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + ".part")
                try:
                    shutil.copyfile(source, temporary)
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
            for member, destination in members:
                extract(mocks, member, destination)
        return
    with ZipFile(path) as archive:
        with ZipFile(BytesIO(archive.read(find(archive, CUSTOMER_ARCHIVE, basename=True)))) as mocks:
            # Resolve the complete allowlist before replacing any prepared files.
            mock_members = [(find(mocks, name), MOCKS / name) for name in MOCK_FILES]
            manual_members = [
                (find(archive, manual.filename, basename=True), MATERIALS / manual.filename)
                for manual in MANUALS
            ]
            for member, destination in mock_members:
                extract(mocks, member, destination)
            for member, destination in manual_members:
                extract(archive, member, destination)


def require_poppler():
    missing = [name for name in ("pdftotext", "pdftoppm") if not shutil.which(name)]
    if missing:
        raise PreparationError(
            f"Не найдены утилиты Poppler в PATH: {', '.join(missing)}.\n"
            "Как исправить: macOS — brew install poppler; Ubuntu/Debian — sudo apt install poppler-utils.\n"
            "Windows — установите Poppler, добавьте папку с pdftotext.exe и pdftoppm.exe в PATH "
            "и откройте новый терминал. Или используйте docker compose up --build: Poppler уже включён в образ."
        )


def ensure_ready(archive: Path | None = None, *, force=False):
    """Prepare missing or changed materials; preserve SQLite and reuse a compatible index."""
    try:
        _ensure_ready(archive, force=force)
    except PreparationError:
        raise
    except (BadZipFile, EOFError, RuntimeError, ValueError) as error:
        raise PreparationError(
            f"Не удалось подготовить материалы: {error}\n"
            f"Как исправить: используйте полный внешний ZIP заказчика ({ARCHIVE_NAME}), "
            "содержащий шесть PDF и вложенный архив заглушек. Повторно скачайте повреждённый ZIP.\n"
            "Для другой версии PDF проверьте каталог data/manuals/catalog.json и разметку применимости. "
            "После исправления повторите python run.py."
        ) from error
    except subprocess.CalledProcessError as error:
        details = error.stderr or ""
        if isinstance(details, bytes):
            details = details.decode("utf-8", errors="replace")
        raise PreparationError(
            f"Poppler не смог обработать PDF (код {error.returncode}). {str(details).strip()[:800]}\n"
            "Как исправить: проверьте целостность PDF и версию руководства, повторно распакуйте архив "
            "командой python -m mechanic.prepare --rebuild. Прежний индекс и SQLite сохранены."
        ) from error
    except OSError as error:
        raise PreparationError(
            f"Ошибка доступа к материалам: {error}\n"
            "Как исправить: проверьте свободное место и права записи в artifacts и каталог индекса. "
            "Для Docker разрешите доступ к папке проекта в настройках Docker Desktop. "
            "Исправьте причину и повторите запуск; удалять SQLite не нужно."
        ) from error


def _ensure_ready(archive: Path | None, *, force):
    state_path = CORPUS.with_suffix(".state.json")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}

    archive = discover_archive(archive)
    signature = None
    if archive:
        if archive.is_dir():
            inputs = [archive / manual.filename for manual in MANUALS] + [archive / CUSTOMER_ARCHIVE]
            if not inputs[-1].is_file():
                raise PreparationError(f"В каталоге {archive} не хватает {CUSTOMER_ARCHIVE}. "
                                       "Поместите архив заглушек рядом с PDF.")
            signature = {"sha256": hashlib.sha256("\n".join(digest(path) for path in inputs).encode()).hexdigest()}
        else:
            signature = {"sha256": digest(archive)}
    required = [MATERIALS / manual.filename for manual in MANUALS]
    required.extend(MOCKS / name for name in MOCK_FILES)
    missing = [path for path in required if not path.is_file()]
    if archive and (force or missing or signature != state.get("archive")):
        print(f"[Подготовка] Загрузка материалов из {archive}…", flush=True)
        unpack(archive)
        missing = [path for path in required if not path.is_file()]
    if missing:
        raise PreparationError(
            "Не хватает материалов заказчика:\n" + "\n".join(f"  • {path}" for path in missing[:8]) + "\n"
            f"Как исправить: поместите {ARCHIVE_NAME} или распакованную папку {Path(ARCHIVE_NAME).stem} "
            "в корень проекта рядом с run.py и запустите python run.py.\n"
            "Поддерживаются также шесть PDF с архивом заглушек прямо в корне. "
            "Другой путь: python run.py --archive ПУТЬ_К_КОМПЛЕКТУ."
        )

    # A content fingerprint also invalidates the cache after changes to parsing or curated data.
    inputs = [*required, Path(__file__), ROOT / "manual_search" / "corpus.py"]
    inputs.extend(sorted((ROOT / "data" / "manuals").glob("*.json")))
    fingerprint = hashlib.sha256("\n".join(digest(path) for path in inputs).encode()).hexdigest()
    cached = (not force and state.get("fingerprint") == fingerprint and CORPUS.is_file()
              and state.get("index_sha256") == digest(CORPUS))
    previews = [(slug, page, PREVIEWS / f"{slug}-{page}.png")
                for slug, pages in PREVIEW_PAGES.items() for page in pages]
    needed_previews = [item for item in previews if force or not cached or not item[2].is_file()]
    if not cached or needed_previews:
        require_poppler()
    if cached:
        print("[Подготовка] Совместимый индекс найден; повторная сборка не нужна.", flush=True)
    else:
        print(f"[Подготовка] Индексация {len(MANUALS)} PDF. Дождитесь завершения…", flush=True)
        index = build_corpus(MATERIALS)
        if not isinstance(index.get("fragments"), list) or not index["fragments"]:
            raise PreparationError("Сборка вернула пустой индекс. Проверьте PDF заказчика и повторите подготовку.")

    PREVIEWS.mkdir(parents=True, exist_ok=True)
    for slug, page, destination in needed_previews:
        source = MATERIALS / BY_SLUG[slug].filename
        temporary = destination.with_name(destination.stem + ".part")
        try:
            subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-scale-to", "1350",
                            "-singlefile", "-png", str(source), str(temporary)],
                           check=True, capture_output=True)
            temporary.with_suffix(".part.png").replace(destination)
        finally:
            temporary.with_suffix(".part.png").unlink(missing_ok=True)
    if not cached:
        atomic_json(CORPUS, index)
        print(f"[Подготовка] Индекс сохранён: {len(index['fragments'])} фрагментов; "
              f"страниц с проблемным текстовым слоем: {len(index.get('extraction_issues', []))}.", flush=True)
    atomic_json(state_path, {"fingerprint": fingerprint, "index_sha256": digest(CORPUS), "archive": signature})
    print("[Подготовка] Руководства, поиск и заглушки готовы. SQLite сохранены.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Подготовка локальных материалов «Цифрового механика»")
    parser.add_argument("archive", type=Path, nargs="?", help="Путь к ZIP или распакованному каталогу заказчика")
    parser.add_argument("--rebuild", action="store_true", help="Пересобрать индекс и изображения, сохранив SQLite")
    args = parser.parse_args()
    try:
        ensure_ready(args.archive, force=args.rebuild)
    except PreparationError as error:
        parser.exit(1, f"\nПодготовка остановлена.\n{error}\n")


if __name__ == "__main__":
    main()
