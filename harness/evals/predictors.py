"""Predictors the backtest can score. Each one ranks posts it sees blind: text and account only.

A predictor returns a score per post id. Higher means "expected to do better than this account's usual
post". If `probabilistic` is True the score is read as a 0-1 probability and gets a calibration report.
Add a predictor by implementing `predict` and registering a factory in `build()`.
"""

import hashlib
import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from harness.evals.dataset import Post
from harness.llm import LLM
from harness.models import Action
from harness.qualify import load_panel, panel_mean, panel_scores


@dataclass
class Prediction:
    score: float
    detail: dict = field(default_factory=dict)


class Predictor(Protocol):
    name: str
    probabilistic: bool

    def predict(self, posts: list[Post]) -> dict[str, Prediction]: ...


class RandomPredictor:
    """Chance level, seeded per post so a rerun gives the same numbers."""

    name = "random"
    probabilistic = False

    def __init__(self, seed: int = 0):
        self.seed = seed

    def predict(self, posts: list[Post]) -> dict[str, Prediction]:
        def draw(p: Post) -> float:
            digest = hashlib.sha256(f"{self.seed}:{p.id}".encode()).hexdigest()
            return random.Random(int(digest[:16], 16)).random()

        return {p.id: Prediction(draw(p)) for p in posts}


class MediaPredictor:
    """Video > photo or gif > text only. The harness drafts text, so this measures what text can't see."""

    name = "media"
    probabilistic = False
    RANK = {"video": 2.0, "photo/video": 2.0, "photo": 1.0, "gif": 1.0, "none": 0.0}

    def predict(self, posts: list[Post]) -> dict[str, Prediction]:
        unknown = {p.media for p in posts} - set(self.RANK)
        if unknown:
            raise ValueError(f"MediaPredictor has no rank for media {sorted(unknown)}")
        return {p.id: Prediction(self.RANK[p.media]) for p in posts}


class LengthPredictor:
    """Longer text scores higher. A cheap baseline an LLM judge should beat."""

    name = "length"
    probabilistic = False

    def predict(self, posts: list[Post]) -> dict[str, Prediction]:
        return {p.id: Prediction(float(len(p.clean_text))) for p in posts}


class PanelPredictor:
    """The production audience panel from harness/qualify.py, called with the same prompt.

    Posts go to the panel one account at a time (up to `batch` per call), the way qualify scores one
    brand's drafts together, so within-account comparisons happen inside one context.
    """

    name = "panel"
    probabilistic = True

    def __init__(self, llm: LLM, panel_path: Path, batch: int = 20, workers: int = 4):
        self.llm, self.panel, self.batch, self.workers = llm, load_panel(panel_path), batch, workers
        self.calls = 0
        self.prompt_chars = 0

    def _batches(self, posts: list[Post]) -> list[list[Post]]:
        by_account: dict[str, list[Post]] = defaultdict(list)
        for p in posts:
            by_account[p.account].append(p)
        return [group[i:i + self.batch] for group in by_account.values() for i in range(0, len(group), self.batch)]

    def _score_batch(self, batch: list[Post]) -> dict[str, Prediction]:
        actions = [Action(type="post", text=p.clean_text, mind="backtest", narrative_id="", id=f"p{i}")
                   for i, p in enumerate(batch)]
        self.calls += 1
        self.prompt_chars += sum(len(a.text) for a in actions) + sum(len(v) for v in self.panel.values())
        scores = panel_scores(self.llm, actions, self.panel)
        out = {}
        for a, p in zip(actions, batch):
            s = {k: float(v) for k, v in scores.get(a.id, {}).items() if k in self.panel}
            if set(s) != set(self.panel):
                raise ValueError(f"panel returned panelists {sorted(s)} for {p.id}; expected {sorted(self.panel)}")
            out[p.id] = Prediction(panel_mean(s), {"panelists": s})
        return out

    def predict(self, posts: list[Post]) -> dict[str, Prediction]:
        from concurrent.futures import ThreadPoolExecutor

        out: dict[str, Prediction] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for part in pool.map(self._score_batch, self._batches(posts)):
                out.update(part)
        return out


class PostJudgePredictor:
    """Dhilan's reference-based judge (harness/post_judge, PR #5), scored as score_100 / 100.

    `grade(post)` returns his `Evaluation`, or anything with `score_100`, `grade.confidence` and
    `references` (each with `id` and `date`). Every reference must predate the holdout; the predictor
    checks that itself rather than trusting the selector, because a leaked reference is the one
    mistake that would make the judge look better than it is.
    """

    name = "post_judge"
    probabilistic = True

    def __init__(self, grade: Callable[[Post], Any], holdout_from: date, workers: int = 4):
        self.grade, self.holdout_from, self.workers = grade, holdout_from, workers

    def _one(self, p: Post) -> tuple[str, Prediction]:
        ev = self.grade(p)
        score = getattr(ev, "score_100", None)
        if not isinstance(score, int | float) or not 0 <= score <= 100:
            raise ValueError(f"post_judge returned score_100={score!r} for {p.id}; expected 0-100")
        refs = list(getattr(ev, "references", []))
        leaked = [r.id for r in refs if r.id == p.id or date.fromisoformat(str(r.date)) >= self.holdout_from]
        if leaked:
            raise ValueError(f"post_judge graded {p.id} against holdout posts {leaked}")
        confidence = getattr(getattr(ev, "grade", None), "confidence", None)
        detail = {"confidence": getattr(confidence, "value", confidence), "references": [r.id for r in refs],
                  "judge_provider": getattr(ev, "judge_provider", None), "judge_model": getattr(ev, "judge_model", None)}
        return p.id, Prediction(score / 100, detail)

    def predict(self, posts: list[Post]) -> dict[str, Prediction]:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return dict(pool.map(self._one, posts))


