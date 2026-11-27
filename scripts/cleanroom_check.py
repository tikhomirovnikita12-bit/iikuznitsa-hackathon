"""Reproduce the stand from the customer archive in a clean directory and test it.

The script copies only source files (no artifacts, databases or virtualenv), builds
the corpus from the original ZIP, then runs the same regression suite as selftest.py.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv/Scripts/python.exe"
PYTHON = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
ARCHIVE = ROOT / "007_хакатон_ИИ.zip"
POPPLER = ROOT / ".runtime/poppler/poppler-26.09.0/Library/bin"
SOURCES = (
    "agent", "backend", "mechanic", "web", "data", "contracts", "manual_search",
    "tests", "scripts", "docs", "output", "main.py", "run.py", "requirements.txt",
    "README.md", ".env.example",
)


def run(name: str, command: list[str], cwd: Path, env: dict[str, str]) -> None:
    print(f"[cleanroom] {name}: {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=cwd, env=env, check=False)
    if result.returncode:
        raise SystemExit(f"[cleanroom] {name} failed with code {result.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, help="directory for the clean copy")
    parser.add_argument("--skip-selftest", action="store_true")
    args = parser.parse_args()

    target = args.target or Path(tempfile.gettempdir()) / "opencode/cleanroom"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in SOURCES:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, target / name, ignore=shutil.ignore_patterns("__pycache__"))
        elif source.exists():
            shutil.copy2(source, target / name)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(target)
    if POPPLER.exists():
        env["PATH"] = str(POPPLER) + os.pathsep + env.get("PATH", "")

    run("prepare", [str(PYTHON), "-m", "mechanic.prepare", str(ARCHIVE)], target, env)
    if not args.skip_selftest:
        run("selftest", [str(PYTHON), "scripts/selftest.py"], target, env)
    print(f"[cleanroom] ready: {target}", flush=True)


if __name__ == "__main__":
    main()
