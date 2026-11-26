"""Integration acceptance against running ML, TOiR and Ollama services."""
from agent.server import create_app


def main():
    app = create_app()
    with app.test_client() as client:
        client.get("/")
        v = "E549YT198"
        def ask(s, q, vehicle=v):
            response = client.post("/api/chat", json=dict(vehicle_id=vehicle, session_id=s, question=q))
            assert response.status_code == 200, response.data
            return response.json
        for question in ("Покажи результат диагностики", "что мне сделать", "как это сделать?"):
            response = ask("S-0159", question, "B332BP98")
            if question == "что мне сделать":
                first = response["checks"][0]["text"]
                assert response["intent"] == "search" and "холодном" in first
            if question == "как это сделать?":
                detail = response["checks"][0]["text"]
                assert response["intent"] == "search" and detail != first
                assert "руководстве по эксплуатации автомобиля" in detail
                assert response["references"][0]["pdf_page"] == 97
        moved = ask("S-0011", "Можно ли своим ходом перегнать машину на базу?")
        assert moved["clarifying_question"] and "перегон" in moved["verdict"]
        assert any(r["pdf_page"] == 19 for r in moved["references"]), moved
        switched = ask("S-0010", "А что проверить сначала?")
        assert switched["ml_observations"]["session_id"] == "S-0010"
        assert switched["ml_observations"]["status"] == "normal" and not switched["checks"]
        failed = ask("S-0003", "Покажи диагностику", "P456AT178")
        assert failed["ml_observations"]["confidence"] is None and not failed["references"]
        assert "Повторите" in failed["verdict"] and "микрофона" in failed["ml_observations"]["description"]
        conflict = ask("S-0132", "Что проверить?", "Y652CK98")
        assert "топливоподача" in conflict["clarifying_question"].lower()
        draft = ask("S-0011", "Подготовь заявку на дефектовку")
        assert draft["ticket_draft"] and draft["ticket_draft"]["operations"], draft
        payload = dict(vehicle_id=v, session_id="S-0010", draft_id=draft["draft_id"])
        assert client.post("/api/confirm", json=payload).status_code == 409
        payload["session_id"] = "S-0011"
        result = client.post("/api/confirm", json=payload)
        assert result.status_code == 200 and result.json["ticket_id"], result.data
        assert client.post("/api/confirm", json=payload).status_code == 409
        url = draft["references"][0]["url"].split("#")[0]
        assert client.get(url).mimetype == "application/pdf"
        print("Перегон, переключение, конфликт F08, ссылки, форма и подтверждение:", result.json)


if __name__ == "__main__":
    main()