class JudgeRankPredictor:
    """Dhilan's `rank_candidates`: one call ranks 2-6 posts from the same account against each other.

    Its scores are relative within a call, so only the within-account numbers mean anything; the
    sample should hold at most `batch` posts per account so each account is ranked in one call.
    An account with a single post can't be ranked and gets the neutral score 50.
    """

    name = "post_judge_rank"
    probabilistic = False

    def __init__(self, rank: Callable[[list[Post]], dict[str, float]], batch: int = 6, workers: int = 4):
        if not 2 <= batch <= 6:
            raise ValueError("rank_candidates takes 2 to 6 candidates per call")
        self.rank, self.batch, self.workers = rank, batch, workers

    def _chunks(self, posts: list[Post]) -> list[list[Post]]:
        by_account: dict[str, list[Post]] = defaultdict(list)
        for p in posts:
            by_account[p.account].append(p)
        out = []
        for group in by_account.values():
            n_calls = -(-len(group) // self.batch)
            size = -(-len(group) // n_calls)  # even sizes, so no call is left with one post
            out += [group[i:i + size] for i in range(0, len(group), size)]
        return out

    def predict(self, posts: list[Post]) -> dict[str, Prediction]:
        from concurrent.futures import ThreadPoolExecutor

        chunks = self._chunks(posts)
        out = {c[0].id: Prediction(50.0, {"unranked": True}) for c in chunks if len(c) == 1}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for chunk, scores in zip((c for c in chunks if len(c) > 1),
                                     pool.map(self.rank, (c for c in chunks if len(c) > 1))):
                if set(scores) != {p.id for p in chunk}:
                    raise ValueError(f"rank_candidates scored {sorted(scores)}; expected {[p.id for p in chunk]}")
                out.update({pid: Prediction(float(sc)) for pid, sc in scores.items()})
        return out


def from_post_judge_rank(holdout_from: date, per_class: int = 3) -> JudgeRankPredictor:
    """Wire Dhilan's rank_candidates with train-only references chosen for the whole batch."""
    try:
        from baml_sdk import CandidatePost, RankingCandidate
        from harness.post_judge.judge import DEFAULT_DATASET
        from harness.post_judge.ranking import rank_candidates
        from harness.post_judge.references import load_references, select_references
    except ImportError as e:
        raise SystemExit(f"post_judge ranking isn't importable ({e}); it lands with PR #5 (dhilan/version-1)") from e
    pool = load_references(DEFAULT_DATASET)
    sha = hashlib.sha256(DEFAULT_DATASET.read_bytes()).hexdigest()

    def rank(chunk: list[Post]) -> dict[str, float]:
        mission = f"X account of {chunk[0].account}, a developer-tools company."
        candidates = [RankingCandidate(id=p.id, post=CandidatePost(text=p.clean_text, mission=mission, context="",
                                                                    lane="post")) for p in chunk]
        refs = select_references(pool, " ".join(p.clean_text for p in chunk), per_class=per_class,
                                 before=holdout_from, exclude_ids={p.id for p in chunk})
        leaked = [r.id for r in refs if date.fromisoformat(str(r.date)) >= holdout_from]
        if leaked:
            raise ValueError(f"rank_candidates got holdout references {leaked}")
        ev = rank_candidates(candidates, refs, dataset_sha256=sha)
        return {item.candidate_id: item.score for item in ev.ranking.ranked}

    from harness.post_judge.judge import judge_settings

    pred = JudgeRankPredictor(rank)
    pred.model_info = dict(zip(("provider", "model"), judge_settings()))
    return pred


def from_post_judge(holdout_from: date, per_class: int = 3) -> PostJudgePredictor:
    """Wire Dhilan's package. References come from his selector with before=holdout_from (train only)."""
    try:
        from baml_sdk import CandidatePost
        from harness.post_judge.judge import DEFAULT_DATASET, grade_with_references
        from harness.post_judge.references import load_references, select_references
    except ImportError as e:
        raise SystemExit(
            f"post_judge isn't importable ({e}). It lands with PR #5 (dhilan/version-1); after merging, "
            "generate baml_sdk per eval/README.md and set HARNESS_JUDGE_PROVIDER / HARNESS_JUDGE_MODEL."
        ) from e
    pool = load_references(DEFAULT_DATASET)
    sha = hashlib.sha256(DEFAULT_DATASET.read_bytes()).hexdigest()

    def grade(p: Post):
        candidate = CandidatePost(text=p.clean_text, mission=f"X account of {p.account}, a developer-tools company.",
                                  context="", lane="post")
        refs = select_references(pool, p.clean_text, per_class=per_class, before=holdout_from, exclude_ids={p.id})
        return grade_with_references(candidate, refs, dataset_sha256=sha)

    from harness.post_judge.judge import judge_settings

    pred = PostJudgePredictor(grade, holdout_from)
    pred.model_info = dict(zip(("provider", "model"), judge_settings()))
    return pred


def build(names: list[str], llm: LLM, panel_path: Path, holdout_from: date, seed: int) -> list[Predictor]:
    factories: dict[str, Callable[[], Predictor]] = {
        "random": lambda: RandomPredictor(seed),
        "media": MediaPredictor,
        "length": LengthPredictor,
        "panel": lambda: PanelPredictor(llm, panel_path),
        "post_judge": lambda: from_post_judge(holdout_from),
        "post_judge_rank": lambda: from_post_judge_rank(holdout_from),
    }
    unknown = set(names) - set(factories)
    if unknown:
        raise SystemExit(f"unknown predictors {sorted(unknown)}; choose from {sorted(factories)}")
    return [factories[n]() for n in names]
