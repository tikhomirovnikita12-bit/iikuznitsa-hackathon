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
    used = dict.fromkeys(item["fragment_id"] for item in checks)
    return dict(vehicle_id=ml["vehicle_id"], session_id=ml["session_id"],
                anomaly_type=ml["anomaly_type"], conclusion=reply["verdict"] + " " + ml["description"],
                operations=[item["text"] for item in checks], urgency="medium", spare_parts=[],
                references=[{key: references[fragment][key] for key in ("manual", "pdf_page")} for fragment in used])
