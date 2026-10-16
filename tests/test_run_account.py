"""A round's files name the account the round was made for, from [brand] x_handle or HARNESS_ACCOUNT_HANDLE."""

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
    monkeypatch.setenv("MEMSTORE_PATH", str(tmp_path / "store"))
    monkeypatch.delenv("HARNESS_OBSERVE_CONFIG", raising=False)
    return tmp_path


def exported_account(run_id: int) -> str | None:
    return json.loads(export_approved(run_id).read_text(encoding="utf-8"))["account"]


def test_hand_off_names_the_runs_account_even_after_the_setting_changes(isolated, monkeypatch):
    run_id = memory.save_run([Action(type="post", text="a post", mind="m", narrative_id="n1", id="a1")], account="_sgu_")
    memory.set_review("a1", "approved")
    monkeypatch.setenv("HARNESS_ACCOUNT_HANDLE", "other")
    assert exported_account(run_id) == "_sgu_"


def test_hand_off_of_a_run_saved_without_an_account_says_null(isolated):
    run_id = memory.save_run([Action(type="post", text="a post", mind="m", narrative_id="n1", id="a1")])
    memory.set_review("a1", "approved")
    assert exported_account(run_id) is None


def test_mock_run_records_the_account(isolated, monkeypatch):
    monkeypatch.setenv("HARNESS_ACCOUNT_HANDLE", "@other")
    cli.run(argparse.Namespace(provider="mock", sources="sample", narratives=2, per_mind=1, top=10, retries=0))
    (path,) = (isolated / "out").glob("recommendations-*.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["account"] == "other"
    assert memory.run_account(data["run_id"]) == "other"
