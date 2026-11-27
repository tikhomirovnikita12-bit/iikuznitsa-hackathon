import json
from pathlib import Path
from backend.mechanic import load_defect_checks
from manual_search.corpus import load_catalog, verified_fragments
from manual_search.search import ManualSearch


class Manuals:
    def __init__(self, index="artifacts/manual-corpus.json"):
        corpus = json.loads(Path(index).read_text(encoding="utf-8"))
        catalog = load_catalog()
        fragments = {item["fragment_id"]: item for item in corpus["fragments"]}
        manuals = {item["manual_id"] for item in fragments.values()}
        # Apply reviewed corrections to already prepared indexes for their manuals.
        fragments.update((item["fragment_id"], item) for item in verified_fragments(catalog)
                         if item["manual_id"] in manuals)
        self.fault_checks, extra = load_defect_checks(catalog)
        self.searcher = ManualSearch({"fragments": list(fragments.values())})
        self.checked_sources = ManualSearch({"fragments": extra})
        self.files = {
            entry["manual_id"]: entry["source_file"]
            for index in (self.searcher, self.checked_sources)
            for entry in index.fragments.values()
        }
        self.urls = {key: "/manuals/" + key for key in self.files}

    def find(self, query, diagnosis, *, initial_check=False):
        hits = self.searcher.search(query, diagnosis["engine_model"], diagnosis["anomaly_type"], limit=3)

        if initial_check:
            check = self.fault_checks.get(diagnosis["engine_model"], {}).get(diagnosis["anomaly_type"])
            if check and not check[2]:
                fragment = self.checked_sources.fragments[check[0]]
                return [fragment, *(hit for hit in hits if hit["fragment_id"] != check[0])][:3]
        return hits

    def reference(self, fragment_id, model):
        index = self.checked_sources if fragment_id in self.checked_sources.fragments else self.searcher
        return dict(fragment_id=fragment_id, **index.resolve_source(fragment_id, model, self.urls))
