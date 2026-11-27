"""Reviewed source groups and server decisions survive both agent adapters."""
import json
from pathlib import Path

import pytest

from agent.dialog import Agent, incompatible_fault
from agent.tools.manual import Manuals
from backend.admission import incompatible_fault as server_incompatible_fault
from backend.mechanic import Mechanic, conflict_case
from backend.models import Diagnosis as BackendDiagnosis
from mechanic.case_agent import CaseAgent
from mechanic.contracts import Diagnosis

ROOT = Path(__file__).resolve().parents[1]


def context(vehicle_id='E549YT198', session_id='S-0011'):
    fleet = json.loads((ROOT / 'data/vehicles.json').read_text(encoding='utf-8'))['vehicles']
    card = next(row for row in fleet if row['vehicle_id'] == vehicle_id)
    session = next(row for row in card['sessions'] if row['session_id'] == session_id)
    return card, {**session, 'engine_model': card['engine_model'], 'fuel': card['fuel']}


class NoModel:
    def ask(self, *_args, **_kwargs):
        raise AssertionError('A reviewed decision must not depend on model selection')


@pytest.mark.parametrize('question,pages', [
    ('Какие тепловые зазоры клапанного механизма?', {31, 269, 276}),
    ('Какое давление масла должно быть на прогретом двигателе?', {19, 12}),
    ('Какой момент затяжки болтов крышек коренных подшипников?', {41, 248}),
])
def test_known_conflicts_return_every_reviewed_source(question, pages, tmp_path):
    card, ml = context()
    case = conflict_case(question, card['engine_model'])
    assert case, 'The reviewed conflict must be registered'
    mechanic = Mechanic(ROOT / 'artifacts/manual-corpus.json', ROOT / 'artifacts/manuals')
    answer = mechanic.answer(question, BackendDiagnosis.model_validate(ml), card['engine_model'])
    assert {item.source.pdf_page for item in answer.evidence} == pages
    assert case['explanation'] in answer.answer
    assert all(item.text in answer.answer for item in answer.evidence)
    agent = Agent(None, Manuals(ROOT / 'artifacts/manual-corpus.json'), NoModel(),
                  log=tmp_path / 'conflicts.jsonl')
    result = agent.turn(ml['vehicle_id'], ml['session_id'], question, diagnosis=ml)
    assert {item['pdf_page'] for item in result['references']} == pages
    assert {item['fragment_id'] for item in result['checks']} == set(case['fragment_ids'])
    assert result['ticket_draft'] is None
    assert 'Ограничения:' in result['clarifying_question']


def test_f02_without_control_manometer_procedure_is_documentary_only(tmp_path):
    card, ml = context()
    agent = CaseAgent(ROOT / 'artifacts/manual-corpus.json', tmp_path / 'blocked.jsonl')
    agent.agent.provider = NoModel()
    assert not agent.can_prepare(card, Diagnosis.model_validate(ml))
    reply, proposal, _entry = agent.chat(card, Diagnosis.model_validate(ml), 'Подготовь заявку', [])
    assert proposal is None
    assert 'процедур' in reply.summary
    assert any('дефектовк' in item for section in reply.sections for item in section.items)


def test_agent_uses_same_engine_compatibility_rule_as_server():
    for plate, session in [('Y652CK98', 'S-0132'), ('M333YK47', 'S-0005')]:
        card, ml = context(plate, session)
        assert incompatible_fault(ml) == server_incompatible_fault(card, ml)


def test_movement_reply_does_not_turn_priority_into_a_prohibition(tmp_path):
    _card, ml = context()
    agent = Agent(None, Manuals(ROOT / 'artifacts/manual-corpus.json'), NoModel(), log=tmp_path / 'movement.jsonl')
    result = agent.turn(ml['vehicle_id'], ml['session_id'], 'Можно ли своим ходом перегнать машину?', diagnosis=ml)
    assert result['ticket_draft'] is None
    assert result['clarifying_question']
    assert 'Приоритет заявки не является запретом' in result['verdict']
    assert 'Прямой запрет из руководства здесь не установлен' in result['movement_limitations']
    assert any(item['pdf_page'] == 19 for item in result['references'])


def test_pressure_reading_and_movement_take_precedence_over_conflict_lookup(tmp_path):
    card, ml = context()
    question = 'Измерили давление масла 90 кПа, можно ли своим ходом перегнать машину?'
    mechanic = Mechanic(ROOT / 'artifacts/manual-corpus.json', ROOT / 'artifacts/manuals')
    answer = mechanic.answer(question, BackendDiagnosis.model_validate(ml), card['engine_model'])
    assert answer.needs_clarification
    assert 'обороты' in answer.answer and 'прогрева' in answer.answer
    agent = Agent(None, Manuals(ROOT / 'artifacts/manual-corpus.json'), NoModel(), log=tmp_path / 'reading.jsonl')
    result = agent.turn(ml['vehicle_id'], ml['session_id'], question, diagnosis=ml)
    assert result['intent'] == 'movement'
    assert result['pressure_measurement']['pressure'] == '90'
    assert result['clarifying_question']
    assert 'прогрет' in result['clarifying_question']
    assert result['ticket_draft'] is None


@pytest.mark.parametrize('question,summary', [
    ('Давление масла 90 кПа, можно ли перегнать машину?', 'Данных для допуска к перегону'),
    ('Измерили давление масла 90 кПа. Это нормально?', 'Для оценки замера уточните'),
])
def test_fixture_reading_and_travel_precede_conflict(question, summary):
    from mechanic.demo_agent import answer

    card, ml = context()
    reply = answer(card, Diagnosis.model_validate(ml), question)
    assert summary in reply.summary
    pages = {reference.pdf_page for section in reply.sections for reference in section.references}
    assert 19 in pages
    assert 12 not in pages, 'The dedicated reading/travel path must not become a norm-conflict answer'


def test_legacy_flask_confirm_cannot_bypass_admission():
    pytest.importorskip('flask')
    from agent.server import create_app

    class NoSubmission:
        def submit(self, _draft):
            raise AssertionError('Legacy confirmation must never call TOiR')

    class StubAgent:
        backend = NoSubmission()

    response = create_app(StubAgent()).test_client().post('/api/confirm', json={})
    assert response.status_code == 409
    assert 'основной экран' in response.get_json()['error']
