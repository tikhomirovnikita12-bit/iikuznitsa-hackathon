def diagnose(backend, vehicle_id, session_id):
    result = backend.diagnose(vehicle_id, session_id)
    verdict = {
        "normal": "По записи признаков отклонения нет. Можно продолжить эксплуатацию.",
        "check_failed": "Запись непригодна для оценки двигателя. Повторите запись.",
        "anomaly": f"Обнаружено отклонение: {result['anomaly_title']}.",
    }[result["status"]]
    return dict(vehicle_id=vehicle_id, session_id=session_id, verdict=verdict,
                ml_observations=result, possible_causes=[], risk=[], checks=[],
                references=[], clarifying_question=None, ticket_draft=None)
