"""Predictors the backtest can score. Each one sees a post's text (and, for baselines, its media type),
never its engagement.

A predictor returns a score per post id; higher means "more likely to be the account's viral post".
Model predictors score each post in its own call by default. A model scoring several posts in one call
assimilates them: the research audit (8vc-context/research/eval.md, finding 1) measured the panel's AUC
at 0.696 with viral posts first in the batch and 0.821 with them last. When a predictor must batch, it
shuffles with a logged seed and averages the forward and reverse orders.
A predictor may leave out posts it failed on and list them in `failures`; the backtest then scores
every predictor on the posts all of them scored.
Add a predictor by implementing `predict` and registering a factory in `build()`.
"""

import hashlib
import math
import random
import re
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from harness.evals.dataset import Post
from harness.llm import LLM
from harness.models import Action
from harness.qualify import load_panel, panel_mean, panel_scores

READER_PANEL = {"typical_follower": "A typical follower of this account on X."}
MISSION = "X account of a developer-tools company."  # no handle, so the judge can't key on brand size


@dataclass
class Prediction:
    score: float
    detail: dict = field(default_factory=dict)


class Predictor(Protocol):
    name: str

    def predict(self, posts: list[Post]) -> dict[str, Prediction]: ...


def with_retries(fn: Callable, attempts: int = 3) -> Callable:
    """Retry a model call that failed validation (bad JSON, an invented citation). Judge calls fail
    now and then, and one failure shouldn't abort a 100-call backtest."""
    def call(*args):
        for i in range(attempts):
            try:
                return fn(*args)
            except Exception as e:  # noqa: BLE001 - re-raised after the last attempt
                if i == attempts - 1:
                    raise
                print(f"[eval] retrying after {type(e).__name__}: {str(e)[:120]}")
    return call


def orders(chunk: list, seed: str) -> list[list]:
    """A seeded shuffle of the chunk and its reverse, so position effects cancel in the average."""
    shuffled = list(chunk)
    random.Random(seed).shuffle(shuffled)
    return [shuffled, shuffled[::-1]] if len(shuffled) > 1 else [shuffled]


class RandomPredictor:
    """Chance level, seeded per post so a rerun gives the same numbers."""

    name = "random"

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
    RANK = {"video": 2.0, "photo/video": 2.0, "photo": 1.0, "gif": 1.0, "none": 0.0}

    def predict(self, posts: list[Post]) -> dict[str, Prediction]:
        unknown = {p.media for p in posts} - set(self.RANK)
        if unknown:
            raise ValueError(f"MediaPredictor has no rank for media {sorted(unknown)}")
        return {p.id: Prediction(self.RANK[p.media]) for p in posts}


class LengthPredictor:
    """Longer text scores higher. A cheap baseline an LLM judge should beat."""

    name = "length"

    def predict(self, posts: list[Post]) -> dict[str, Prediction]:
        return {p.id: Prediction(float(len(p.clean_text))) for p in posts}


class AccountPredictor:
    """Each account's viral rate over its eligible posts outside the scored sample. It knows nothing
    about the post, so it scores exactly 0.5 within accounts; across accounts it is the number to beat
    (account identity alone separates viral from Deadweight at AUC 0.906 per
    8vc-context/research/memory.md, finding 2). Leaving out only the post itself would not do: an
    account's viral posts would then see one fewer viral neighbour and score below its flops."""

    name = "account"

    def __init__(self, pool: list[Post]):
        self.pool = pool

    def predict(self, posts: list[Post]) -> dict[str, Prediction]:
        scored = {p.id for p in posts}
        by_account: dict[str, list[bool]] = defaultdict(list)
        for q in self.pool:
            if q.id not in scored:
                by_account[q.account].append(q.viral)
        return {p.id: Prediction(sum(v) / len(v) if (v := by_account[p.account]) else 0.5) for p in posts}


