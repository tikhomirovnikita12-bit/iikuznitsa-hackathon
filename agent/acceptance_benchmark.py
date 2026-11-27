"""Two real Ollama runs, original mock APIs and a confirmed reviewed F14 ticket."""
import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from urllib.error import URLError
from urllib.request import urlopen


def now():
    return datetime.now(timezone.utc).isoformat()


def read_trace(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def write_report(output, report):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with output.with_suffix('.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        columns = ['run', 'step', 'purpose', 'model', 'input_tokens', 'output_tokens', 'seconds', 'result']
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for run in report['runs']:
            for index, step in enumerate(run['steps'], 1):
                calls = step['calls'] or [{'model': '', 'input_tokens': 0, 'output_tokens': 0, 'seconds': step['seconds']}]
                for call_index, call in enumerate(calls, 1):
                    writer.writerow({'run': run['name'], 'step': f'{index}.{call_index}',
                        'purpose': step['purpose'], 'model': call.get('model'),
                        'input_tokens': call.get('input_tokens'), 'output_tokens': call.get('output_tokens'),
                        'seconds': call.get('seconds'), 'result': step.get('ticket_id') or ('draft' if step.get('draft') else 'answer')})


def run(output):
    root = Path(__file__).resolve().parents[1]
    model = os.getenv('AGENT_MODEL', 'gemma2:2b')
    host = os.getenv('OLLAMA_HOST_URL', 'http://127.0.0.1:11434').rstrip('/')
    report = {'started_at': now(), 'configured_model': model, 'ollama_url': host,
              'executed': False, 'runs': [], 'accounting': 'Sum calls once from each step; search and indexing have no LLM tokens.'}
    try:
        with urlopen(host + '/api/tags', timeout=2) as response:
            names = [item['name'] for item in json.load(response).get('models', [])]
        if model not in names:
            raise RuntimeError(f'Configured model {model} is not installed: {names}')
    except (OSError, URLError, RuntimeError) as error:
        report.update(ended_at=now(), status='unavailable', reason=str(error))
        write_report(output, report)
        return 2

    import httpx
    from fastapi.testclient import TestClient
    from backend.admission import diagnosis_revision
    from backend.mechanic import Mechanic
    from backend.models import Confirm, Pair
    from backend.store import Store
    from backend.workflow import Catalog, Workflow
    from mechanic.case_agent import CaseAgent
    from mechanic.contracts import Diagnosis

    sys.path.insert(0, str(root / 'artifacts/customer'))
    from mock_ml.app import app as ml_app
    from mock_toir.app import app as toir_app
    catalog = Catalog(root / 'data/vehicles.json')
    vehicles = json.loads((root / 'data/vehicles.json').read_text(encoding='utf-8'))['vehicles']
    fleet = {row['vehicle_id']: row for row in vehicles}
    with TemporaryDirectory(prefix='mechanic-acceptance-') as scratch, TestClient(ml_app) as ml_api, TestClient(toir_app) as toir_api:
        def relay(api, request):
            return api.request(request.method, request.url.path, content=request.content,
                               headers={'Content-Type': 'application/json'})

        with httpx.Client(base_url='http://mock-ml', transport=httpx.MockTransport(lambda request: relay(ml_api, request))) as ml, \
                httpx.Client(base_url='http://mock-toir', transport=httpx.MockTransport(lambda request: relay(toir_api, request))) as toir:
            mechanic = Mechanic(root / 'artifacts/manual-corpus.json', root / 'artifacts/manuals')
            workflow = Workflow(catalog, Store(Path(scratch) / 'workflow.sqlite3'), mechanic, ml, toir, 60)
            log = Path(scratch) / 'turns.jsonl'
            agent = CaseAgent(root / 'artifacts/manual-corpus.json', log, mechanic=mechanic)
            scenarios = [
                ('documentation', 'E549YT198', 'S-0011', [
                    'На что обратить внимание при падении давления масла?',
                    'Какое давление масла должно быть на прогретом двигателе?',
                    'Можно ли своим ходом перегнать машину?']),
                ('ticket', 'Y815BM178', 'S-0152', [
                    'Что проверить сначала?',
                    'Стук спереди на средних оборотах: чем различаются цепь и гидронатяжитель?',
                    'Как это проверить?']),
            ]
            for name, plate, session, questions in scenarios:
                row = {'name': name, 'vehicle_id': plate, 'session_id': session, 'started_at': now(), 'steps': []}
                start = perf_counter()
                pair = Pair(vehicle_id=plate, session_id=session)
                diagnosis_started_at = now()
                diagnosis_start = perf_counter()
                diagnosis = workflow.diagnose(pair).diagnosis
                row['steps'].append({'purpose': 'mock_ml_diagnosis', 'started_at': diagnosis_started_at,
                    'ended_at': now(), 'seconds': perf_counter() - diagnosis_start,
                    'calls': [], 'result': diagnosis.model_dump()})
                context = Diagnosis.model_validate(diagnosis.model_dump())
                history = []
                for question in questions + (['Подготовь заявку'] if name == 'ticket' else []):
                    started_at = now()
                    step_start = perf_counter()
                    count = len(read_trace(log))
                    reply, draft, entry = agent.chat(fleet[plate], context, question, history)
                    traces = read_trace(log)[count:]
                    calls = [call for trace in traces for call in trace['calls']]
                    row['steps'].append({'purpose': 'prepare_ticket' if draft else 'answer', 'question': question,
                        'started_at': started_at, 'ended_at': now(), 'seconds': perf_counter() - step_start,
                        'calls': calls, 'tools': [tool for trace in traces for tool in trace['tools']],
                        'result': reply.model_dump(), 'draft': bool(draft)})
                    if entry:
                        history = [*history[-5:], entry]
                    if name == 'ticket' and question == 'Подготовь заявку':
                        if not draft:
                            raise RuntimeError('Reviewed F14 draft was not prepared')
                        saved = workflow.create_draft(pair)
                        confirm_started_at = now()
                        confirm_start = perf_counter()
                        receipt = workflow.confirm_draft(saved.draft_id, Confirm(
                            revision=saved.revision, urgency=draft.urgency, confirmed=True,
                            diagnosis_revision=diagnosis_revision(diagnosis),
                            warning_acknowledged=diagnosis.signal_quality == 'warning',
                        ))
                        if not receipt.ticket_id:
                            raise RuntimeError('Confirmation did not return a ticket ID')
                        row['steps'].append({'purpose': 'confirmed_mock_toir', 'calls': [],
                            'started_at': confirm_started_at, 'ended_at': now(),
                            'seconds': perf_counter() - confirm_start, 'ticket_id': receipt.ticket_id})
                calls = [call for step in row['steps'] for call in step['calls']]
                row.update(ended_at=now(), seconds=perf_counter() - start, llm_calls=len(calls))
                for key in ('input_tokens', 'output_tokens'):
                    row[key] = sum(call[key] for call in calls) if all(call.get(key) is not None for call in calls) else None
                report['runs'].append(row)
    report.update(executed=True, status='completed', ended_at=now())
    write_report(output, report)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('docs/agent/acceptance-runs.json'))
    args = parser.parse_args()
    return run(args.output)


if __name__ == '__main__':
    raise SystemExit(main())
