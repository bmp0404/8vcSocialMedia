"""Phase D: score the production judges on the post benchmark's test authors.

Two judges, each scoring one post per call so no batch order can move them:
- panel: `harness.qualify.panel_scores` with the dev-tool personas (eval/panels/devtools.json), the
  mean of the panelists' 0-1 engagement guesses. Production runs the panel on the run's model.
- baml_judge: `GradePost` from eval/baml_src (harness/post_judge), score_100 from its five rubric
  scores, after the same evidence checks `grade_with_references` makes. References come from the
  dataset CSV, posted before the candidate's day, and never from the candidate's own account, so the
  judge ranks an account it knows nothing about, as the TF-IDF ridge and the label model do.

Per-item scores are cached in data/bench/results/judges/<judge>-<model>.jsonl (status IDs and
numbers only), so a rerun spends no calls on items already scored.
"""

import html
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from harness.corpus import Corpus
from harness.corpus.bench import BENCH_DIR, Pair
from harness.corpus.stats import accuracy, pair_credit, paired_difference

JUDGE_DIR = BENCH_DIR / "results" / "judges"
MISSION = "X account of a developer-tools company."  # harness/evals/predictors.MISSION: no handle
_TCO = re.compile(r"\s*https://t\.co/\S+")


def judge_text(text: str) -> str:
    """What a judge reads: t.co links removed and HTML entities decoded, as harness/evals does."""
    return html.unescape(_TCO.sub("", text)).strip()


class Cache:
    def __init__(self, path: Path):
        self.path, self.lock = path, threading.Lock()
        self.rows = {}
        if path.exists():
            for row in map(json.loads, path.open(encoding="utf-8")):
                self.rows[row["status_id"]] = row

    def put(self, row: dict) -> None:
        with self.lock:
            self.rows[row["status_id"]] = row
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

    def scores(self) -> dict[str, float]:
        return {sid: r["score"] for sid, r in self.rows.items() if r.get("score") is not None}


class CallCounter:
    """Reserves the most calls an attempt can make before it starts, so the hard limit holds, and
    counts the calls that actually happened for the report."""

    def __init__(self, limit: int):
        self.limit, self.reserved, self.actual, self.lock = limit, 0, 0, threading.Lock()

    def take(self, n: int) -> None:
        with self.lock:
            if self.reserved + n > self.limit:
                raise RuntimeError(f"judge call budget of {self.limit} reached")
            self.reserved += n

    def spent(self, reserved: int, actual: int) -> None:
        with self.lock:
            self.reserved += actual - reserved
            self.actual += actual


def score_panel(items: list[str], corpus: Corpus, model: str, budget: CallCounter, workers: int = 6) -> Cache:
    from harness.llm import AnthropicLLM
    from harness.models import Action
    from harness.qualify import load_panel, panel_mean, panel_scores
    from harness import config
    panel = load_panel(config.ROOT / "eval" / "panels" / "devtools.json")
    cache = Cache(JUDGE_DIR / f"panel-{model}.jsonl")
    calls = threading.local()

    class Counted(AnthropicLLM):
        def complete(self, system: str, prompt: str) -> str:
            calls.n = getattr(calls, "n", 0) + 1
            return super().complete(system, prompt)
    llm = Counted(model)

    def one(sid: str) -> None:
        action = Action(type="post", text=judge_text(corpus.get(sid).text), mind="phase_d", narrative_id="", id="p0")
        for attempt in range(2):  # complete_json already asks once more on a non-JSON answer
            budget.take(2)  # an answer that isn't JSON costs a second call
            calls.n = 0
            try:
                s = panel_scores(llm, [action], panel).get("p0", {})
                s = {k: float(v) for k, v in s.items() if k in panel}
                if set(s) == set(panel):
                    cache.put({"status_id": sid, "score": panel_mean(s), "panelists": s, "attempts": attempt + 1})
                    return
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                err = type(e).__name__
            else:
                err = f"panelists {sorted(s)}"
            finally:
                budget.spent(2, calls.n)
        cache.put({"status_id": sid, "score": None, "error": err})

    todo = [sid for sid in items if sid not in cache.rows]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))
    return cache


def score_baml_judge(items: list[str], corpus: Corpus, model: str, budget: CallCounter, workers: int = 4) -> Cache:
    from harness import config
    from baml_sdk import CandidatePost, GradePost
    from harness.baml_reply import ReplyRecorder
    from harness.post_judge.judge import check_reply, validate_grade
    from harness.post_judge.references import load_references, normalized_text, select_references
    pool_refs = load_references(config.ROOT / "data" / "devtools_virality_30d.csv")
    cache = Cache(JUDGE_DIR / f"baml_judge-{model}.jsonl")

    def one(sid: str) -> None:
        item = corpus.get(sid)
        text = judge_text(item.text)
        own = f"@{item.author}".casefold()
        refs_pool = [r for r in pool_refs if r.account.casefold() != own
                     and normalized_text(r.text) != normalized_text(text)]
        try:
            refs = select_references(refs_pool, text, per_class=3, before=item.created_at.date())
        except ValueError as e:
            cache.put({"status_id": sid, "score": None, "error": f"references: {e}"})
            return
        leaked = [r.id for r in refs if date.fromisoformat(r.date) >= item.created_at.date()]
        if leaked:
            raise ValueError(f"references from the candidate's day or later: {leaked}")
        candidate = CandidatePost(text=text, mission=MISSION, context="An original post, not a reply.", lane="post")
        err = None
        for attempt in range(2):
            budget.take(6)  # BAML re-asks once on a parse failure, and its Retry client tries 3 times
            reply = ReplyRecorder()
            try:
                grade = GradePost(candidate, refs, provider="anthropic", model=model, on_event=reply)
                check_reply(grade, reply.text)
                score = validate_grade(grade, refs)
            except Exception as e:  # noqa: BLE001 - InvalidGradeError or a BAML/API error; recorded, retried once
                err = f"{type(e).__name__}: {str(e)[:120]}"
                continue
            finally:
                budget.spent(6, max(reply.replies, 1))
            cache.put({"status_id": sid, "score": score, "verdict": str(getattr(grade.verdict, "value", grade.verdict)),
                       "confidence": str(getattr(grade.confidence, "value", grade.confidence)),
                       "references": [r.id for r in refs], "replies": reply.replies, "attempts": attempt + 1})
            return
        cache.put({"status_id": sid, "score": None, "error": err})

    todo = [sid for sid in items if sid not in cache.rows]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))
    return cache


def compare(test: list[Pair], scorers: dict[str, dict[str, float]], judges: tuple[str, ...],
            baselines: tuple[str, ...], b: int = 2000) -> dict:
    """Accuracy of every scorer on the test pairs it covers, and each judge minus each baseline on the
    pairs both cover, with author-cluster bootstrap intervals."""
    out = {"accuracy": {}, "paired": {}}
    for name, s in scorers.items():
        ps = [p for p in test if p.winner in s and p.loser in s]
        out["accuracy"][name] = accuracy([pair_credit(s[p.winner], s[p.loser]) for p in ps],
                                         [p.group for p in ps], b).to_dict()
    for j in judges:
        for base in baselines:
            sj, sb = scorers[j], scorers[base]
            ps = [p for p in test if all(x in sj and x in sb for x in (p.winner, p.loser))]
            out["paired"][f"{j} - {base}"] = paired_difference(
                [pair_credit(sj[p.winner], sj[p.loser]) for p in ps],
                [pair_credit(sb[p.winner], sb[p.loser]) for p in ps], [p.group for p in ps], b).to_dict()
    return out