_WORD = re.compile(r"[a-z0-9][a-z0-9'.+-]*")


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class TfidfPredictor:
    """TF-IDF logistic regression on the text, cross-fitted by account.

    Accounts are split into folds; each post is scored by a model trained only on other folds'
    accounts, so the model can't learn a product name and recognize its brand. Labels are viral vs
    Deadweight over `pool` (all eligible posts). Plain Python to avoid a scikit-learn dependency.
    """

    name = "tfidf"

    def __init__(self, pool: list[Post], folds: int = 5, seed: int = 0, epochs: int = 300, lr: float = 2.0,
                 l2: float = 1e-3):
        self.pool, self.folds, self.seed, self.epochs, self.lr, self.l2 = pool, folds, seed, epochs, lr, l2

    def fold(self, account: str) -> int:
        return int(hashlib.sha256(f"{self.seed}:{account}".encode()).hexdigest(), 16) % self.folds

    @staticmethod
    def _vectors(docs: list[list[str]], idf: dict[str, float]) -> list[dict[str, float]]:
        out = []
        for doc in docs:
            tf: dict[str, float] = defaultdict(float)
            for t in doc:
                if t in idf:
                    tf[t] += 1.0
            vec = {t: c * idf[t] for t, c in tf.items()}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            out.append({t: v / norm for t, v in vec.items()})
        return out

    def _fit(self, train: list[Post]) -> Callable[[Post], float]:
        docs = [_tokens(p.clean_text) for p in train]
        df: dict[str, int] = defaultdict(int)
        for d in docs:
            for t in set(d):
                df[t] += 1
        n = len(docs)
        idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items() if c >= 2}
        xs, ys = self._vectors(docs, idf), [1.0 if p.viral else 0.0 for p in train]
        w: dict[str, float] = defaultdict(float)
        b = 0.0
        for _ in range(self.epochs):  # full-batch gradient descent on the L2-penalized log loss
            gw: dict[str, float] = defaultdict(float)
            gb = 0.0
            for x, y in zip(xs, ys):
                z = b + sum(w[t] * v for t, v in x.items())
                err = 1 / (1 + math.exp(-max(-30.0, min(30.0, z)))) - y
                gb += err
                for t, v in x.items():
                    gw[t] += err * v
            b -= self.lr * gb / n
            for t in set(gw) | set(w):
                w[t] -= self.lr * (gw[t] / n + self.l2 * w[t])

        def score(p: Post) -> float:
            (x,) = self._vectors([_tokens(p.clean_text)], idf)
            return b + sum(w.get(t, 0.0) * v for t, v in x.items())

        return score

    def predict(self, posts: list[Post]) -> dict[str, Prediction]:
        out = {}
        for f in range(self.folds):
            test = [p for p in posts if self.fold(p.account) == f]
            if not test:
                continue
            train = [q for q in self.pool if self.fold(q.account) != f]
            if {q.viral for q in train} != {True, False}:
                raise ValueError(f"fold {f} has only one class to train on")
            score = self._fit(train)
            out.update({p.id: Prediction(score(p), {"fold": f}) for p in test})
        return out


