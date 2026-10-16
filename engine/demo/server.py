"""HTTP UI and authenticated job queue. No model credentials are stored here."""

import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent


class Queue:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        self.worker_seen = 0.0
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created REAL, status TEXT, payload TEXT, result TEXT, lease TEXT, claimed REAL)")

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def submit(self, payload: dict) -> str:
        now = time.time()
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM jobs WHERE created < ?", (now - 86400,))
            if db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 20:
                raise ValueError("The demo queue is full. Please try again shortly.")
            job_id = secrets.token_hex(16)
            db.execute("INSERT INTO jobs VALUES (?,?,'queued',?,NULL,NULL,NULL)", (job_id, now, json.dumps(payload)))
            return job_id

    def claim(self):
        with self.lock, self.connect() as db:
            self.worker_seen = time.time()
            db.execute("DELETE FROM jobs WHERE created < ?", (time.time() - 86400,))
            db.execute("UPDATE jobs SET status='failed',result=? WHERE status='running' AND claimed < ?",
                       (json.dumps({"error": "The judge timed out. Please submit again."}), time.time() - 180))
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            lease = secrets.token_hex(16)
            db.execute("UPDATE jobs SET status='running',lease=?,claimed=? WHERE id=?", (lease, time.time(), row['id']))
            return {"id": row['id'], "lease": lease, "candidate": json.loads(row['payload'])}

    def finish(self, job_id: str, lease: str, result: dict, failed: bool):
        with self.lock, self.connect() as db:
            self.worker_seen = time.time()
            changed = db.execute("UPDATE jobs SET status=?,result=?,lease=NULL WHERE id=? AND lease=? AND status='running'",
                                 ('failed' if failed else 'done', json.dumps(result), job_id, lease)).rowcount
            return changed == 1

    def online(self):
        with self.connect() as db:
            running = db.execute("SELECT count(*) FROM jobs WHERE status='running' AND claimed > ?", (time.time() - 180,)).fetchone()[0]
        return time.time() - self.worker_seen < 30 or running > 0

    def get(self, job_id):
        with self.connect() as db:
            db.execute("DELETE FROM jobs WHERE created < ?", (time.time() - 86400,))
            db.execute("UPDATE jobs SET status='failed',result=? WHERE status='running' AND claimed < ?",
                       (json.dumps({"error": "The judge timed out. Please submit again."}), time.time() - 180))
            row = db.execute("SELECT id,status,result FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return {"id": row['id'], "status": row['status'], "result": json.loads(row['result']) if row['result'] else None}


def candidate_payload(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Submit a post and its context.")
    result = {}
    for key, limit in (("text", 4000), ("mission", 2000), ("context", 8000)):
        item = value.get(key, "")
        if not isinstance(item, str) or len(item) > limit:
            raise ValueError(f"{key} must be text with at most {limit} characters.")
        result[key] = item.strip()
    if not result['text'] or not result['mission']:
        raise ValueError("Add a post and describe its audience or goal.")
    result['lane'] = value.get('lane', 'post')
    if result['lane'] not in ('post', 'reply', 'quote'):
        raise ValueError("Choose post, reply, or quote.")
    return result


def make_handler(queue: Queue, access_token: str, worker_token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Request paths or headers can contain user-provided data.
            return

        def send(self, status, value, content_type='application/json'):
            body = value.encode() if isinstance(value, str) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(body)

        def authorized(self, token):
            return hmac.compare_digest(self.headers.get('Authorization', ''), f'Bearer {token}')

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path == '/':
                return self.send(200, (ROOT / 'index.html').read_text(encoding='utf-8'), 'text/html; charset=utf-8')
            if path == '/healthz':
                return self.send(200, {'ok': True})
            if not self.authorized(access_token):
                return self.send(401, {'error': 'Enter the team access code.'})
            if path == '/api/status':
                return self.send(200, {'worker_online': queue.online()})
            if path.startswith('/api/jobs/'):
                result = queue.get(path.rsplit('/', 1)[1])
                return self.send(200 if result else 404, result or {'error': 'Evaluation not found or expired.'})
            return self.send(404, {'error': 'Not found'})

        def do_POST(self):
            internal = self.path.startswith('/internal/')
            if not self.authorized(worker_token if internal else access_token):
                return self.send(401, {'error': 'Invalid access code.'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= (200000 if internal else 20000):
                    return self.send(413, {'error': 'Request is too large or empty.'})
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ValueError('Expected an object.')
                if self.path == '/api/jobs':
                    job_id = queue.submit(candidate_payload(value))
                    return self.send(202, {'id': job_id, 'status': 'queued'})
                if self.path == '/internal/claim':
                    return self.send(200, {'job': queue.claim()})
                if self.path == '/internal/finish':
                    if not isinstance(value.get('result'), dict):
                        raise ValueError('Expected a result object.')
                    ok = queue.finish(value['id'], value['lease'], value['result'], bool(value.get('failed')))
                    return self.send(200 if ok else 409, {'accepted': ok})
                return self.send(404, {'error': 'Not found'})
            except (ValueError, KeyError, TypeError) as exc:
                return self.send(400, {'error': str(exc)})
    return Handler


def main():
    access_token, worker_token = os.environ['DEMO_ACCESS_TOKEN'], os.environ['DEMO_WORKER_TOKEN']
    if len(access_token) < 24 or len(worker_token) < 24 or access_token == worker_token:
        raise ValueError('Use distinct demo and worker tokens of at least 24 characters.')
    queue = Queue(Path(os.getenv('DEMO_DB_PATH', '/tmp/post-eval-demo.sqlite')))
    server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '8080'))), make_handler(queue, access_token, worker_token))
    server.serve_forever()


if __name__ == '__main__':
    main()
