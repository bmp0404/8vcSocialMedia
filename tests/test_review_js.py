"""Runs the console's JavaScript unit tests (tests/js/) with Node's built-in runner, when Node is installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

JS_TESTS = Path(__file__).parent / "js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")
def test_js_units():
    files = sorted(str(p) for p in JS_TESTS.glob("*.test.mjs"))
    result = subprocess.run(["node", "--test", *files], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
