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
        term = ask("S-0011", "что такое картер")
        assert term["intent"] == "search"
        assert term["references"] or term["clarifying_question"]
        for message in ("я сегодня устал", "Какой фильм посмотреть вечером?"):
            off_topic = ask("S-0011", message)
            assert off_topic["intent"] == "conversation", off_topic
            assert not off_topic["references"] and not off_topic["checks"]
        for message in ("как считаешь пол уокер заслужил?", "у пол уокера митсубиши эклипс"):
            response = ask("S-0011", message)
            assert response["intent"] == "conversation" and not response["references"], response
        for question in ("лад давай че надо сделать", "так че сделать"):
            response = ask("S-0011", question)
            assert response["intent"] == "search" and response["checks"], response
            assert response["references"][0]["pdf_page"] == 53
        diagnosis = ask("S-0011", "ЧЕ СЛУЧИЛОСЬ")
        assert diagnosis["intent"] == "diagnosis" and "Низкое давление масла" in diagnosis["verdict"]
        enough = ask("S-0011", "так ну достаточно")
        assert enough["intent"] == "clarify" and "уровни масла" in enough["clarifying_question"], enough
        advice = ask("S-0011", "давай подскажи")
        assert advice["intent"] == "search" and advice["references"][0]["pdf_page"] == 53, advice
        complaint = ask("S-0011", "ты че ахуел")
        assert complaint["intent"] == "conversation" and "запутал диалог" in complaint["explanation"]
        advice = ask("S-0011", "сам мне расскажи че дальше делать")
        assert advice["intent"] == "search" and advice["references"][0]["pdf_page"] == 53, advice
        stop = ask("S-0011", "на этом всё")
        assert stop["intent"] == "conversation" and "остановимся" in stop["explanation"]
        oil = ask("S-0011", "так ну достаточно масла")
        assert oil["fluid_check"] == {"oil": "ok"} and "охлаждающей жидкости" in oil["clarifying_question"], oil
        for question in ("тебя спросить надо", "а что дальше мне-то сделать надо"):
            next_check = ask("S-0011", question)
            assert next_check["clarifying_question"] == oil["clarifying_question"], next_check
        both = ask("S-0011", "все достаточно")
        assert both["fluid_check"] == {"oil": "ok", "coolant": "ok"}, both
        assert "манометра" in both["checks"][0]["text"] and both["references"][0]["pdf_page"] == 53
        guidance = ask("S-0011", "подскажи")
        assert guidance["checks"] == both["checks"], guidance
        low_level = ask("S-0011", "недостаточно")
        assert "моторного масла" in low_level["clarifying_question"], low_level
        moved = ask("S-0011", "Можно ли своим ходом перегнать машину на базу?")
        assert moved["clarifying_question"] and "перегон" in moved["verdict"]
        assert any(r["pdf_page"] == 19 for r in moved["references"]), moved
        bare = ask("S-0011", "350")
        assert bare["intent"] == "movement" and "350 — это" in bare["clarifying_question"]
        assert not bare["checks"]
        partial = ask("S-0011", "да, кПа")
        assert partial["pressure_measurement"]["pressure"] == "350"
        assert "прогрет?" in partial["clarifying_question"] and not partial["checks"]
        low = ask("S-0011", "Двигатель прогрет, 2100 об/мин, без допуска после длительной эксплуатации")
        assert "350 кПа — ниже" in low["checks"][0]["text"], low
        assert low["references"][0]["pdf_page"] == 19
        within = ask("S-0011", "Перемерил: 450 кПа")
        assert "450 кПа соответствует" in within["checks"][0]["text"], within
        assert "не подтверждена" in within["verdict"] and not within["clarifying_question"]
        cold = ask("S-0011", "На холодном двигателе 450 кПа")
        assert "после прогрева" in cold["clarifying_question"]
        switched = ask("S-0010", "А что проверить сначала?")
        assert switched["ml_observations"]["session_id"] == "S-0010"
        assert switched["ml_observations"]["status"] == "normal" and not switched["checks"]
        failed = ask("S-0003", "Покажи диагностику", "P456AT178")
        assert failed["ml_observations"]["confidence"] is None and not failed["references"]
        assert "Повторите" in failed["verdict"] and "микрофона" in failed["ml_observations"]["description"]
        conflict = ask("S-0132", "Что проверить?", "Y652CK98")
        assert conflict["admission"]["block_reason"]["code"] == "fault_engine_mismatch"
        assert "F08" in conflict["explanation"] and "маркировку" in conflict["clarifying_question"]
        oil_draft = ask("S-0011", "Подготовь заявку на дефектовку")
        assert oil_draft["admission"]["can_prepare"] and not oil_draft["admission"]["can_submit"], oil_draft
        assert oil_draft["ticket_draft"]["operations"] == [
            "Проверить достаточность уровня масла в картере и уровня охлаждающей жидкости."
        ], oil_draft
        assert [item["pdf_page"] for item in oil_draft["references"]] == [53], oil_draft
        for question, pages in (
            ("Какие тепловые зазоры клапанного механизма?", {31, 269, 276}),
            ("Какое давление масла на прогретом двигателе?", {19, 12}),
            ("Какой момент затяжки болтов крышек коренных подшипников?", {41, 248}),
        ):
            response = ask("S-0011", question)
            assert {item["pdf_page"] for item in response["references"]} == pages, response
        draft = ask("S-0152", "Подготовь заявку на дефектовку", "Y815BM178")
        assert draft["ticket_draft"] and draft["ticket_draft"]["operations"], draft
        assert draft["references"][0]["pdf_page"] == 143, draft
        # The legacy dialogue sandbox has no alternate route around Workflow admission.
        payload = dict(vehicle_id="Y815BM178", session_id="S-0152", draft_id=draft["draft_id"])
        result = client.post("/api/confirm", json=payload)
        assert result.status_code == 409 and "основной экран" in result.json["error"], result.data
        url = draft["references"][0]["url"].split("#")[0]
        assert client.get(url).mimetype == "application/pdf"
        print("Перегон, конфликты руководств, проверенный черновик F14 и закрытый обход отправки: OK")


if __name__ == "__main__":
    main()
