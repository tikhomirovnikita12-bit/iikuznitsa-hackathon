"""Install and run the Russian dictation model on the mechanic server."""

import argparse
from functools import cache
import hashlib
import json
import os
from pathlib import Path
from shutil import copyfileobj
import threading
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from zipfile import ZipFile


MODEL_NAME = "vosk-model-small-ru-0.22"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
MODEL_SHA256 = "961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4"
MODEL_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "digital-mechanic" / MODEL_NAME
SAMPLE_RATE = 16000
MAX_AUDIO_BYTES = SAMPLE_RATE * 2 * 30
_model_lock = threading.Lock()


class SpeechUnavailable(Exception):
    pass


def install_model(archive: Path | None = None) -> Path:
    if MODEL_DIR.is_dir():
        return MODEL_DIR
    MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="speech-", dir=MODEL_DIR.parent) as temporary:
        directory = Path(temporary)
        if archive is None:
            archive = directory / "model.zip"
            with urlopen(MODEL_URL, timeout=40) as response, archive.open("wb") as target:
                copyfileobj(response, target)
        with archive.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != MODEL_SHA256:
                raise ValueError("Контрольная сумма архива модели не совпала.")
        with ZipFile(archive) as source:
            source.extractall(directory)
        try:
            (directory / MODEL_NAME).rename(MODEL_DIR)
        except OSError:
            if not MODEL_DIR.is_dir():
                raise
    return MODEL_DIR


@cache
def _load_model():
    if not MODEL_DIR.is_dir():
        raise SpeechUnavailable("Установите локальную модель командой python -m mechanic.speech.")
    try:
        from vosk import KaldiRecognizer, Model, SetLogLevel

        SetLogLevel(-1)
        return Model(str(MODEL_DIR)), KaldiRecognizer
    except Exception as error:
        raise SpeechUnavailable("Локальная модель распознавания не запустилась.") from error


def transcribe(pcm: bytes) -> str:
    with _model_lock:
        model, recognizer_type = _load_model()
    recognizer = recognizer_type(model, SAMPLE_RATE)
    parts = []
    for start in range(0, len(pcm), 8000):
        if recognizer.AcceptWaveform(pcm[start:start + 8000]):
            parts.append(json.loads(recognizer.Result())["text"])
    parts.append(json.loads(recognizer.FinalResult())["text"])
    return " ".join(filter(None, parts))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Установить локальную русскую модель для голосового ввода.")
    parser.add_argument("--archive", type=Path, help="заранее загруженный архив модели")
    args = parser.parse_args()
    print("Модель установлена в", install_model(args.archive))
