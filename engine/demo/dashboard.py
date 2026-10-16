"""Per-browser demo state, deterministic checks, and isolated engine task queue."""
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent


def normalize_signals(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError('Provide between 1 and 100 signal objects.')
    signals, seen, duplicates = [], set(), 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('text'), str) or not row['text'].strip():
            raise ValueError('Every signal needs nonempty text.')
        if len(row['text']) > 4000:
            raise ValueError('Signal text must be at most 4,000 characters.')
        signal = {'text': row['text'].strip(), 'source': row.get('source') or 'manual', 'metrics': row.get('metrics', {})}
        for key in ('url', 'author', 'created_at', 'kind'):
            signal[key] = row.get(key) or ''
        if any(not isinstance(signal[k], str) or len(signal[k]) > 2000 for k in ('source', 'url', 'author', 'created_at', 'kind')):
            raise ValueError('Source, URL, author, date, and kind must be short text fields.')
        if signal['url'] and not signal['url'].startswith(('https://', 'http://')):
            raise ValueError('Signal URLs must use http or https.')
        if not isinstance(signal['metrics'], dict) or len(signal['metrics']) > 30 or any(not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 1e15 for v in signal['metrics'].values()):
            raise ValueError('Metrics must be nonnegative numeric counts.')
        key = ' '.join(signal['text'].lower().split())
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        signal['id'] = hashlib.sha256(key.encode()).hexdigest()[:16]
        signals.append(signal)
    return {'signals': signals, 'received': len(rows), 'duplicates': duplicates}


def validate_post(value):
    text, context, mission = (value.get(k, '') for k in ('text', 'context', 'mission'))
    if any(not isinstance(v, str) for v in (text, context, mission)) or max(map(len, (text, context, mission))) > 8000:
        raise ValueError('Post, context, and mission must be text of at most 8,000 characters.')
    lane = value.get('lane', 'post')
    if lane not in ('post', 'reply', 'quote'):
        raise ValueError('Choose post, reply, or quote.')
    checks = []
    def add(name, status, detail):
        checks.append({'name': name, 'status': status, 'detail': detail})
    add('Draft text', 'pass' if text.strip() else 'block', 'Draft is present.' if text.strip() else 'Write a post first.')
    add('Audience & goal', 'pass' if mission.strip() else 'block', 'Audience provided.' if mission.strip() else 'Define who the post is for.')
    add('Length', 'review' if len(text) > 280 else 'pass', f'{len(text)} characters. This is a plain character count, not X weighted length; long-post eligibility is not checked.')
    add('Verification placeholders', 'block' if re.search(r'\[VERIFY\b', text, re.I) else 'pass', 'Resolve [VERIFY: ...] before approval.' if re.search(r'\[VERIFY\b', text, re.I) else 'No unresolved [VERIFY] markers.')
    add('Supporting context', 'pass' if context.strip() else 'review', 'Context supplied; the model still needs to assess support.' if context.strip() else 'No evidence supplied. Factual claims need review.')
    if lane != 'post':
        add('Parent post', 'pass' if context.strip() else 'block', 'Confirm the context includes the parent post.' if context.strip() else 'Replies and quotes need parent-post context.')
    if re.search(r'\d+(?:\.\d+)?\s*(?:%|x\b|times\b)', text, re.I):
        add('Quantified claims', 'review', 'Check each percentage or multiplier against the source. Presence of context does not prove it.')
    status = 'blocked' if any(c['status'] == 'block' for c in checks) else 'needs_review' if any(c['status'] == 'review' for c in checks) else 'checks_passed'
    return {'status': status, 'checks': checks, 'note': 'Structural checks only. This is not factual verification, a safety review, or publishing approval.'}


