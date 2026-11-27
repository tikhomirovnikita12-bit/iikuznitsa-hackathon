"""Main-screen API enforces the same admission as the backend, with no UI trust."""
import pytest
from test_draft_flow import integrated

CHAIN = {"vehicle_id": "Y815BM178", "session_id": "S-0152"}
WARNING = {"vehicle_id": "O658MX98", "session_id": "S-0063"}
FUEL = {"vehicle_id": "M333YK47", "session_id": "S-0005"}


def prepare(client, pair):
    response = client.post('/api/diagnose', json=pair)
    assert response.status_code == 200, response.text
    response = client.post('/api/draft', json=pair)
    assert response.status_code == 200, response.text
    state = response.json()
    fields = state['draft']
    return state, {**pair, 'draft_id': state['draft_id'], 'revision': state['draft_revision'],
                   'edits': {key: fields[key] for key in ('conclusion', 'urgency', 'operations', 'spare_parts')},
                   'diagnosis_revision': state['admission']['diagnosis_revision'], 'priority_confirmed': True}


@pytest.mark.parametrize('pair,code', [
    ({'vehicle_id':'M739BA198','session_id':'S-0103'},'confidence_too_low'),
    ({'vehicle_id':'P456AT178','session_id':'S-0003'},'recording_invalid'),
    ({'vehicle_id':'Y652CK98','session_id':'S-0132'},'fault_engine_mismatch'),
    ({'vehicle_id':'O710HH78','session_id':'S-0014'},'procedure_unavailable'),
])
def test_screen_blocks_unsafe_drafts_and_chat_without_delivery(integrated, pair, code):
    api, _adapter, sent = integrated
    result = api.post('/api/diagnose', json=pair).json()
    assert not result['draft_available']
    assert result['admission']['block_reason']['code'] == code
    assert result['admission']['block_reason']['next_action']
    refused = api.post('/api/draft', json=pair)
    assert refused.status_code in (409, 422)
    assert refused.json()['admission']['block_reason']['code'] == code
    chat = api.post('/api/chat', json={**pair, 'message':'Подготовь заявку'})
    assert chat.status_code == 200, chat.text
    assert not chat.json()['draft']
    assert not sent


def test_screen_warning_is_separate_from_priority_and_payload_unchanged(integrated):
    api, _adapter, sent = integrated
    state, request = prepare(api, WARNING)
    assert state['admission']['warning_ack_required']
    assert state['admission']['suggested_urgency'] == 'medium'
    assert not state['admission']['can_submit']
    refused = api.post('/api/ticket', json=request)
    assert refused.status_code == 422, refused.text
    assert refused.json()['admission']['block_reason']['code'] == 'warning_ack_required'
    assert not sent
    accepted = api.post('/api/ticket', json={**request, 'warning_acknowledged':True})
    assert accepted.status_code == 200, accepted.text
    ticket = accepted.json()['ticket']
    assert ticket['ticket_id']
    assert len(sent) == 1
    assert set(sent[0]) == {'vehicle_id','session_id','anomaly_type','conclusion','urgency','operations','spare_parts','references'}
    assert api.post('/api/ticket', json={**request,'warning_acknowledged':True}).json()['ticket'] == ticket
    assert len(sent) == 1


def test_screen_rejects_free_form_work_and_f08_unknown_condition(integrated):
    api, _adapter, sent = integrated
    state, request = prepare(api, FUEL)
    refused = api.post('/api/ticket', json=request)
    assert refused.status_code == 422, refused.text
    assert refused.json()['admission']['block_reason']['code'] == 'applicability_required'
    wrong = {**request,'edits':{**request['edits'],'operations':['Проверить давление масла.']}}
    refused = api.put('/api/draft', json=wrong)
    assert refused.status_code == 422, refused.text
    assert refused.json()['admission']['block_reason']['code'] == 'operation_not_approved'
    assert not sent
    accepted = api.post('/api/ticket', json={**request,'applicability_confirmations':['initial_start_or_long_storage']})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['ticket']['ticket_id']
    assert len(sent) == 1

from test_mechanic import stand as local_stand


@pytest.mark.parametrize("fixture", ["integrated", "local_stand"])
def test_screen_rejects_unconfirmed_parts_and_delivers_empty_list(request, fixture):
    api, *_, sent = request.getfixturevalue(fixture)
    _, payload = prepare(api, CHAIN)
    payload["edits"]["spare_parts"] = ["Произвольная деталь X-999"]
    refused = api.post("/api/ticket", json=payload)
    assert refused.status_code == 422, refused.text
    assert refused.json()["admission"]["block_reason"]["code"] == "spare_part_not_approved"
    assert not sent

    payload["edits"]["spare_parts"] = []
    accepted = api.post("/api/ticket", json=payload)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["ticket"]["ticket_id"]
    assert len(sent) == 1 and sent[0]["spare_parts"] == []


def test_local_warning_requires_diagnosis_fingerprint(local_stand):
    api, sent = local_stand
    _state, request = prepare(api, WARNING)
    request.pop('diagnosis_revision')
    response = api.post('/api/ticket', json={**request, 'warning_acknowledged':True})
    assert response.status_code == 409, response.text
    assert response.json()['admission']['block_reason']['code'] == 'diagnosis_changed'
    assert not sent


def test_local_condition_save_changes_revision(local_stand):
    api, sent = local_stand
    state, request = prepare(api, FUEL)
    updated = api.put('/api/draft', json={**request, 'applicability_confirmations':['initial_start_or_long_storage']})
    assert updated.status_code == 200, updated.text
    assert updated.json()['draft_revision'] > state['draft_revision']
    response = api.post('/api/ticket', json={**request,'applicability_confirmations':['initial_start_or_long_storage']})
    assert response.status_code == 409, response.text
    assert not sent


def test_first_check_matches_reviewed_operation_in_fixture_mode(integrated):
    api, _adapter, sent = integrated
    state, _request = prepare(api, WARNING)
    answer = api.post('/api/chat', json={**WARNING, 'message':'Что проверить сначала?'})
    assert answer.status_code == 200, answer.text
    checks = [row for row in answer.json()['messages'][-1]['sections'] if row['kind']=='checks']
    assert checks and checks[0]['items'] == state['draft']['operations']
    assert {ref['pdf_page'] for ref in checks[0]['references']} == {173,174,175,176}
    assert not sent
