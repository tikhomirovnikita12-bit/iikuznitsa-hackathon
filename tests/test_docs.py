"""Регрессия документации: локальные ссылки и заголовки файлов."""

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
SKIP = ("http://", "https://", "mailto:", "#")


def markdown_files():
    return [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]


@pytest.mark.parametrize("path", markdown_files(), ids=lambda path: path.relative_to(ROOT).as_posix())
def test_local_links_exist(path):
    text = path.read_text(encoding="utf-8")
    for target in LINK.findall(text):
        target = target.strip().strip("<>").split("#", 1)[0].split("?", 1)[0]
        if not target or target.startswith(SKIP):
            continue
        resolved = (path.parent / target).resolve()
        assert resolved.exists(), f"{path.name}: отсутствует {target}"


@pytest.mark.parametrize("path", markdown_files(), ids=lambda path: path.relative_to(ROOT).as_posix())
def test_document_starts_with_heading(path):
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines and lines[0].startswith("# "), f"{path.name}: нет заголовка первого уровня"


def test_no_mojibake_markdown():
    bad_files = [path.name for path in markdown_files() if "╨" in path.name]
    assert not bad_files, f"Файлы с мозаикой в имени: {bad_files}"
    bad_text = [path.name for path in markdown_files() if "╨" in path.read_text(encoding="utf-8")]
    assert not bad_text, f"Файлы с мозаикой в тексте: {bad_text}"
