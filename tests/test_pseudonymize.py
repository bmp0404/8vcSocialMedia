"""Private people's @handles stay out of committed experiment data (harness/minds/lab/pseudonymize.py)."""

import json
import re
from pathlib import Path

from harness import config
from harness.minds.lab.pseudonymize import HANDLE, pseudonymize, public_handles

COMMITTED = [config.ROOT / p for p in (
    "action/fixtures/x_threads.json", "eval/experiments/fixtures/x_threads-extra-20261016.json",
    "action/experiments/reply_experiment_raw.json", "action/experiments/side_experiment_raw.json",
    "action/experiments/drafts.json", "action/experiments/gate_calibration.json",
    "action/experiments/live_decide-20261015-210304.json", "action/experiments/live_decide-20261015-210939.json",
)]


def test_handles_become_the_same_pseudonym_everywhere_and_public_ones_stay(tmp_path):
    threads = tmp_path / "threads.json"
    threads.write_text(json.dumps({"threads": [{"author": "WNBA", "text": "hi @WNBA",
                                                "direct_replies": [{"text": "@fan_one @Fan_One and @fan_two"}]}]}),
                       encoding="utf-8")
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"gen|1|continuation": {"text": "yo @fan_two", "raw": "@fan_three page"}}) + "\n",
                     encoding="utf-8")
    assert pseudonymize([threads, cache]) == {threads: 2, cache: 0}
    assert json.loads(threads.read_text(encoding="utf-8"))["threads"][0]["direct_replies"][0]["text"] == \
        "@person01 @person01 and @person02"
    assert json.loads(threads.read_text(encoding="utf-8"))["threads"][0]["text"] == "hi @WNBA"
    assert json.loads(cache.read_text(encoding="utf-8")) == {"gen|1|continuation": {"text": "yo @person02"}}
    assert cache.read_text(encoding="utf-8").endswith("\n") and not threads.read_text(encoding="utf-8").endswith("\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cache.json", "threads.json"]  # no mapping kept


def test_committed_experiment_data_names_only_public_accounts():
    keep = public_handles(COMMITTED)
    leaked = {f"{p.relative_to(config.ROOT)}: @{h}" for p in COMMITTED for h in HANDLE.findall(p.read_text(encoding="utf-8"))
              if h.lower() not in keep and not re.fullmatch(r"person\d+", h)}
    assert leaked == set()
