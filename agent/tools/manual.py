import json
from pathlib import Path
from manual_search.corpus import verified_fragments
from manual_search.search import ManualSearch


class Manuals:
    def __init__(self, index="artifacts/manual-corpus.json"):
        corpus = json.loads(Path(index).read_text(encoding="utf-8"))
        fragments = {item["fragment_id"]: item for item in corpus["fragments"]}
        manuals = {item["manual_id"] for item in fragments.values()}
        # Apply reviewed corrections to already prepared indexes for their manuals.
        fragments.update((item["fragment_id"], item) for item in verified_fragments()
                         if item["manual_id"] in manuals)
        self.searcher = ManualSearch({"fragments": list(fragments.values())})
        self.files = {e["manual_id"]: e["source_file"] for e in self.searcher.fragments.values()}
        self.urls = {key: "/manuals/" + key for key in self.files}

    def find(self, query, diagnosis):
        return self.searcher.search(query, diagnosis["engine_model"], diagnosis["anomaly_type"], limit=3)

    def reference(self, fragment_id, model):
        return dict(fragment_id=fragment_id, **self.searcher.resolve_source(fragment_id, model, self.urls))