class PanelPredictor:
    """The audience panel from harness/qualify.py, called with the same prompt. Production ranks with the post ridge
    now; HARNESS_QUALIFY_RANKER=panel brings the panel back.

    `batch=1` scores each post alone. With a larger batch, posts from one account are cut into chunks,
    and each chunk is scored in a seeded shuffle and its reverse, then averaged. A one-entry panel
    (READER_PANEL) gives the no-persona reader the research audit asked for as a baseline.
    """

    def __init__(self, llm: LLM, panel: dict[str, str], name: str = "panel", batch: int = 1, workers: int = 8,
                 seed: int = 0):
        self.llm, self.panel, self.name, self.batch, self.workers, self.seed = llm, panel, name, batch, workers, seed
        self.calls = 0
        self.failures: list[str] = []

    def _chunks(self, posts: list[Post]) -> list[list[Post]]:
        by_account: dict[str, list[Post]] = defaultdict(list)
        for p in posts:
            by_account[p.account].append(p)
        return [group[i:i + self.batch] for group in by_account.values() for i in range(0, len(group), self.batch)]

    def _score(self, chunk: list[Post]) -> list[dict[str, float]]:
        actions = [Action(type="post", text=p.clean_text, mind="backtest", narrative_id="", id=f"p{i}")
                   for i, p in enumerate(chunk)]
        self.calls += 1
        scores = panel_scores(self.llm, actions, self.panel)
        out = []
        for a, p in zip(actions, chunk):
            s = {k: float(v) for k, v in scores.get(a.id, {}).items() if k in self.panel}
            if set(s) != set(self.panel):
                raise ValueError(f"panel returned panelists {sorted(s)} for {p.id}; expected {sorted(self.panel)}")
            out.append(s)
        return out

    def _chunk(self, chunk: list[Post], seed: str) -> dict[str, Prediction]:
        sums: dict[str, list[float]] = defaultdict(list)
        panelists: dict[str, list[dict]] = defaultdict(list)
        try:
            for order in orders(chunk, seed):
                for p, s in zip(order, with_retries(self._score)(order)):
                    sums[p.id].append(panel_mean(s))
                    panelists[p.id].append(s)
        except Exception as e:  # noqa: BLE001 - recorded and reported; the post drops out of every predictor
            print(f"[backtest] {self.name} gave up on {[p.id for p in chunk]}: {e}")
            self.failures += [p.id for p in chunk]
            return {}
        return {pid: Prediction(sum(v) / len(v), {"panelists": panelists[pid]}) for pid, v in sums.items()}

    def predict(self, posts: list[Post], seed: str | None = None) -> dict[str, Prediction]:
        seed = f"{self.seed}" if seed is None else seed
        out: dict[str, Prediction] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for part in pool.map(lambda c: self._chunk(c, f"{seed}:{c[0].id}"), self._chunks(posts)):
                out.update(part)
        return out


class PostJudgePredictor:
    """Dhilan's reference-based judge (harness/post_judge, PR #5), scored as score_100 / 100.

    `grade(post)` returns his `Evaluation`, or anything with `score_100`, `grade.confidence` and
    `references` (each with `id` and `date`). References come from a rolling origin: only posts
    dated before the candidate. The predictor checks that itself rather than trusting the selector,
    because a leaked reference is the one mistake that would make the judge look better than it is.
    """

    name = "post_judge"

    def __init__(self, grade: Callable[[Post], Any], workers: int = 4):
        self.grade, self.workers = with_retries(grade), workers
        self.failures: list[str] = []

    def _one(self, p: Post) -> tuple[str, Prediction | None]:
        try:
            ev = self.grade(p)
        except Exception as e:  # noqa: BLE001 - recorded and reported; the post drops out of every predictor
            print(f"[backtest] post_judge gave up on {p.id}: {str(e)[:160]}")
            self.failures.append(p.id)
            return p.id, None
        score = getattr(ev, "score_100", None)
        if not isinstance(score, int | float) or not 0 <= score <= 100:
            raise ValueError(f"post_judge returned score_100={score!r} for {p.id}; expected 0-100")
        refs = list(getattr(ev, "references", []))
        leaked = [r.id for r in refs if r.id == p.id or str(r.date) >= p.date.isoformat()]
        if leaked:
            raise ValueError(f"post_judge graded {p.id} against references from its own day or later: {leaked}")
        confidence = getattr(getattr(ev, "grade", None), "confidence", None)
        detail = {"confidence": getattr(confidence, "value", confidence), "references": [r.id for r in refs],
                  "judge_provider": getattr(ev, "judge_provider", None), "judge_model": getattr(ev, "judge_model", None)}
        return p.id, Prediction(score / 100, detail)

    def predict(self, posts: list[Post]) -> dict[str, Prediction]:
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return {pid: pred for pid, pred in pool.map(self._one, posts) if pred is not None}


