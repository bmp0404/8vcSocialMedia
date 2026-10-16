import io
import json
import tempfile
import unittest
from pathlib import Path

from engine.demo.server import Queue, candidate_payload, make_handler


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.queue = Queue(Path(self.temp.name) / 'demo.db')
        self.payload = {'text': 'Our release', 'mission': 'Developers', 'context': 'Release notes', 'lane': 'post'}

    def test_job_requires_matching_lease(self):
        job_id = self.queue.submit(self.payload)
        job = self.queue.claim()
        self.assertEqual(job['candidate'], self.payload)
        self.assertEqual(self.queue.get(job_id)['status'], 'running')
        self.assertFalse(self.queue.finish(job_id, 'wrong', {'score_100': 99}, False))
        self.assertTrue(self.queue.finish(job_id, job['lease'], {'score_100': 70}, False))
        self.assertFalse(self.queue.finish(job_id, job['lease'], {'score_100': 99}, False))
        self.assertEqual(self.queue.get(job_id)['result']['score_100'], 70)

    def test_failed_judge_is_not_a_score(self):
        job_id = self.queue.submit(self.payload)
        job = self.queue.claim()
        self.queue.finish(job_id, job['lease'], {'error': 'Unavailable'}, True)
        self.assertEqual(self.queue.get(job_id)['status'], 'failed')
        self.assertNotIn('score_100', self.queue.get(job_id)['result'])

    def test_input_validation(self):
        for bad in ({}, {'text': 'x', 'mission': []}, self.payload | {'lane': 'delete'}, self.payload | {'text': 'x'*4001}):
            with self.assertRaises(ValueError):
                candidate_payload(bad)
        self.assertEqual(candidate_payload(self.payload), self.payload)

    def test_public_jobs_and_authenticated_worker(self):
        handler = make_handler(self.queue, 'worker-token')

        def request(path, payload=None, token=None):
            body = json.dumps(payload).encode() if payload is not None else b''
            method = 'POST' if payload is not None else 'GET'
            headers = f"{method} {path} HTTP/1.0\r\nContent-Length: {len(body)}\r\n"
            if token:
                headers += f"Authorization: Bearer {token}\r\n"

            class Connection:
                def __init__(self):
                    self.response = bytearray()

                def makefile(self, *args):
                    return io.BytesIO(headers.encode() + b'\r\n' + body)

                def sendall(self, data):
                    self.response.extend(data)

            connection = Connection()
            handler(connection, ('127.0.0.1', 1234), None)
            head, content = bytes(connection.response).split(b'\r\n\r\n', 1)
            return int(head.split()[1]), json.loads(content)

        self.assertEqual(request('/healthz'), (200, {'ok': True}))
        self.assertIn('worker_online', request('/api/status')[1])
        for path, token in (('/internal/claim', None), ('/internal/finish', 'wrong')):
            self.assertEqual(request(path, {}, token)[0], 401)
        status, job = request('/api/jobs', self.payload)
        self.assertEqual(status, 202)
        self.assertEqual(request('/api/jobs/' + job['id'])[1]['status'], 'queued')
        self.assertEqual(request('/internal/claim', {}, 'worker-token')[1]['job']['id'], job['id'])


if __name__ == '__main__':
    unittest.main()
