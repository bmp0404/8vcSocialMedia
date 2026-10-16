import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

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
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.queue, 'worker-token'))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        with urlopen(base + '/healthz') as res:
            self.assertTrue(json.load(res)['ok'])
        with urlopen(base + '/api/status') as res:
            self.assertIn('worker_online', json.load(res))
        for path, token in (('/internal/claim', ''), ('/internal/finish', 'wrong')):
            req = Request(base + path, b'{}', {'Authorization': 'Bearer ' + token})
            with self.assertRaises(HTTPError) as err:
                urlopen(req)
            self.assertEqual(err.exception.code, 401)
        req = Request(base + '/api/jobs', json.dumps(self.payload).encode())
        with urlopen(req) as res:
            self.assertEqual(res.status, 202)
            job_id = json.load(res)['id']
        with urlopen(Request(base + '/api/jobs/' + job_id)) as res:
            self.assertEqual(json.load(res)['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
