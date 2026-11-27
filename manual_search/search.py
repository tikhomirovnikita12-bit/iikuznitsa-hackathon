"""Search applicable manual fragments and resolve their source references."""

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .corpus import DATA_DIR, correct_extracted_fragment, load_catalog

STOP_WORDS = set("""
а в во для до и из или к как какие какой какая каких какие когда ли на над
не по под при про с со у что это чем нужно можно указан указано руководстве
""".split())
COMMON_WORDS = {"двигател", "машин", "мотор", "руковод"}
WORD = re.compile(r"[a-zа-яё0-9]+", re.I)
RUSSIAN_ENDING = re.compile(r"(?:иями|ами|ями|ого|ему|ыми|ими|ая|яя|ое|ые|ие|ых|их|ий|ый|ой|ам|ям|ом|ем|ов|ев|ия|ие|ья|[аеиоуыяюь])$")


def normalized_words(text):
    text = re.sub(r"(?<=[а-яё])[-–]\s*\n\s*(?=[а-яё])", "", text.lower())
    return WORD.findall(text.replace("ё", "е"))


def term(word):
    if len(word) > 4 and re.fullmatch(r"[а-я]+", word):
        return RUSSIAN_ENDING.sub("", word)
    return word


def tokens(words):
    return [term(word) for word in words if word not in STOP_WORDS]


class ManualSearch:
    def __init__(self, corpus):
        self.catalog = {manual["manual_id"]: manual for manual in load_catalog()}
        self.fragments = {
            entry["fragment_id"]: correct_extracted_fragment(entry) for entry in corpus["fragments"]
        }
        self.by_model = defaultdict(list)
        self.frequencies = defaultdict(Counter)
        terms = json.loads((DATA_DIR / "search-terms.json").read_text(encoding="utf-8"))
        self.fault_terms = terms["fault_terms"]
        self.synonyms = [
            [set(tokens(normalized_words(phrase))) for phrase in group]
            for group in terms["synonyms"]
        ]
        for fragment_id, entry in self.fragments.items():
            if entry.get("reference_only") or entry.get("conflict_only"):
                continue
            section = tokens(normalized_words(entry["section"]))
            body = tokens(normalized_words(entry["text"]))
            tags = tokens(normalized_words(" ".join(entry.get("search_terms", ()))))
            counts = Counter(section + body + tags)
            for model in entry["applicable_engine_models"]:
                self.by_model[model].append((fragment_id, counts))
                self.frequencies[model].update(counts.keys())

    def search(self, question, engine_model, anomaly_type=None, *, limit=5):
        """Return ranked fragments. The agent decides whether evidence answers the question."""
        word_list = normalized_words(question)
        normalized = " ".join(word_list)
        query = set(tokens(word_list)) - COMMON_WORDS
        if not query or limit < 1:
            return []

        alternatives = [
            phrase
            for group in self.synonyms
            if any(phrase <= query for phrase in group)
            for phrase in group
        ]
        aliases = set().union(*alternatives) - query
        context = self.fault_terms.get(anomaly_type, ())
        context_terms = set(tokens(normalized_words(" ".join(context)))) - COMMON_WORDS - query
        rows = self.by_model.get(engine_model, ())
        frequency = self.frequencies.get(engine_model, {})
        results = []
        for fragment_id, counts in rows:
            matches = (query | aliases) & counts.keys()
            direct = query & counts.keys()
            if not matches:
                continue
            if not direct and not any(phrase <= counts.keys() for phrase in alternatives):
                continue
            entry = self.fragments[fragment_id]
            coverage = min(1.0, (len(direct) + 0.6 * len(matches - direct)) / len(query))
            if coverage < 0.4:
                continue

            weights = sum(min(math.log1p(len(rows) / frequency[word]), 5.0) for word in matches)
            context_bonus = min(len(context_terms & counts.keys()), 4) * 0.25
            section_words = set(tokens(normalized_words(entry["section"])))
            section_bonus = 0.5 * len(query & section_words)
            exact_bonus = 2.0 if len(word_list) > 1 and normalized in " ".join(
                normalized_words(entry["text"])
            ) else 0
            verified_bonus = 3.0 if entry.get("verified") and coverage >= 0.6 else 0
            score = (
                weights * coverage + context_bonus + section_bonus + exact_bonus + verified_bonus
            )
            results.append((score, entry))

        results.sort(key=lambda row: (-row[0], row[1]["fragment_id"]))
        hits = []
        seen_pages = set()
        for _, entry in results:
            page = (entry["manual_id"], entry["pdf_page"])
            if page not in seen_pages:
                hits.append(entry)
                seen_pages.add(page)
            if len(hits) == limit:
                break
        return hits

    def resolve_source(self, fragment_id, engine_model, manual_urls=None):
        """Resolve an agent-returned ID against the current engine and URL registry."""
        entry = self.fragments.get(fragment_id)
        if entry is None or engine_model not in entry["applicable_engine_models"]:
            raise ValueError("Source does not apply to the selected engine")
        source = {"manual": entry["manual"], "pdf_page": entry["pdf_page"]}
        url = (manual_urls or {}).get(entry["manual_id"])
        if url:
            parts = urlsplit(url)
            source["url"] = urlunsplit(parts._replace(fragment=f"page={entry['pdf_page']}"))
        return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=Path)
    parser.add_argument("engine_model")
    parser.add_argument("question")
    parser.add_argument("--fault")
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()
    engine = ManualSearch(json.loads(args.index.read_text(encoding="utf-8")))
    matches = engine.search(args.question, args.engine_model, args.fault, limit=args.limit)
    print(json.dumps(matches, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
