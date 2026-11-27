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
    fragments = {item["fragment_id"] for item in reply["checks"]}
    references = list(dict.fromkeys(
        (ref["manual"], ref["pdf_page"]) for ref in reply["references"] if ref["fragment_id"] in fragments
    ))
    if not references:
        return None
    return dict(vehicle_id=ml["vehicle_id"], session_id=ml["session_id"],
                anomaly_type=ml["anomaly_type"], conclusion=reply["verdict"] + " " + ml["description"],
                operations=[item["text"] for item in reply["checks"]], urgency="medium", spare_parts=[],
                references=[{"manual": manual, "pdf_page": page} for manual, page in references])
