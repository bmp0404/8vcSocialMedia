import unittest

from baml_sdk.ai import content, events
from harness.baml_reply import ReplyRecorder, fractional_values, json_closes, raw_array_length


class RawArrayLengthTests(unittest.TestCase):
    def test_counts_elements(self):
        self.assertEqual(raw_array_length('{"items": [{"a": 1}, {"a": [2, 3]}, {"a": "x, y"}]}', "items"), 3)
        self.assertEqual(raw_array_length('{"items": []}', "items"), 0)
        self.assertEqual(raw_array_length('{"items": ["a", "b]", 3]}', "items"), 3)

    def test_counts_malformed_and_cut_off_elements(self):
        self.assertEqual(raw_array_length('{"items": [{"a": 1}, {"a": }, {"b": 2,]}', "items"), 3)
        self.assertEqual(raw_array_length('{"items": [{"a": 1}, {"a": "cut', "items"), 2)
        self.assertEqual(raw_array_length('{"items": [{"a": 1},]}', "items"), 1)

    def test_reads_the_first_array_with_that_key(self):
        text = 'Sure:\n```json\n{"items": [1, 2]}\n```\n{"items": [1, 2, 3]}'
        self.assertEqual(raw_array_length(text, "items"), 2)
        self.assertEqual(raw_array_length('{"outer": [{"items": [1]}]}', "items"), 1)

    def test_absent_key(self):
        self.assertIsNone(raw_array_length('{"other": [1]}', "items"))
        self.assertIsNone(raw_array_length('{"items": 3}', "items"))
        self.assertIsNone(raw_array_length("no json here", "items"))

    def test_escaped_quotes(self):
        self.assertEqual(raw_array_length(r'{"items": ["say \"hi\", then", "b"]}', "items"), 2)


class JsonClosesTests(unittest.TestCase):
    def test_complete_values(self):
        self.assertTrue(json_closes('{"a": [1, {"b": "}"}]}'))
        self.assertTrue(json_closes('prose {"a": 1} more prose'))
        self.assertTrue(json_closes('{a: 1, "b": [1,],}'))  # SAP's repairable noise still closes
        self.assertTrue(json_closes('[1, 2]'))

    def test_cut_off_values(self):
        self.assertFalse(json_closes('{"a": [1, 2'))
        self.assertFalse(json_closes('{"a": "unterminated'))
        self.assertFalse(json_closes('{"a": "escaped quote \\"}'))
        self.assertFalse(json_closes("no json"))
        self.assertFalse(json_closes(""))


class FractionalValuesTests(unittest.TestCase):
    def test_finds_only_non_integers(self):
        text = '{"score": 4.6, "x": {"score": 3}, "score": 2.0, "score": -0.5, "other": 1.5}'
        self.assertEqual(fractional_values(text, "score"), [4.6, -0.5])
        self.assertEqual(fractional_values('{"score": 3}', "score"), [])


class ReplyRecorderTests(unittest.TestCase):
    def test_keeps_last_reply_and_sums_usage(self):
        seen = []
        recorder = ReplyRecorder(forward=seen.append)
        first = events.AssistantMessage(content=[content.Text(text="not json")], client_id="c")
        second = events.AssistantMessage(content=[content.Text(text='{"a": '), content.Text(text="1}")], client_id="c")
        for event in (first, events.Usage(input_tokens=10, output_tokens=5), second,
                      events.Usage(input_tokens=12, output_tokens=7)):
            recorder(event)
        self.assertEqual(recorder.text, '{"a": 1}')
        self.assertEqual(recorder.replies, 2)
        self.assertEqual((recorder.input_tokens, recorder.output_tokens), (22, 12))
        self.assertEqual(len(seen), 4)

    def test_no_reply(self):
        self.assertIsNone(ReplyRecorder().text)


if __name__ == "__main__":
    unittest.main()
