import pytest
from fastapi.testclient import TestClient

from mechanic import main, speech


@pytest.mark.parametrize(("content_type", "recording", "status"), [
    ("audio/webm", b"ab", 415),
    ("application/octet-stream", b"", 422),
    ("application/octet-stream", b"a", 422),
    ("application/octet-stream", b"\0" * (speech.MAX_AUDIO_BYTES + 2), 413),
])
def test_dictation_rejects_invalid_recordings(monkeypatch, content_type, recording, status):
    def invalid_call(_):
        pytest.fail("Invalid audio reached speech recognition")

    monkeypatch.setattr(speech, "transcribe", invalid_call)
    with TestClient(main.app) as client:
        response = client.post("/api/dictation", content=recording, headers={"Content-Type": content_type})
    assert response.status_code == status


def test_dictation_returns_only_transcript(monkeypatch):
    recording = b"\0" * 32000

    def transcribe(pcm):
        assert pcm == recording
        return "проверить давление масла"

    monkeypatch.setattr(speech, "transcribe", transcribe)
    with TestClient(main.app) as client:
        response = client.post("/api/dictation", content=recording,
                               headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 200
    assert response.json() == {"transcript": "проверить давление масла"}
