"""Reviewed evidence keeps engine, page and operating conditions together."""
import json
from pathlib import Path

from manual_search.corpus import DATA_DIR, verified_fragments
from manual_search.search import ManualSearch


def test_approved_work_and_conflicts_resolve_reviewed_engine_sources():
    fragments = {item['fragment_id']: item for item in verified_fragments()}
    operations = json.loads((DATA_DIR / 'approved-operations.json').read_text(encoding='utf-8'))
    for operation in operations:
        source = fragments[operation['fragment_id']]
        assert source['text'] == operation['quote']
        assert source['manual_id'] == operation['manual_id']
        assert source['pdf_page'] == operation['pdf_page']
        assert operation['engine_model'] in source['applicable_engine_models']
        for linked in operation.get('supporting_fragment_ids', []):
            assert operation['engine_model'] in fragments[linked]['applicable_engine_models']
    fuel, = [row for row in operations if row['anomaly_type'] == 'F08']
    assert fuel['conditions'] == [{
        'id': 'initial_start_or_long_storage',
        'text': 'Это подготовка к первичному пуску нового двигателя или к пуску после длительной стоянки.',
        'requires_confirmation': True,
    }]
    assert not any(row['engine_model'] == 'ЯМЗ-236М2' and row['anomaly_type'] == 'F02' for row in operations)
    search = ManualSearch({'fragments': list(fragments.values())})
    expected = {
        'yamz236-valve-clearance': [31, 269, 276],
        'yamz236-oil-pressure': [19, 12],
        'yamz236-main-bearing-bolts': [41, 248],
    }
    conflicts = json.loads((DATA_DIR / 'conflicts.json').read_text(encoding='utf-8'))
    for conflict in conflicts:
        sources = [search.resolve_source(key, conflict['engine_model']) for key in conflict['fragment_ids']]
        assert [source['pdf_page'] for source in sources] == expected[conflict['id']]
        assert all(fragments[key]['conflict_only'] for key in conflict['fragment_ids'])
