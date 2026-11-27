import re


DRAFT_REQUEST = re.compile(
    r"^(?:(?:пожалуйста[, ]+|давай(?:те)?\s+|ну\s+|а\s+))*"
    r"(?:созда(?:й(?:те)?|ть)|оформ(?:и(?:те)?|ить)|подготов(?:ь(?:те)?|ить)|сдела(?:й(?:те)?|ть))"
    r"\s+(?:(?:мне|новую|новый)\s+)*(?:заявк[ауи]|наряд|черновик)\b"
)


def wants_draft(question):
    request = " ".join(question.casefold().split())
    return bool(DRAFT_REQUEST.match(request) or re.fullmatch(r"заявка(?: на дефектовку)?[.!]?", request))


def prepare(reply):
    ml = reply["ml_observations"]
    if ml["status"] != "anomaly" or not reply["checks"]:
        return None
    references = {
        item.get("fragment_id"): item for item in reply["references"]
        if isinstance(item, dict) and item.get("fragment_id") and item.get("manual")
        and type(item.get("pdf_page")) is int and item["pdf_page"] > 0
    }
    checks = reply["checks"]
    if any(not isinstance(item, dict) or not isinstance(item.get("text"), str)
           or not item["text"].strip() or item.get("fragment_id") not in references for item in checks):
        return None
    pages = dict.fromkeys((references[item["fragment_id"]]["manual"], references[item["fragment_id"]]["pdf_page"])
                          for item in checks)
    return dict(vehicle_id=ml["vehicle_id"], session_id=ml["session_id"],
                anomaly_type=ml["anomaly_type"], conclusion=reply["verdict"] + " " + ml["description"],
                operations=[item["text"] for item in checks], urgency="medium", spare_parts=[],
                references=[{"manual": manual, "pdf_page": page} for manual, page in pages])