class Dashboard:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS dashboard_state (workspace TEXT PRIMARY KEY, updated REAL, data TEXT);
            CREATE TABLE IF NOT EXISTS dashboard_tasks (id TEXT PRIMARY KEY, workspace TEXT, created REAL, status TEXT, payload TEXT, result TEXT, lease TEXT, claimed REAL);''')
        self.bridge_seen = 0

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def load(self, db, workspace):
        row = db.execute('SELECT data FROM dashboard_state WHERE workspace=?', (workspace,)).fetchone()
        return json.loads(row['data']) if row else {'signals': [], 'notes': [], 'runs': [], 'ingestions': []}

    def save(self, db, workspace, state):
        db.execute('INSERT OR REPLACE INTO dashboard_state VALUES (?,?,?)', (workspace, time.time(), json.dumps(state)))

    def snapshot(self, workspace):
        with self.connect() as db:
            db.execute('DELETE FROM dashboard_state WHERE updated < ?', (time.time()-86400,))
            db.execute('DELETE FROM dashboard_tasks WHERE created < ?', (time.time()-86400,))
            state = self.load(db, workspace)
            busy = db.execute("SELECT count(*) FROM dashboard_tasks WHERE status='running' AND claimed>?", (time.time()-180,)).fetchone()[0]
        return state | {'bridge_online': time.time()-self.bridge_seen < 30 or bool(busy), 'sources': ['sample', 'google_news'], 'scope': 'Isolated demo workspace; not production memory', 'sample': json.loads((ROOT/'sample.json').read_text(encoding='utf-8'))}

    def ingest(self, workspace, rows, origin='manual'):
        batch = normalize_signals(rows)
        with self.lock, self.connect() as db:
            state = self.load(db, workspace)
            known = {s['id'] for s in state['signals']}
            added = [s for s in batch['signals'] if s['id'] not in known]
            state['signals'] = (state['signals'] + added)[-200:]
            report = {'at': time.time(), 'source': origin, 'received': batch['received'], 'added': len(added), 'duplicates': batch['received']-len(added)}
            state['ingestions'] = (state['ingestions'] + [report])[-20:]
            self.save(db, workspace, state)
        return report

    def add_note(self, workspace, value):
        text, kind = value.get('text', ''), value.get('kind', 'procedure')
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000 or kind not in ('procedure', 'observation'):
            raise ValueError('Add a procedure or observation of 1–4,000 characters.')
        note = {'id': secrets.token_hex(8), 'kind': kind, 'text': text.strip(), 'at': time.time(), 'origin': 'human', 'status': 'unverified'}
        with self.lock, self.connect() as db:
            state = self.load(db, workspace)
            state['notes'] = (state['notes'] + [note])[-100:]
            self.save(db, workspace, state)
        return note

    def submit(self, workspace, value):
        kind = value.get('kind')
        if kind not in ('ingest', 'run'):
            raise ValueError('Unknown task.')
        mission = value.get('mission', '')
        if not isinstance(mission, str) or not 1 <= len(mission.strip()) <= 2000:
            raise ValueError('Provide an audience and goal of at most 2,000 characters.')
        with self.lock, self.connect() as db:
            if db.execute("SELECT count(*) FROM dashboard_tasks WHERE status IN ('queued','running')").fetchone()[0] >= 10:
                raise ValueError('Demo queue is full. Try again shortly.')
            state = self.load(db, workspace)
            payload = {'kind': kind, 'mission': mission}
            if kind == 'ingest':
                if value.get('source') not in ('sample', 'google_news'):
                    raise ValueError('Choose sample or google_news.')
                payload['source'] = value['source']
            else:
                if not state['signals']:
                    raise ValueError('Ingest signals before running a round.')
                payload.update(signals=state['signals'][-50:], notes=state['notes'][-20:])
            task_id = secrets.token_hex(16)
            db.execute("INSERT INTO dashboard_tasks VALUES (?,?,?,'queued',?,NULL,NULL,NULL)", (task_id, workspace, time.time(), json.dumps(payload)))
        return {'id': task_id, 'status': 'queued'}

    def expire(self, db):
        db.execute("UPDATE dashboard_tasks SET status='failed',result=? WHERE status='running' AND claimed<?", (json.dumps({'error': 'The dashboard worker timed out. Try again.'}), time.time()-180))

    def claim(self):
        with self.lock, self.connect() as db:
            self.bridge_seen = time.time()
            self.expire(db)
            row = db.execute("SELECT * FROM dashboard_tasks WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            lease = secrets.token_hex(16)
            db.execute("UPDATE dashboard_tasks SET status='running',lease=?,claimed=? WHERE id=?", (lease, time.time(), row['id']))
            return {'id': row['id'], 'lease': lease, 'payload': json.loads(row['payload'])}

    def finish(self, value):
        result = value.get('result')
        if not isinstance(result, dict):
            raise ValueError('Expected a result object.')
        with self.lock, self.connect() as db:
            self.expire(db)
            row = db.execute("SELECT * FROM dashboard_tasks WHERE id=? AND lease=? AND status='running'", (value.get('id'), value.get('lease'))).fetchone()
            if not row:
                return False
            failed = bool(value.get('failed'))
            if not failed:
                state = self.load(db, row['workspace'])
                if json.loads(row['payload'])['kind'] == 'ingest':
                    batch = normalize_signals(result['signals']) if result['signals'] else {'signals': [], 'received': 0}
                    known = {s['id'] for s in state['signals']}
                    added = [s for s in batch['signals'] if s['id'] not in known]
                    state['signals'] = (state['signals']+added)[-200:]
                    state['ingestions'] = (state['ingestions']+[{'at': time.time(), 'source': result['source'], 'received': batch['received'], 'added': len(added), 'duplicates': batch['received']-len(added)}])[-20:]
                else:
                    state['runs'] = (state['runs']+[result])[-10:]
                    state['notes'] = (state['notes']+[{'id': row['id'], 'kind': 'episode', 'text': f"Mock round: {result['journal']['status']}. {len(result['actions'])} drafts; no published outcomes.", 'at': time.time(), 'origin': 'mock engine', 'status': 'recorded', 'run_id': result['journal']['round_id']}])[-100:]
                self.save(db, row['workspace'], state)
            db.execute('UPDATE dashboard_tasks SET status=?,result=?,lease=NULL WHERE id=?', ('failed' if failed else 'done', json.dumps(result), row['id']))
            return True

    def task(self, workspace, task_id):
        with self.connect() as db:
            self.expire(db)
            row = db.execute('SELECT id,status,result FROM dashboard_tasks WHERE id=? AND workspace=?', (task_id, workspace)).fetchone()
        return dict(row) | {'result': json.loads(row['result']) if row['result'] else None} if row else None
