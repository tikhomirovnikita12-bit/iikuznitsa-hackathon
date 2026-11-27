import json
from pathlib import Path
from backend.mechanic import load_defect_checks
from manual_search.corpus import load_catalog
from manual_search.search import ManualSearch


class Manuals:
    def __init__(self, index="artifacts/manual-corpus.json"):
        corpus = json.loads(Path(index).read_text(encoding="utf-8"))
        self.fault_checks, extra = load_defect_checks(load_catalog())
        known = {entry["fragment_id"] for entry in corpus["fragments"]}
        corpus["fragments"].extend(entry for entry in extra if entry["fragment_id"] not in known)
        self.searcher = ManualSearch(corpus)
        self.files = {e["manual_id"]: e["source_file"] for e in self.searcher.fragments.values()}
        self.urls = {key: "/manuals/" + key for key in self.files}

    def find(self, query, diagnosis, *, initial_check=False):
        hits = self.searcher.search(query, diagnosis["engine_model"], diagnosis["anomaly_type"], limit=3)

        if initial_check:
            check = self.fault_checks.get(diagnosis["engine_model"], {}).get(diagnosis["anomaly_type"])
            if check and not check[2]:
                fragment = self.searcher.fragments[check[0]]
                return [fragment, *(hit for hit in hits if hit["fragment_id"] != check[0])][:3]
        return hits

    def reference(self, fragment_id, model):
        return dict(fragment_id=fragment_id, **self.searcher.resolve_source(fragment_id, model, self.urls))
