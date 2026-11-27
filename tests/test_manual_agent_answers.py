"""Reviewed oil-pressure norms and source-passage deduplication in agent replies."""

import json
from pathlib import Path

import pytest

from agent.dialog import Agent, passages
from agent.tools.diagnosis import describe
from agent.tools.manual import Manuals
from manual_search.corpus import verified_fragments


ROOT = Path(__file__).resolve().parents[1]
NORM = 'yamz-536-01-02-rk-p013-oil-pressure'
QUESTION = 'Какое давление масла на прогретом двигателе?'


def diagnosis(vehicle_id, session_id):
    fleet = json.loads((ROOT / 'data/vehicles.json').read_text(encoding='utf-8'))['vehicles']
    card = next(vehicle for vehicle in fleet if vehicle['vehicle_id'] == vehicle_id)
    check = next(session for session in card['sessions'] if session['session_id'] == session_id)
    return {**check, 'engine_model': card['engine_model'], 'fuel': card['fuel']}


class NoModel:
    def ask(self, *_args):
        raise AssertionError('A complete reviewed norm must not be discarded by a classifier')


def test_yamz536_pressure_answer_keeps_conditions_units_and_source(tmp_path):
    manuals = Manuals(ROOT / 'artifacts/manual-corpus.json')
    agent = Agent(None, manuals, NoModel(), log=tmp_path / 'turns.jsonl')
    result = agent.turn('A464EP47', 'S-0096', QUESTION,
                        diagnosis=diagnosis('A464EP47', 'S-0096'))
    assert result['intent'] == 'search'
    assert result['clarifying_question'] is None
    assert len(result['checks']) == 2
    text = result['checks'][0]['text']
    for term in ['80–100 °C', '400–635 кПа', '4,1–6,5 кгс/см²', 'номинальной']:
        assert term in text
    assert '1000' not in text
    for term in ['минимальной', 'не менее 100 кПа', '1,0 кгс/см²']:
        assert term in result['checks'][1]['text']
    assert result['references'][0]['fragment_id'] == NORM
    assert [(ref['manual'], ref['pdf_page']) for ref in result['references']] == [
        ('536.01.02 РК', 13), ('536.3902150 РЭ', 49),
    ]


def test_reviewed_norm_overlays_old_index_without_leaking_other_engine(tmp_path):
    path = tmp_path / 'old-corpus.json'
    raw = {
        'fragment_id': 'yamz-536-01-02-rk-p013-l001',
        'manual_id': 'yamz-536-01-02-rk',
        'manual': '536.01.02 РК',
        'pdf_page': 13,
        'source_file': 'yamz-536_rk_2013.pdf',
        'section': 'Техническая характеристика двигателей',
        'applicable_engine_models': ['ЯМЗ-536'],
        'text': 'Давление масла в системе смазки двигателя, прогретого до\n80-1000 С, кПа',
    }
    path.write_text(json.dumps({'fragments': [raw]}, ensure_ascii=False), encoding='utf-8')
    manuals = Manuals(path)
    assert '80–100 °C' in manuals.searcher.fragments[raw['fragment_id']]['text']
    context = diagnosis('A464EP47', 'S-0096')
    hits = manuals.find(QUESTION, context)
    norm = hits[0]
    assert norm['fragment_id'] == NORM
    assert passages(norm) == [norm['text']], 'Do not split the table into disconnected values'
    other = diagnosis('E549YT198', 'S-0011')
    assert all(hit['fragment_id'] != NORM for hit in manuals.find(QUESTION, other))


class ChoosePassages:
    def __init__(self, *, duplicate=True, cross_field=False):
        self.duplicate = duplicate
        self.cross_field = cross_field

    def ask(self, _prompt, response_schema, _trace):
        options = response_schema['properties']['items']['items']['properties']['source']['enum']
        return {'items': [
            {'field': 'checks', 'source': options[0]},
            {'field': 'risk' if self.cross_field else 'checks',
             'source': options[0] if self.duplicate else options[1]},
        ]}


def chain_select(tmp_path, provider, text=None):
    manuals = Manuals(ROOT / 'artifacts/manual-corpus.json')
    agent = Agent(None, manuals, provider, log=tmp_path / 'turns.jsonl')
    context = diagnosis('Y815BM178', 'S-0152')
    reply = describe(context)
    hit = next(fragment for fragment in verified_fragments()
               if fragment['fragment_id'] == 'zmz-40904-40905-rk-p107-engine-knock')
    if text is not None:
        hit = {**hit, 'text': text}
    agent.select(reply, 'search', 'Почему шумит цепь?', [hit], {'calls': []})
    return reply


@pytest.mark.parametrize('cross_field', [False, True])
def test_chain_selected_passage_is_not_repeated(tmp_path, cross_field):
    reply = chain_select(tmp_path, ChoosePassages(cross_field=cross_field))
    items = [item for field in ('checks', 'risk', 'possible_causes') for item in reply[field]]
    assert len(items) == 1
    assert len(reply['references']) == 1
    assert reply['references'][0]['pdf_page'] == 107


def test_distinct_passages_from_one_fragment_are_preserved(tmp_path):
    reply = chain_select(tmp_path, ChoosePassages(duplicate=False),
                         text='Проверить увеличение длины цепей.\nПроверить гидронатяжитель цепи.')
    assert len(reply['checks']) == 2
    assert reply['checks'][0]['text'] != reply['checks'][1]['text']
    assert len(reply['references']) == 1


def test_personal_message_still_goes_through_relevance_filter(tmp_path):
    class Unrelated:
        def ask(self, _prompt, response_schema, _trace):
            if 'action' in response_schema['properties']:
                return {'action': 'off_topic', 'query': '', 'question': ''}
            return {'topic': 'unrelated'}

    agent = Agent(None, Manuals(ROOT / 'artifacts/manual-corpus.json'), Unrelated(),
                  log=tmp_path / 'turns.jsonl')
    result = agent.turn('A464EP47', 'S-0096', 'У Киану Ривза Ford Mustang',
                        diagnosis=diagnosis('A464EP47', 'S-0096'))
    assert result['intent'] == 'conversation'
    assert result['checks'] == []
    assert result['references'] == []
