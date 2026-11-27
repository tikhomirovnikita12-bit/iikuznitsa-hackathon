"""Responses can be returned to the chat API as history without client truncation."""
import json

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from backend.app import ROOT, Settings, create_app
from backend.models import ChatReply, ChatRequest, ChatTurn


Q08 = "Что делать, если не работает свеча зажигания?"
PAIR = {"vehicle_id": "C991PH47", "session_id": "S-0147"}


def test_chat_answer_and_history_share_lossless_text_contract():
    answer = "  Начало ответа.\n" + "Полная выдержка из руководства.\n" * 180 + "Конец ответа.  "
    received = ChatReply(answer=answer)
    turn = ChatTurn(question=Q08, answer=received.answer)
    assert turn.answer == received.answer == answer
    reply_schema = ChatReply.model_json_schema()["properties"]["answer"]
    turn_schema = ChatTurn.model_json_schema()["properties"]["answer"]
    assert reply_schema == turn_schema
    assert "maxLength" not in turn_schema
    with pytest.raises(ValidationError):
        ChatRequest(**PAIR, question="?" * 2001)
    with pytest.raises(ValidationError):
        ChatRequest(**PAIR, question="Как это проверить?", history=[turn] * 7)


def test_q08_full_answer_round_trips_in_history(tmp_path):
    if not Settings.corpus.is_file():
        pytest.skip("Подготовьте исходные PDF и индекс: python -m mechanic.prepare")
    vehicles = json.loads((ROOT / "data/vehicles.json").read_text(encoding="utf-8"))["vehicles"]
    vehicle = next(item for item in vehicles if item["vehicle_id"] == PAIR["vehicle_id"])
    session = next(item for item in vehicle["sessions"] if item["session_id"] == PAIR["session_id"])
    app = create_app(
        Settings(database=tmp_path / "history.sqlite3", ollama_model=""),
        ml_transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=session)),
    )
    with TestClient(app) as api:
        schema = api.get("/openapi.json").json()["components"]["schemas"]
        assert schema["ChatReply"]["properties"]["answer"] == schema["ChatTurn"]["properties"]["answer"]
        assert api.post("/api/diagnose", json=PAIR).status_code == 200
        first = api.post("/api/chat", json={**PAIR, "question": Q08})
        assert first.status_code == 200, first.text
        answer = first.json()["answer"]
        assert len(answer) > 2000, "The regression requires an actual answer exceeding the old limit"
        assert any(item["source"]["pdf_page"] == 57 for item in first.json()["evidence"])
        history_seen = []
        reply = app.state.agent.reply

        def capture(request):
            history_seen.append(request.history[0].answer)
            return reply(request)

        app.state.agent.reply = capture
        following = api.post("/api/chat", json={
            **PAIR, "question": "Как это проверить?",
            "history": [{"question": Q08, "answer": answer}],
        })
        assert following.status_code == 200, following.text
        assert history_seen == [answer]
