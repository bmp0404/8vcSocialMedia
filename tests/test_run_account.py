"""The files a round hands on name the account they are for, from [brand] x_handle or HARNESS_ACCOUNT_HANDLE."""

import argparse
import json

import pytest

from harness import cli, config, memory
from harness.models import Action
from harness.review import export_approved


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.db")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    monkeypatch.delenv("HARNESS_OBSERVE_CONFIG", raising=False)
    monkeypatch.delenv("HARNESS_ACCOUNT_HANDLE", raising=False)
    return tmp_path


def test_hand_off_names_the_account(isolated, monkeypatch):
    run_id = memory.save_run([Action(type="post", text="a post", mind="m", narrative_id="n1", id="a1")])
    memory.set_review("a1", "approved")
    assert json.loads(export_approved(run_id).read_text(encoding="utf-8"))["account"] == "_sgu_"
    monkeypatch.setenv("HARNESS_ACCOUNT_HANDLE", "@other")
    assert json.loads(export_approved(run_id).read_text(encoding="utf-8"))["account"] == "other"


def test_mock_run_names_the_account_in_its_recommendations(isolated):
    args = argparse.Namespace(provider="mock", sources="sample", narratives=2, per_mind=1, top=10, retries=0)
    cli.run(args)
    (path,) = (isolated / "out").glob("recommendations-*.json")
    assert json.loads(path.read_text(encoding="utf-8"))["account"] == "_sgu_"
