def diagnose(backend, vehicle_id, session_id):
    return describe(backend.diagnose(vehicle_id, session_id))


def describe(result):
    verdict = {
        "normal": "По записи признаков отклонения нет. Если осталась жалоба, уточните симптом.",
        "check_failed": f"Проверка не состоялась. {result['description']} Повторите запись.",
        "anomaly": f"Обнаружено отклонение: {result['anomaly_title']}.",
    }[result["status"]]
    return dict(vehicle_id=result["vehicle_id"], session_id=result["session_id"], verdict=verdict,
                ml_observations=result, possible_causes=[], risk=[], checks=[], repair_actions=[],
                risk_level="unassessed", references=[], clarifying_question=None, ticket_draft=None)
