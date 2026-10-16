import tempfile
import unittest
from pathlib import Path

from engine.demo.dashboard import Dashboard, normalize_signals, validate_post
from engine.demo.bridge import execute


class DashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Dashboard(Path(temp.name)/'demo.db')
        self.a, self.b = 'a'*32, 'b'*32
        self.signal = {'text': 'A sample release', 'source': 'manual', 'metrics': {'likes': 2}}

    def test_ingestion_deduplicates_with_provenance(self):
        report = self.store.ingest(self.a, [self.signal, self.signal | {'text': ' A SAMPLE  release '}])
        self.assertEqual((report['added'], report['duplicates']), (1, 1))
        again = self.store.ingest(self.a, [self.signal])
        self.assertEqual((again['added'], again['duplicates']), (0, 1))
        state = self.store.snapshot(self.a)
        self.assertEqual(state['signals'][0]['source'], 'manual')
        self.assertEqual(len(state['ingestions']), 2)
        self.assertEqual(self.store.snapshot(self.b)['signals'], [])

    def test_bad_import_is_atomic(self):
        with self.assertRaises(ValueError):
            self.store.ingest(self.a, [self.signal, {'text': ''}])
        self.assertEqual(self.store.snapshot(self.a)['signals'], [])
        for value in ([self.signal | {'metrics': {'likes': -1}}], [self.signal | {'url': 'javascript:alert(1)'}], 'bad'):
            with self.assertRaises(ValueError):
                normalize_signals(value)

    def test_mock_round_reads_notes_and_records_episode_only_once(self):
        self.store.ingest(self.a, [self.signal])
        self.store.add_note(self.a, {'kind': 'procedure', 'text': 'Cite the source.'})
        task = self.store.submit(self.a, {'kind': 'run', 'mission': 'Help developers'})
        job = self.store.claim()
        self.assertEqual(job['id'], task['id'])
        result = execute(job['payload'])
        self.assertIn('Cite the source.', result['memory_input'])
        self.assertEqual(result['mode'], 'mock')
        self.assertGreater(len(result['actions']), 0)
        value = {'id': task['id'], 'lease': job['lease'], 'result': result}
        self.assertTrue(self.store.finish(value))
        self.assertFalse(self.store.finish(value))
        state = self.store.snapshot(self.a)
        self.assertEqual(len(state['runs']), 1)
        self.assertEqual([n['kind'] for n in state['notes']], ['procedure', 'episode'])
        self.assertIsNone(self.store.task(self.b, task['id']))

    def test_failed_source_does_not_invent_signals(self):
        self.store.submit(self.a, {'kind': 'ingest', 'source': 'google_news', 'mission': 'Test'})
        job = self.store.claim()
        self.store.finish({'id': job['id'], 'lease': job['lease'], 'failed': True, 'result': {'error': 'Source unavailable'}})
        self.assertEqual(self.store.task(self.a, job['id'])['status'], 'failed')
        self.assertEqual(self.store.snapshot(self.a)['signals'], [])

    def test_run_requires_input_and_source_is_allowlisted(self):
        for value in ({'kind': 'run', 'mission': 'Test'}, {'kind': 'ingest', 'mission': 'Test', 'source': 'shell'}):
            with self.assertRaises(ValueError):
                self.store.submit(self.a, value)

    def test_validation_separates_blockers_from_review(self):
        result = validate_post({'text': '[VERIFY: benchmark] 10x faster', 'mission': 'Developers', 'lane': 'reply'})
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(any(c['name'] == 'Parent post' and c['status'] == 'block' for c in result['checks']))
        result = validate_post({'text': '10x faster', 'mission': 'Developers', 'context': 'Claim without proof'})
        self.assertEqual(result['status'], 'needs_review')
        result = validate_post({'text': 'The CLI now shows cached steps.', 'mission': 'Developers', 'context': 'Release notes: shows cached steps.'})
        self.assertEqual(result['status'], 'checks_passed')
        self.assertIn('not factual verification', result['note'])


if __name__ == '__main__':
    unittest.main()
