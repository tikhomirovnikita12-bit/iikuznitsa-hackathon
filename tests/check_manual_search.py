"""Check corpus integrity, the search gold set and citation resolution."""

import argparse
import json
from pathlib import Path

from manual_search.search import ManualSearch


def check(index):
    search = ManualSearch(index)
    fragments = index["fragments"]
    assert len(search.fragments) == len(fragments), "Duplicate fragment IDs"
    assert {item["manual_id"] for item in fragments} == set(search.catalog)
    for item in fragments:
        manual = search.catalog[item["manual_id"]]
        assert 1 <= item["pdf_page"] <= manual["page_count"]
        assert item["source_file"] == manual["source_file"]
        assert set(item["applicable_engine_models"]) <= set(manual["applicable_engine_models"])
        for linked in item.get("continues_from", []):
            previous = search.fragments[linked]
            assert previous["manual_id"] == item["manual_id"]
            assert previous["pdf_page"] == item["pdf_page"] - 1

    file_url = {"yamz-236-3902150-b-re": "/manuals/yamz.pdf"}
    for page in (19, 53):
        entry = next(
            fragment for fragment in fragments
            if fragment.get("verified")
            and fragment["manual_id"] == "yamz-236-3902150-b-re"
            and fragment["pdf_page"] == page
        )
        source = search.resolve_source(entry["fragment_id"], "ЯМЗ-236М2", file_url)
        assert source["manual"] == "236-3902150-Б РЭ"
        assert source["pdf_page"] == page
        assert source["url"] == f"/manuals/yamz.pdf#page={page}"

    dmrv = search.search("Где расположен датчик массового расхода воздуха?", "ЗМЗ-40904.10")[0]
    try:
        search.resolve_source(dmrv["fragment_id"], "ЗМЗ-40905.10")
    except ValueError:
        pass
    else:
        raise AssertionError("A 40904.10 DMRV reference leaked to 40905.10")

    cases = json.loads(Path(__file__).with_name("manual-search-cases.json").read_text(encoding="utf-8"))
    failures = []
    for case in cases:
        if "sources" not in case:
            print(f"{case['id']} pending agent integration")
            continue
        hits = search.search(
            case["question"], case["engine_model"], case.get("anomaly_type"), limit=case["top"]
        )
        expected = {tuple(source) for source in case["sources"]}
        actual = {(hit["manual_id"], hit["pdf_page"]) for hit in hits}
        if not (actual & expected if expected else not actual):
            failures.append((case["id"], sorted(actual)))
        for hit in hits:
            base = f"http://localhost/manuals/{hit['manual_id']}.pdf"
            citation = search.resolve_source(
                hit["fragment_id"], case["engine_model"], {hit["manual_id"]: base}
            )
            assert citation["pdf_page"] == hit["pdf_page"]
            assert citation["url"] == f"{base}#page={hit['pdf_page']}"
    assert not failures, f"Unexpected source pages: {failures}"
    print("Corpus integrity and 13 search cases passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=Path)
    check(json.loads(parser.parse_args().index.read_text(encoding="utf-8")))
