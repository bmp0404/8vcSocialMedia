"""The committed baml_sdk was generated from the .baml files in eval/baml_src.

Editing a .baml file without regenerating leaves the old prompt running, because Python loads the
generated package, not the source. These checks need no BAML toolchain. CI's baml job also
regenerates the package and diffs everything except _inlinedbaml.py, the compiled program, whose
bytes embed the generating machine's path; the rendered-prompt test covers that file.
"""

import importlib.util
import json
import os
import re
from unittest.mock import patch

import pytest

from baml_sdk import CandidatePost, GradePost_spec, Performance, RankCandidates_spec, ReferencePost
from harness import baml_adapter
from harness.config import ROOT
from harness.llm import AnthropicLLM

SOURCES = ROOT / "eval" / "baml_src"


def test_generated_sources_match_baml_src():
    spec = importlib.util.spec_from_file_location("generated_sources", ROOT / "baml_sdk" / "_baml_sources.py")
    generated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generated)
    on_disk = {p.relative_to(SOURCES).as_posix(): p.read_text(encoding="utf-8") for p in SOURCES.rglob("*.baml")}
    stale = sorted(name for name in on_disk.keys() | generated.FILES.keys() if on_disk.get(name) != generated.FILES.get(name))
    assert not stale, f"baml_sdk is stale for {stale}; run BAML_VERSION=0.22.2 baml generate --directory eval"


def prompt_lines(source: str, function: str) -> list[str]:
    """The literal lines of `function`'s prompt in a .baml source, without interpolations."""
    body = source[source.index(f"function {function}("):]
    prompt = re.search(r"prompt: `(.*?)\n\s*`\n}", body, re.S).group(1)
    return [line.strip() for line in prompt.splitlines() if line.strip() and "${" not in line]


def rendered_text(request) -> str:
    body = json.loads(request.body)
    blocks = body["system"] + [part for m in body["messages"] for part in m["content"]]
    return "\n".join(block["text"] for block in blocks)


CANDIDATE = CandidatePost(text="t", mission="m", context="c", lane="post")
REFS = [ReferencePost(id="a", text="x", account="@a", performance=Performance.Successful, category="c",
                      engagement=1, media="none", date="2026-09-01")]


@pytest.mark.parametrize("file,function,build", [
    ("orient.baml", "OrientSignals",
     lambda: baml_adapter.spec(AnthropicLLM("m"), "OrientSignals", "mission", "[]", 3).build_request()),
    ("main.baml", "GradePost",
     lambda: GradePost_spec(CANDIDATE, REFS, provider="anthropic", model="m").build_request()),
    ("main.baml", "RankCandidates",
     lambda: RankCandidates_spec([], REFS, provider="anthropic", model="m").build_request()),
])
def test_runtime_renders_the_current_prompt(file, function, build):
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-key"}, clear=True):
        text = rendered_text(build())
    missing = [line for line in prompt_lines((SOURCES / file).read_text(encoding="utf-8"), function) if line not in text]
    assert not missing, f"baml_sdk renders an older {function} prompt; regenerate it. Missing: {missing[:3]}"