class JudgeRankPredictor:
    """Dhilan's `rank_candidates`: one call ranks 2-6 posts from the same account against each other.

    Its scores are relative within a call, so `detail["call"]` names the call and the backtest only
    compares posts ranked together. Each chunk is ranked in a seeded shuffle and its reverse, averaged.
    """

    name = "post_judge_rank"
    relative = True

    def __init__(self, rank: Callable[[list[Post]], dict[str, float]], batch: int = 4, workers: int = 4,
                 seed: int = 0):
        if not 2 <= batch <= 6:
            raise ValueError("rank_candidates takes 2 to 6 candidates per call")
        self.rank, self.batch, self.workers, self.seed = with_retries(rank), batch, workers, seed
        self.failures: list[str] = []

    def _chunks(self, posts: list[Post]) -> list[list[Post]]:
        by_account: dict[str, list[Post]] = defaultdict(list)
        for p in posts:
            by_account[p.account].append(p)
        out = []
        for account, group in sorted(by_account.items()):
            group = list(group)
            random.Random(f"{self.seed}:{account}").shuffle(group)
            n_calls = -(-len(group) // self.batch)
            size = -(-len(group) // n_calls)  # even sizes, so no call is left with one post
            out += [group[i:i + size] for i in range(0, len(group), size)]
        return out

    def _chunk(self, chunk: list[Post]) -> dict[str, Prediction]:
        call = chunk[0].id
        if len(chunk) == 1:
            return {call: Prediction(50.0, {"call": call, "unranked": True})}
        sums: dict[str, list[float]] = defaultdict(list)
        try:
            for order in orders(chunk, f"{self.seed}:{call}"):
                scores = self.rank(order)
                if set(scores) != {p.id for p in chunk}:
                    raise ValueError(f"rank_candidates scored {sorted(scores)}; expected {[p.id for p in chunk]}")
                for pid, s in scores.items():
                    sums[pid].append(float(s))
        except Exception as e:  # noqa: BLE001 - recorded and reported; the posts drop out of every predictor
            print(f"[backtest] post_judge_rank gave up on {[p.id for p in chunk]}: {str(e)[:160]}")
            self.failures += [p.id for p in chunk]
            return {}
        return {pid: Prediction(sum(v) / len(v), {"call": call}) for pid, v in sums.items()}

    def predict(self, posts: list[Post]) -> dict[str, Prediction]:
        out: dict[str, Prediction] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for part in pool.map(self._chunk, self._chunks(posts)):
                out.update(part)
        return out


MEMORY_MODES = ("on", "shuffled")


def apply_memory(refs: list, mode: str, seed: str) -> list:
    """`on` passes the references as selected. `shuffled` keeps the same posts (the same retrieval budget)
    but permutes their outcome labels, so any gain that survives shuffling isn't coming from memory.
    The judge requires references, so there is no `off` mode until it can run without them."""
    if mode == "on":
        return refs
    if mode != "shuffled":
        raise ValueError(f"memory must be one of {MEMORY_MODES}")
    labels = [(r.performance, r.category, r.engagement) for r in refs]
    random.Random(seed).shuffle(labels)
    return [r.model_copy(update={"performance": pf, "category": c, "engagement": e})
            for r, (pf, c, e) in zip(refs, labels)]


def _post_judge_api():
    try:
        from baml_sdk import CandidatePost, RankingCandidate
        from harness.post_judge.judge import DEFAULT_DATASET, grade_with_references, judge_settings
        from harness.post_judge.ranking import rank_candidates
        from harness.post_judge.references import load_references, select_references
    except ImportError as e:
        raise SystemExit(
            f"post_judge isn't importable ({e}). It lands with PR #5 (dhilan/version-1); after merging, "
            "generate baml_sdk per eval/README.md and set HARNESS_JUDGE_PROVIDER / HARNESS_JUDGE_MODEL."
        ) from e
    return SimpleNamespace(CandidatePost=CandidatePost, RankingCandidate=RankingCandidate, dataset=DEFAULT_DATASET,
                           grade_with_references=grade_with_references, judge_settings=judge_settings,
                           rank_candidates=rank_candidates, load_references=load_references,
                           select_references=select_references)


def from_post_judge(per_class: int = 3, memory: str = "on") -> PostJudgePredictor:
    """Wire Dhilan's judge with references dated strictly before each candidate (a rolling origin)."""
    api = _post_judge_api()
    pool = api.load_references(api.dataset)
    sha = hashlib.sha256(api.dataset.read_bytes()).hexdigest()

    def grade(p: Post):
        candidate = api.CandidatePost(text=p.clean_text, mission=MISSION, context="", lane="post")
        refs = api.select_references(pool, p.clean_text, per_class=per_class, before=p.date, exclude_ids={p.id})
        return api.grade_with_references(candidate, apply_memory(refs, memory, p.id), dataset_sha256=sha)

    pred = PostJudgePredictor(grade)
    pred.model_info = dict(zip(("provider", "model"), api.judge_settings()))
    return pred


def from_post_judge_rank(per_class: int = 3, memory: str = "on", seed: int = 0) -> JudgeRankPredictor:
    """Wire rank_candidates; references predate the oldest post in the chunk."""
    api = _post_judge_api()
    pool = api.load_references(api.dataset)
    sha = hashlib.sha256(api.dataset.read_bytes()).hexdigest()

    def rank(chunk: list[Post]) -> dict[str, float]:
        oldest = min(p.date for p in chunk)
        candidates = [api.RankingCandidate(id=p.id, post=api.CandidatePost(text=p.clean_text, mission=MISSION,
                                                                            context="", lane="post"))
                      for p in chunk]
        refs = api.select_references(pool, " ".join(p.clean_text for p in chunk), per_class=per_class,
                                     before=oldest, exclude_ids={p.id for p in chunk})
        leaked = [r.id for r in refs if str(r.date) >= oldest.isoformat()]
        if leaked:
            raise ValueError(f"rank_candidates got references from the chunk's day or later: {leaked}")
        ev = api.rank_candidates(candidates, apply_memory(refs, memory, chunk[0].id), dataset_sha256=sha)
        return {item.candidate_id: item.score for item in ev.ranking.ranked}

    pred = JudgeRankPredictor(rank, seed=seed)
    pred.model_info = dict(zip(("provider", "model"), api.judge_settings()))
    return pred


def llm_for(provider: str, model: str) -> LLM:
    """One LLM per model, so the same run can score with two model families through the gateway."""
    from harness.llm import AnthropicLLM, MockLLM, get_llm

    if provider == "mock":
        return MockLLM()
    if provider == "anthropic":
        return AnthropicLLM(model)
    return get_llm(provider)


def build(names: list[str], provider: str, models: list[str], panel_path: Path, pool: list[Post], seed: int,
          memory: str = "on", batch: int = 1) -> list[Predictor]:
    """`pool` is every eligible post: the account and TF-IDF baselines learn from it, cross-fitted."""
    if memory not in MEMORY_MODES:
        raise SystemExit(f"--memory must be one of {MEMORY_MODES}")
    fixed: dict[str, Callable[[], Predictor]] = {
        "random": lambda: RandomPredictor(seed),
        "account": lambda: AccountPredictor(pool),
        "media": MediaPredictor,
        "length": LengthPredictor,
        "tfidf": lambda: TfidfPredictor(pool, seed=seed),
        "post_judge": lambda: from_post_judge(memory=memory),
        "post_judge_rank": lambda: from_post_judge_rank(memory=memory, seed=seed),
    }
    per_model = {"panel": lambda: load_panel(panel_path), "reader": lambda: READER_PANEL}
    unknown = set(names) - set(fixed) - set(per_model)
    if unknown:
        raise SystemExit(f"unknown predictors {sorted(unknown)}; choose from {sorted(fixed) + sorted(per_model)}")
    out: list[Predictor] = []
    for n in names:
        if n in fixed:
            out.append(fixed[n]())
            continue
        for model in models:
            label = n if len(models) == 1 else f"{n}@{model}"
            pred = PanelPredictor(llm_for(provider, model), per_model[n](), name=label, batch=batch, seed=seed)
            pred.model_info = {"provider": provider, "model": model}
            out.append(pred)
    return out
