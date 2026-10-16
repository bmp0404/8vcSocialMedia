"""BAML's Anthropic and OpenAI clients send what the official SDKs send for the same environment.

The SDK side is a real request from anthropic.Anthropic() or openai.OpenAI(), captured by an httpx2
mock transport; harness/llm.py builds its clients the same way. The BAML side is the request the
judge renders with .build_request(). BAML's preview leaves out x-api-key, so the API key is checked
on the client value instead.
"""

import os
from unittest.mock import patch

import anthropic
import httpx2
import openai
import pytest

from baml_sdk import CandidatePost, GradePost_spec, Performance, ReferencePost, judge_api_client
from harness import baml_adapter
from harness.llm import AnthropicLLM, OpenAICompatLLM

ANTHROPIC_REPLY = {"id": "msg_1", "type": "message", "role": "assistant", "model": "m",
                   "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                   "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
OPENAI_REPLY = {"id": "r1", "object": "response", "created_at": 0, "model": "m", "status": "completed",
                "output": [], "parallel_tool_calls": False, "tool_choice": "auto", "tools": []}

ENVS = {
    "gateway token": {"ANTHROPIC_BASE_URL": "https://gw.example", "ANTHROPIC_AUTH_TOKEN": "tok"},
    "gateway token, trailing slash": {"ANTHROPIC_BASE_URL": "https://gw.example/", "ANTHROPIC_AUTH_TOKEN": "tok"},
    "API key": {"ANTHROPIC_API_KEY": "key"},
    "API key and token": {"ANTHROPIC_API_KEY": "key", "ANTHROPIC_AUTH_TOKEN": "tok"},
    "API key, empty token": {"ANTHROPIC_API_KEY": "key", "ANTHROPIC_AUTH_TOKEN": ""},
}


def captured(make_client, call, reply):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(200, json=reply)

    call(make_client(httpx2.Client(transport=httpx2.MockTransport(handler))))
    return seen[0]


def sdk_anthropic(env):
    with patch.dict(os.environ, env, clear=True):
        return captured(lambda http: anthropic.Anthropic(http_client=http),
                        lambda c: c.messages.create(model="m", max_tokens=8, messages=[{"role": "user", "content": "x"}]),
                        ANTHROPIC_REPLY)


def judge_request(env, provider="anthropic"):
    candidate = CandidatePost(text="t", mission="m", context="c", lane="post")
    refs = [ReferencePost(id="a", text="x", account="@a", performance=Performance.Successful, category="c",
                          engagement=1, media="none", date="2026-09-01")]
    with patch.dict(os.environ, env, clear=True):
        return GradePost_spec(candidate, refs, provider=provider, model="m").build_request()


@pytest.mark.parametrize("env", ENVS.values(), ids=ENVS.keys())
def test_judge_anthropic_request_matches_sdk(env):
    sdk, baml = sdk_anthropic(env), judge_request(env)
    assert baml.url == str(sdk.url)
    assert baml.headers.get("authorization") == sdk.headers.get("authorization")


@pytest.mark.parametrize("env,variable", [
    (ENVS["gateway token"], "ANTHROPIC_AUTH_TOKEN"),
    (ENVS["API key and token"], "ANTHROPIC_API_KEY"),
    (ENVS["API key, empty token"], "ANTHROPIC_API_KEY"),
    ({}, "ANTHROPIC_API_KEY"),  # neither set: the error names the variable the SDK reads first
])
def test_judge_anthropic_key_variable(env, variable):
    with patch.dict(os.environ, env, clear=True):
        assert judge_api_client("anthropic", "m").api_key.name == variable


def test_judge_openai_gateway_url_matches_sdk():
    env = {"OPENAI_API_KEY": "key", "OPENAI_BASE_URL": "https://or.example/v1/"}
    with patch.dict(os.environ, env, clear=True):
        sdk = captured(lambda http: openai.OpenAI(http_client=http),
                       lambda c: c.responses.create(model="m", input="x"), OPENAI_REPLY)
    assert judge_request(env, provider="openai").url == str(sdk.url)


def orient_request(llm, env):
    with patch.dict(os.environ, env, clear=True):
        return baml_adapter.spec(llm, "OrientSignals", "mission", "[]", 3).build_request()


@pytest.mark.parametrize("env", ENVS.values(), ids=ENVS.keys())
def test_orient_anthropic_request_matches_sdk(env):
    with patch.dict(os.environ, env, clear=True):
        llm = AnthropicLLM("m")
    sdk, baml = sdk_anthropic(env), orient_request(llm, env)
    assert baml.url == str(sdk.url)
    assert baml.headers.get("authorization") == sdk.headers.get("authorization")


def test_orient_openai_gateway_url_matches_sdk():
    env = {"OPENAI_API_KEY": "key", "OPENAI_BASE_URL": "https://or.example/v1/"}
    with patch.dict(os.environ, env, clear=True):
        sdk = captured(lambda http: openai.OpenAI(http_client=http),
                       lambda c: c.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}]),
                       {"id": "c1", "object": "chat.completion", "created": 0, "model": "m", "choices": []})
    llm = OpenAICompatLLM("m", "key", "https://or.example/v1/")
    assert orient_request(llm, env).url == str(sdk.url)
