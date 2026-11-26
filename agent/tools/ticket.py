def prepare(reply):
    ml = reply["ml_observations"]
    if ml["status"] != "anomaly" or not reply["checks"]:
        return None
    return dict(vehicle_id=ml["vehicle_id"], session_id=ml["session_id"],
                anomaly_type=ml["anomaly_type"], conclusion=reply["verdict"] + " " + ml["description"],
                operations=[item["text"] for item in reply["checks"]], urgency="medium", spare_parts=[],
                references=[{k: r[k] for k in ("manual", "pdf_page")} for r in reply["references"]])
