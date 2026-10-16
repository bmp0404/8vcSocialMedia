"""Run allowlisted ingestion and mock engine tasks for the Fly dashboard."""
import json
import os
import tempfile
import time
from dataclasses import asdict, fields
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from harness.llm import MockLLM
from harness.models import Signal
from harness.sources import REGISTRY
from engine.runner import Components, Options, run_loop
from engine.runtime import runtime
def request(base, token, path, payload):
    req = Request(base.rstrip('/') + path, json.dumps(payload).encode(),
                  {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    with urlopen(req, timeout=20) as response:
        return json.load(response)



def execute(payload):
    if payload['kind'] == 'ingest':
        source = payload['source']
        if source not in ('sample', 'google_news'):
            raise ValueError('Unsupported source')
        signals = REGISTRY[source]().fetch(payload['mission'])
        return {'source': source, 'signals': [asdict(s) for s in signals[:100]]}
    if payload['kind'] != 'run':
        raise ValueError('Unsupported task')
    allowed = {f.name for f in fields(Signal)}
    signals = [Signal(**{k: v for k, v in row.items() if k in allowed}) for row in payload['signals']]
    notes = '\n'.join(f"[{n['kind']}; {n['status']}] {n['text']}" for n in payload['notes'])
    with tempfile.TemporaryDirectory(prefix='dashboard-round-') as folder:
        with runtime('mock', ['sample'], Path(folder)) as out:
            components = Components(observe=lambda names, mission: signals, read_memory=lambda: notes or '(no demo memory yet)')
            journal = run_loop(MockLLM(), payload['mission'], ['sample'], Options(rounds=1), components, out)[0]
            artifacts = out/'engine'/journal['round_id']
            actions = json.loads((artifacts/'actions.json').read_text(encoding='utf-8'))['actions']
            narratives = json.loads((artifacts/'narratives.json').read_text(encoding='utf-8'))['narratives']
            journal.pop('recommendations_path', None)
            return {'mission': payload['mission'], 'journal': journal, 'actions': actions, 'narratives': narratives, 'memory_input': notes, 'mode': 'mock'}


def main():
    load_dotenv()
    base, token = os.environ['DEMO_URL'], os.environ['DEMO_WORKER_TOKEN']
    while True:
        try:
            job = request(base, token, '/internal/dashboard/claim', {})['job']
        except (URLError, OSError, TimeoutError):
            time.sleep(5)
            continue
        if not job:
            time.sleep(2)
            continue
        failed = False
        try:
            result = execute(job['payload'])
        except Exception as exc:
            failed = True
            result = {'error': 'The source or engine failed. Check the dashboard worker log.', 'error_type': type(exc).__name__}
            print(f"[dashboard] {job['id']}: {type(exc).__name__}: {exc}", flush=True)
        for _ in range(3):
            try:
                request(base, token, '/internal/dashboard/finish', {'id': job['id'], 'lease': job['lease'], 'failed': failed, 'result': result})
                break
            except (URLError, OSError, TimeoutError):
                time.sleep(2)
        print(f"[dashboard] {job['id']}: {'failed' if failed else 'completed'}", flush=True)


if __name__ == '__main__':
    main()
