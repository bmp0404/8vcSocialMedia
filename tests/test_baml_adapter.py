import json
import os
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

from baml_sdk.ai import content, events
from harness import baml_adapter as ba
from harness.config import ROOT
from harness.llm import LLM, MAX_TOKENS, AnthropicLLM, MockLLM, OpenAICompatLLM

GATEWAY = {"ANTHROPIC_BASE_URL": "https://gateway.example", "ANTHROPIC_AUTH_TOKEN": "fixture-token"}


def anthropic_llm(model="fixture-model"):
    with patch.dict("os.environ", GATEWAY):
        return AnthropicLLM(model)


class Forwarding(LLM):
    """A wrapper that forwards unknown attributes, like the tracing proxy on sam/integration."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)


class ClientSettingsTests(unittest.TestCase):
    def test_backends(self):
        self.assertEqual(ba.client_settings(anthropic_llm()), ("anthropic", "fixture-model"))
        self.assertEqual(ba.client_settings(OpenAICompatLLM("gpt-x", "k", None)), ("openai", "gpt-x"))
        xai = OpenAICompatLLM("grok-x", "k", "https://api.x.ai/v1", provider="xai")
        self.assertEqual(ba.client_settings(xai), ("xai", "grok-x"))

    def test_forwarding_wrapper_keeps_settings(self):
        self.assertEqual(ba.client_settings(Forwarding(anthropic_llm())), ("anthropic", "fixture-model"))

    def test_unknown_provider_and_missing_model(self):
        with self.assertRaisesRegex(ba.BamlProviderUnsupported, "provider 'mock'"):
            ba.client_settings(MockLLM())
        with self.assertRaisesRegex(ba.BamlProviderUnsupported, "provider ''"):
            ba.client_settings(LLM())
        no_model = anthropic_llm()
        no_model.model = ""
        with self.assertRaisesRegex(ba.BamlProviderUnsupported, "no model"):
            ba.client_settings(no_model)


class MockRunTests(unittest.TestCase):
    def test_returns_mock_without_building_a_spec(self):
        with patch.object(ba, "spec") as spec:
            result = ba.call(MockLLM(), "OrientSignals", "m", "[]", 3, mock=lambda: {"narratives": []})
        spec.assert_not_called()
        self.assertEqual(result.value, {"narratives": []})
        self.assertIsNone(result.raw)

    def test_mock_run_never_imports_baml(self):
        script = (
            "import sys\n"
            "from harness import baml_adapter, orient\n"
            "from harness.llm import MockLLM\n"
            "from harness.models import Signal\n"
            "out = orient.orient(MockLLM(), 'm', [Signal(source='s', text='t')], top=1)\n"
            "assert out and out[0].id == 'sample-0', out\n"
            "print(sorted(m for m in sys.modules if m.startswith('baml')))\n"
        )
        env = {**os.environ, "HARNESS_ORIENT": "baml"}
        proc = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        self.assertIn("via baml", proc.stdout)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "[]")


class SdkTests(unittest.TestCase):
    def test_missing_package(self):
        with patch.object(ba, "SDK_MODULE", "baml_sdk_not_generated"):
            with self.assertRaisesRegex(ba.BamlSdkMissing, "isn't generated; run BAML_VERSION=0.22.2 baml generate"):
                ba.spec(anthropic_llm(), "OrientSignals")

    def test_missing_function(self):
        with self.assertRaisesRegex(ba.BamlSdkMissing, "has no NoSuchFunction; regenerate"):
            ba.spec(anthropic_llm(), "NoSuchFunction")


class FakeSpec:
    """Stands in for a BAML FunctionSpec: emits a reply and usage the way .call does."""

    def __init__(self, reply, value):
        self.reply, self.value = reply, value

    def call(self, on_event):
        on_event(events.AssistantMessage(content=[content.Text(text=self.reply)], client_id="c"))
        on_event(events.Usage(input_tokens=120, output_tokens=30))
        on_event(events.FinalProduced(value_json=json.dumps(self.value)))
        return self.value


class CallTests(unittest.TestCase):
    def test_passes_settings_and_forwards_events(self):
        made = []

        def factory(*args, **kwargs):
            made.append((args, kwargs))
            return FakeSpec('{"narratives": []}', "parsed")

        llm = anthropic_llm()
        metered, observed = [], []
        llm.on_baml_event = metered.append
        with patch.object(ba, "load_sdk", return_value=types.SimpleNamespace(OrientSignals_spec=factory)):
            result = ba.call(llm, "OrientSignals", "mission", "[]", 4, mock=lambda: "unused", on_event=observed.append)
        self.assertEqual(made, [(("mission", "[]", 4), {"provider": "anthropic", "model": "fixture-model",
                                                         "max_tokens": MAX_TOKENS})])
        self.assertEqual(result.value, "parsed")
        self.assertEqual(result.raw, '{"narratives": []}')
        self.assertEqual((result.input_tokens, result.output_tokens), (120, 30))
        self.assertEqual(len(metered), 3)
        self.assertEqual(metered, observed)


class RequestTests(unittest.TestCase):
    """The real OrientSignals request, rendered offline, matches what harness/llm.py would send."""

    def request(self, llm, env):
        with patch.dict("os.environ", env, clear=True):
            return ba.spec(llm, "OrientSignals", "MISSION TEXT", '[{"id": 0, "text": "SIGNAL TEXT"}]', 6).build_request()

    def test_anthropic_gateway(self):
        req = self.request(anthropic_llm("claude-fixture"), GATEWAY)
        self.assertEqual(req.url, "https://gateway.example/v1/messages")
        self.assertEqual(req.headers["authorization"], "Bearer fixture-token")
        body = json.loads(req.body)
        self.assertEqual((body["model"], body["max_tokens"]), ("claude-fixture", MAX_TOKENS))
        system = json.dumps(body["system"])
        self.assertIn("MISSION TEXT", system)
        self.assertNotIn("SIGNAL TEXT", system)
        self.assertIn("SIGNAL TEXT", json.dumps(body["messages"]))

    def test_anthropic_api_key(self):
        req = self.request(anthropic_llm(), {"ANTHROPIC_API_KEY": "fixture-key"})
        self.assertEqual(req.url, "https://api.anthropic.com/v1/messages")
        # The preview leaves out the x-api-key header; the bearer header is only for the gateway.
        self.assertNotIn("authorization", req.headers)

    def test_xai_and_openai_gateway(self):
        xai = OpenAICompatLLM("grok-fixture", "k", "https://api.x.ai/v1", provider="xai")
        req = self.request(xai, {"XAI_API_KEY": "fixture-key"})
        self.assertEqual(req.url, "https://api.x.ai/v1/chat/completions")
        self.assertEqual(json.loads(req.body)["model"], "grok-fixture")
        req = self.request(OpenAICompatLLM("gpt-fixture", "k", None),
                           {"OPENAI_API_KEY": "fixture-key", "OPENAI_BASE_URL": "https://openrouter.example/v1"})
        self.assertEqual(req.url, "https://openrouter.example/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
