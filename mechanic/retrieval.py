import json
import os
import sys
from pathlib import Path

from .config import CORPUS, ROOT
from .contracts import Reference
from .sources import YAMZ236, ZMZ409


SEARCH_QUESTIONS = {
    (YAMZ236.name, 19): "При каких условиях проверять давление масла?",
    (YAMZ236.name, 53): "Какие причины пониженного давления указаны в руководстве?",
    (ZMZ409.name, 107): "Что вызывает равномерный шум в передней части двигателя на средних оборотах?",
}


def key(reference: Reference):
    return reference.manual, reference.pdf_page


class Retrieval:
    def __init__(self, index):
        self.index = index

    def candidates(self, messages, engine, fault):
        hits = {}
        for prompt in messages:
            for fragment in self.index.search(prompt, engine, fault, limit=5):
                hits.setdefault(fragment["fragment_id"], fragment)
        return list(hits.values())

    def resolve(self, fragment_id: str, engine: str) -> Reference:
        return Reference.model_validate(self.index.resolve_source(fragment_id, engine))

    def validate_fixture(self, references, engine, fault, question):
        targets = {key(reference) for reference in references if key(reference) in SEARCH_QUESTIONS}
        if not targets:
            return {}
        prompts = [question] if question else []
        prompts.extend(SEARCH_QUESTIONS[target] for target in sorted(targets))
        resolved = {}
        for hit in self.candidates(prompts, engine, fault):
            candidate = self.resolve(hit["fragment_id"], engine)
            if key(candidate) in targets:
                resolved[key(candidate)] = candidate
        if targets != resolved.keys():
            raise ValueError("Поиск не вернул нужные страницы для выбранной модификации.")
        return resolved


def load_retrieval(preferred_checkout: Path | None = None) -> Retrieval | None:
    checkout = ROOT / os.environ.get("TEAM_MANUALS_DIR", str(preferred_checkout or ROOT))
    if not (checkout / "manual_search").is_dir() or not CORPUS.is_file():
        return None
    sys.path.insert(0, str(checkout.resolve()))
    from manual_search.search import ManualSearch
    return Retrieval(ManualSearch(json.loads(CORPUS.read_text(encoding="utf-8"))))
