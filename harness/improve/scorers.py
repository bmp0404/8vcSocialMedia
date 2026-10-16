"""How each instrument is scored against the outcomes in harness/improve/outcomes.py.

Every scorer returns {source: Score} for "sgu" and "threads". A source the instrument can't be scored on
says why in an empty Score rather than disappearing. Both sources use only outcomes at or after the
instrument's cutoff: threads rooted then, `_sgu_` replies posted then.

- Rankers (reply ranker, TF-IDF ridge, label model, panel) score both items of a pair; credit goes to
  the one with more likes or engagement. On `_sgu_` replies the ranker's score is correlated with the
  reply's rank percentile.
- Screens (slop gate, the two vetoes) pass or fail each real reply. Only discordant pairs count, where
  one reply passes and the other fails: did the passing reply get more likes? The pass rate on real
  replies is reported beside it, because a screen that fails real human replies costs the lane good drafts.
- The lineup detector names one of five settled real replies as AI-written. Each pair of the named reply
  with another reply in the lineup counts: did the other one get more likes? Ties in likes are skipped.

LLM verdicts are cached per instrument version in out/improve/cache/, keyed by status or thread ID, so
no item is ever judged twice by one version. Only answers are cached. When the cycle's call budget runs
out, or a call fails, the item waits for the next cycle and is counted as `deferred`, never as an
abstention. An answer the instrument gave that names nothing usable is an abstention: it scores as the
instrument's worst rank, and coverage beside the number says how often that happened.
"""

import hashlib
import inspect
import json
import random
import threading
from collections import defaultdict
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from harness import config
from harness.corpus.bench import BENCH_DIR, Pair, reply_body
from harness.corpus.stats import accuracy
from harness.improve.instruments import Score, empty, ids_digest
from harness.improve.outcomes import Evidence, pair_credits, root_time, spearman, spearman_interval
from harness.iterate.llm import BudgetExceeded, Meter

THREADS, SGU = "threads", "sgu"
PAIRWISE, SPEARMAN = "pairwise_accuracy", "spearman"
NO_SGU_POSTS = "_sgu_ replies by hand and posts no originals yet, so there is nothing for a post ranker to rank"
LINEUP_SIZE = 5
MAX_BATCH = 12  # replies one lane-veto call judges together, and other replies shown as a thread's context
RESULTS = BENCH_DIR / "results"


def fingerprint(*parts) -> str:
    return hashlib.sha256(json.dumps([str(p) for p in parts]).encode()).hexdigest()[:12]


@dataclass
class Ctx:
    llm_for: Callable[[str, str], object]  # (model, instrument name) -> an LLM charged to that instrument
    meter: Meter
    cache_dir: Path
    log: Callable[[str], None] = print


class Cache:
    """Append-only {key: value} file for one instrument version. Values hold verdicts, never text."""

    def __init__(self, path: Path):
        self.path, self.rows, self.lock = path, {}, threading.Lock()
        if path.is_file():
            with path.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        row = json.loads(line)
                        self.rows[row["key"]] = row["value"]

    def __contains__(self, key: str) -> bool:
        return key in self.rows

    def get(self, key: str):
        return self.rows.get(key)

    def put(self, key: str, value) -> None:
        with self.lock:
            self.rows[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, "value": value}) + "\n")


def _bench_result(path: Path, *keys) -> dict | None:
    """A number from a frozen benchmark results file, for an instrument's `fit_evidence`; None if absent."""
    if not path.is_file():
        return None
    node = json.loads(path.read_text(encoding="utf-8"))
    for k in keys:
        node = node.get(k) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def _pairwise(pairs: list[Pair], scores: dict[str, float | None], clusters_of: Callable[[Pair], str],
              detail: dict, coverage: float | None = None) -> Score:
    if not pairs:
        none = empty(THREADS, PAIRWISE, 0.5, detail.get("note", "no pairs after the cutoff"))
        return replace(none, detail={**detail, **none.detail})
    credits = pair_credits(pairs, scores)
    groups = [clusters_of(p) for p in pairs]
    iv = accuracy(credits, groups)
    if coverage is None:
        items = {s for p in pairs for s in (p.winner, p.loser)}
        coverage = sum(scores.get(s) is not None for s in items) / len(items)
    return Score(THREADS, PAIRWISE, 0.5, iv.n_clusters, len(pairs), iv.estimate, (iv.low, iv.high), coverage,
                 ids_digest(p.pair_id for p in pairs), detail)


def _spearman(values: list[tuple[str, float | None, float]], detail: dict) -> Score:
    """values: (outcome id, instrument score or None for an abstention, outcome percentile)."""
    if not values:
        return empty(SGU, SPEARMAN, 0.0, detail.get("note", "no ranked _sgu_ replies after the cutoff yet"))
    scored = [v for v in values if v[1] is not None]
    worst = min((v[1] for v in scored), default=0.0) - 1.0
    xs = [v[1] if v[1] is not None else worst for v in values]
    ys = [v[2] for v in values]
    return Score(SGU, SPEARMAN, 0.0, len(values), len(values), spearman(xs, ys), spearman_interval(xs, ys),
                 len(scored) / len(values), ids_digest(v[0] for v in values), detail)


def _screen_threads(pairs_with_origin, verdicts: dict[str, bool | None], deferred: int) -> Score:
    """`verdicts`: True passed, False failed, None abstained (counted as failed, its worst outcome)."""
    pairs = [p for p, _ in pairs_with_origin]
    judged = {s for p in pairs for s in (p.winner, p.loser) if s in verdicts}
    passed = {s: bool(verdicts[s]) for s in judged}
    rate = sum(passed.values()) / len(judged) if judged else None
    disc = [p for p in pairs if p.winner in judged and p.loser in judged and passed[p.winner] != passed[p.loser]]
    detail = {"pass_rate_on_real_replies": None if rate is None else round(rate, 4), "replies_judged": len(judged),
              "discordant_pairs": len(disc), "deferred": deferred, "origins": _origins(pairs_with_origin, disc)}
    if not disc:
        detail["note"] = "no pair where one reply passed and the other failed"
    coverage = sum(verdicts[s] is not None for s in judged) / len(judged) if judged else None
    return _pairwise(disc, {s: float(v) for s, v in passed.items()}, lambda p: p.group, detail, coverage)


def _origins(pairs_with_origin, kept: list[Pair]) -> dict:
    ids = {p.pair_id for p in kept}
    out = defaultdict(set)
    for p, origin in pairs_with_origin:
        if p.pair_id in ids:
            out[origin].add(p.group)
    return {k: len(v) for k, v in sorted(out.items())}


def _thread_replies(ev: Evidence, root_id: str) -> list:
    """Every direct reply with words to a watched or corpus root, oldest first: what a writer would see."""
    corpus = ev.watch if ev.watch is not None and ev.watch.get(root_id) is not None else ev.frozen
    return sorted((it for it in corpus.items if it.thread_root == root_id and it.parent_id == root_id
                   and reply_body(it.text)), key=lambda it: it.created_at)


def _others(ev: Evidence, root_id: str, exclude: set[str]) -> list[str]:
    return [r.text for r in _thread_replies(ev, root_id) if r.status_id not in exclude][:MAX_BATCH]


# ---------------------------------------------------------------- fitted rankers


class ReplyRanker:
    name, role = "reply_ranker", "gate"
    what = ("pairwise logistic on a reply's timing, reach and text (harness/rank/reply.py), trained on the whole "
            "frozen reply benchmark")
    blocks = ("timing", "text")

    def __init__(self):
        self._model = None

    @staticmethod
    def fit_before(frozen_pairs: list[Pair]) -> datetime:
        """One second after the frozen benchmark's newest root, so the fit uses every frozen thread."""
        return max(root_time(p) for p in frozen_pairs) + timedelta(seconds=1)

    def version(self) -> str:
        from harness.rank import pairwise, reply

        manifest = BENCH_DIR / "manifest.json"
        return fingerprint(self.blocks, hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else "",
                           inspect.getsource(reply.timing_features), inspect.getsource(reply.text_features),
                           inspect.getsource(pairwise.fit))

    def cutoff(self, ev: Evidence, first_seen: datetime) -> tuple[datetime, str]:
        before = self.fit_before(ev.frozen_pairs)
        return before, f"trained on every frozen benchmark thread, roots up to {(before - timedelta(seconds=1)).isoformat()}"

    def fit_evidence(self) -> dict | None:
        full = _bench_result(RESULTS / "replies.json", "ranker_pooled_test", "accuracy", "full")
        if full is None:
            return None
        return {"source": "data/bench/results/replies.json, ranker_pooled_test (fitted per cutoff, not this fit)",
                "test_accuracy": full.get("estimate"), "ci95": full.get("ci95"),
                "note": "the test split was seen once while choosing the feature blocks, so it never decides status"}

    def model(self, ev: Evidence):
        if self._model is None:
            from harness.rank import reply

            self._model = reply.fit(self.fit_before(ev.frozen_pairs), self.blocks, ev.frozen, pairs=ev.frozen_pairs)
        return self._model

    def _input(self, ev: Evidence, sid: str, root):
        from harness.rank.reply import ReplyInput

        it = ev.item(sid)
        return ReplyInput(root.text, it.text, (it.created_at - root.created_at).total_seconds() / 60,
                          it.author_followers, bool(it.author_verified), it.has_media, it.n_links)

    def score(self, ev: Evidence, cutoff: datetime, ctx: Ctx) -> dict[str, Score]:
        pairs_o = ev.reply_pairs(cutoff)
        out = {}
        if pairs_o:
            model = self.model(ev)
            scores = {}
            for p, _ in pairs_o:
                root = ev.item(p.group)
                for sid in (p.winner, p.loser):
                    if sid not in scores:
                        scores[sid] = model.score_input(self._input(ev, sid, root))
            pairs = [p for p, _ in pairs_o]
            out[THREADS] = _pairwise(pairs, scores, lambda p: p.group, {"origins": _origins(pairs_o, pairs)})
        else:
            out[THREADS] = empty(THREADS, PAIRWISE, 0.5, "no reply pairs rooted after the cutoff")
        sgu = ev.sgu_after(cutoff)
        values = []
        if sgu:
            model = self.model(ev)
            values = [(r.action_id, model.score(r.parent_text, r.text, r.minutes_after or 0.0, r.followers),
                       r.percentile) for r in sgu]
        out[SGU] = _spearman(values, {})
        return out


class TfidfPostRanker:
    name, role = "tfidf_post_ranker", "gate"
    what = "TF-IDF ridge on a post's words, target engagement within the account (harness/rank/post.py)"
    profiles = ("devtools",)  # fitted on dev-tool accounts; its words mean nothing for another domain
    alpha = 10.0

    def __init__(self):
        self._model = None

    def _targets(self, ev: Evidence):
        from harness.rank.post import within_account_targets

        return within_account_targets(ev.frozen)

    def version(self) -> str:
        from harness.rank import post

        return fingerprint(self.alpha, inspect.getsource(post.ridge_fit), inspect.getsource(post._row),
                           inspect.getsource(post.within_account_targets))

    def cutoff(self, ev: Evidence, first_seen: datetime) -> tuple[datetime, str]:
        newest = max(ev.frozen.get(s).created_at for s in self._targets(ev))
        return newest + timedelta(seconds=1), f"fitted on every dev-tool account's posts up to {newest.isoformat()}"

    def fit_evidence(self) -> dict | None:
        r = _bench_result(RESULTS / "judges-posts.json", "accuracy", "tfidf_ridge")
        return r and {"source": "data/bench/results/judges-posts.json (cross-fitted by account)",
                      "test_accuracy": r.get("estimate"), "ci95": r.get("ci95")}

    def score(self, ev: Evidence, cutoff: datetime, ctx: Ctx) -> dict[str, Score]:
        from harness.rank.post import clean_text, ridge_fit

        pairs = ev.posts_after(cutoff, self.profiles)
        out = {SGU: empty(SGU, SPEARMAN, 0.0, NO_SGU_POSTS)}
        if not pairs:
            out[THREADS] = empty(THREADS, PAIRWISE, 0.5, "no within-account post pairs after the cutoff")
            return out
        if self._model is None:
            targets = self._targets(ev)
            ids = sorted(targets)
            self._model = ridge_fit([clean_text(ev.frozen.get(s).text) for s in ids], [targets[s][1] for s in ids],
                                    self.alpha)
        scores = {s: self._model.predict(clean_text(ev.item(s).text)) for p in pairs for s in (p.winner, p.loser)}
        out[THREADS] = _pairwise(pairs, scores, lambda p: p.group, {})
        return out


class LabelModel:
    name, role = "label_model", "gate"
    what = "logistic on Claude Sonnet 5.5's labels of a post (harness/label), fitted on dev-author post pairs"
    profiles = ("devtools",)
    model_id = "claude-sonnet-5-5"

    def __init__(self):
        self._fit = None

    def _pairs(self):
        from harness.corpus.bench import load_pairs

        return load_pairs("posts")

    def version(self) -> str:
        from harness.label.prompt import prompt_sha

        return fingerprint(self.model_id, prompt_sha())

    def cutoff(self, ev: Evidence, first_seen: datetime) -> tuple[datetime, str]:
        dev = [p for p in self._pairs() if p.split == "dev"]
        newest = max(ev.frozen.get(s).created_at for p in dev for s in (p.winner, p.loser))
        return newest + timedelta(seconds=1), f"fitted on dev-author post pairs with posts up to {newest.isoformat()}"

    def fit_evidence(self) -> dict | None:
        r = _bench_result(RESULTS / "judges-posts.json", "accuracy", "claude_label_model")
        return r and {"source": "data/bench/results/judges-posts.json (test authors)",
                      "test_accuracy": r.get("estimate"), "ci95": r.get("ci95")}

    def fit(self):
        """(weights, z-scorer, schema) of a pairwise logistic on Claude's label features, z-scored on dev items."""
        if self._fit is None:
            from harness.label.features import reply_label_votes
            from harness.rank import pairwise
            from harness.rank.post import label_features_for

            votes, schema = reply_label_votes((self.model_id,))
            pairs = self._pairs()
            feats = label_features_for(pairs, votes, schema)
            dev = [p for p in pairs if p.split == "dev" and p.winner in feats and p.loser in feats]
            if not dev:
                raise ValueError("no labeled dev post pairs; rebuild data/labels with `python -m harness.corpus rebuild`")
            keys = sorted({k for f in feats.values() for k in f})
            vals = {k: [feats[s].get(k, 0.0) for p in dev for s in (p.winner, p.loser)] for k in keys}
            mean = {k: sum(v) / len(v) for k, v in vals.items()}
            std = {k: ((sum((x - mean[k]) ** 2 for x in v) / len(v)) ** 0.5 or 1.0) for k, v in vals.items()}
            z = lambda f: {k: (f.get(k, 0.0) - mean[k]) / std[k] for k in keys}  # noqa: E731
            diffs = [pairwise.diff(z(feats[p.winner]), z(feats[p.loser])) for p in dev]
            l2, _ = pairwise.choose_l2(diffs, [p.group for p in dev])
            self._fit = (pairwise.fit(diffs, l2), z, schema)
        return self._fit

    def _label(self, ev: Evidence, sid: str, ctx: Ctx) -> dict | None:
        """Claude's labels for one post, or None when it answered with no valid labels. API errors raise."""
        from harness.label.items import item_row
        from harness.label.prompt import system_prompt, user_message
        from harness.label.schema import validate_labels
        from harness.llm import parse_json

        corpus = ev.watch if ev.watch is not None and ev.watch.get(sid) else ev.frozen
        llm = ctx.llm_for(self.model_id, self.name)
        message = user_message(item_row(corpus.get(sid), corpus), None)
        if getattr(llm, "is_mock", False):
            text = json.dumps(llm.complete_json(system_prompt(), message, mock=lambda: {"labels": {}}))
        else:
            text = llm._create(max_tokens=2000, system=system_prompt(), messages=[{"role": "user", "content": message}],
                               extra_body={"output_config": {"effort": "low"}})
        try:
            labels = (parse_json(text) or {}).get("labels")
        except ValueError:
            return None
        if not isinstance(labels, dict) or validate_labels(labels, "post"):
            return None
        return labels

    def score(self, ev: Evidence, cutoff: datetime, ctx: Ctx) -> dict[str, Score]:
        from harness.rank import pairwise
        from harness.rank.reply import label_features

        pairs = ev.posts_after(cutoff, self.profiles)
        out = {SGU: empty(SGU, SPEARMAN, 0.0, NO_SGU_POSTS)}
        if not pairs:
            out[THREADS] = empty(THREADS, PAIRWISE, 0.5, "no within-account post pairs after the cutoff")
            return out
        cache = Cache(ctx.cache_dir / f"{self.name}-{self.version()}.jsonl")
        deferred = 0
        for sid in sorted({s for p in pairs for s in (p.winner, p.loser)}):
            if sid in cache:
                continue
            try:
                cache.put(sid, self._label(ev, sid, ctx))
            except BudgetExceeded:
                deferred += 1
            except Exception as e:  # noqa: BLE001 - a failed call waits for the next cycle
                ctx.log(f"[instruments] {self.name}: labeling {sid} failed, retried next cycle: {type(e).__name__}: {e}")
                deferred += 1
        w, z, schema = self.fit()
        scores = {s: (None if cache.get(s) is None else pairwise.dot(w, z(label_features(
            {k: [v] for k, v in cache.get(s).items()}, schema)))) for s in cache.rows}
        done = [p for p in pairs if p.winner in cache and p.loser in cache]
        out[THREADS] = _pairwise(done, scores, lambda p: p.group, {"deferred_posts": deferred})
        return out


# ---------------------------------------------------------------- LLM and rule instruments


class LineupDetector:
    name, role = "lineup_detector", "gate"
    what = "Opus 5.5 names the AI-written reply among five, the action lineup's primary (harness/iterate/fitness/action.py)"

    def version(self) -> str:
        from harness.iterate.fitness.action import DETECTOR_MODEL, LINEUP_SYSTEM

        return fingerprint(DETECTOR_MODEL, LINEUP_SYSTEM)

    def cutoff(self, ev: Evidence, first_seen: datetime) -> tuple[datetime, str]:
        return first_seen, "a prompt: scored only on outcomes after the ledger first saw this version"

    def _pick(self, root, shown: list[str], ctx: Ctx) -> int | None:
        """The 1-based reply the detector names, or None when its answer names none. API errors raise."""
        from harness.iterate.fitness.action import DETECTOR_MODEL, LINEUP_SYSTEM, clean

        user = f"POST:\n@{root.author} posted:\n{clean(root.text)}\n\nREPLIES:\n" + "\n".join(
            f"{i + 1}. {clean(t)}" for i, t in enumerate(shown))
        out = ctx.llm_for(DETECTOR_MODEL, self.name).complete_json(
            LINEUP_SYSTEM, user, mock=lambda: {"ai_reply": 1 + int(hashlib.sha256(user.encode()).hexdigest(), 16) % 5})
        try:
            pick = int(out["ai_reply"])
        except (KeyError, TypeError, ValueError):
            return None
        return pick if 1 <= pick <= len(shown) else None

    def score(self, ev: Evidence, cutoff: datetime, ctx: Ctx) -> dict[str, Score]:
        cache = Cache(ctx.cache_dir / f"{self.name}-{self.version()}.jsonl")
        roots = sorted({p.group for p, _ in ev.reply_pairs(cutoff)})
        deferred, pairs, scores, attempted, named = 0, [], {}, 0, 0
        for root_id in roots:
            root = ev.item(root_id)
            replies = [r for r in ev.settled_replies(root_id) if len(reply_body(r.text).split()) >= 3]
            if len(replies) < LINEUP_SIZE:
                continue
            shown = random.Random(f"lineup:{root_id}").sample(replies, LINEUP_SIZE)
            key = f"thread:{root_id}"
            if key not in cache:
                try:
                    pick = self._pick(root, [r.text for r in shown], ctx)
                except BudgetExceeded:
                    deferred += 1
                    continue
                except Exception as e:  # noqa: BLE001 - a failed call waits for the next cycle
                    ctx.log(f"[instruments] {self.name}: {root_id} failed, retried next cycle: {type(e).__name__}: {e}")
                    deferred += 1
                    continue
                cache.put(key, {"pick": pick, "shown": [r.status_id for r in shown]})
            row = cache.get(key)
            attempted += 1
            if row["pick"] is None:
                # An abstention scores as the worst pick: the most-liked reply named as the AI one.
                pick_id = max(row["shown"], key=lambda s: (ev.item(s).metrics["likes"], s))
            else:
                named += 1
                pick_id = row["shown"][row["pick"] - 1]
            n = ev.item(pick_id)
            for other in row["shown"]:
                o = ev.item(other)
                if other == pick_id or o.metrics["likes"] == n.metrics["likes"]:
                    continue
                w, l = (other, pick_id) if o.metrics["likes"] > n.metrics["likes"] else (pick_id, other)
                pairs.append(Pair(f"{root_id}:{w}:{l}", "lineup", root_id, w, l, ev.item(w).metrics["likes"],
                                  ev.item(l).metrics["likes"], "rolling"))
                scores[other], scores[pick_id] = 1.0, 0.0
        detail = {"lineups": attempted, "lineups_named": named, "deferred": deferred}
        coverage = named / attempted if attempted else None
        return {THREADS: _pairwise(pairs, scores, lambda p: p.group, detail, coverage), SGU: self._sgu(ev, cutoff, ctx, cache)}

    def _sgu(self, ev: Evidence, cutoff: datetime, ctx: Ctx, cache: Cache) -> Score:
        values, waiting = [], 0
        for r in ev.sgu_after(cutoff):
            key = f"sgu:{r.action_id}"
            if key not in cache:
                others = [x for x in _thread_replies(ev, r.parent_status) if x.status_id != r.status_id]
                if len(others) < LINEUP_SIZE - 1 or ev.item(r.parent_status) is None:
                    waiting += 1  # the watcher hasn't read the parent's thread yet; try again next cycle
                    continue
                rng = random.Random(f"lineup:{r.action_id}")
                shown = [x.text for x in rng.sample(others, LINEUP_SIZE - 1)]
                at = rng.randrange(LINEUP_SIZE)
                shown.insert(at, r.text)
                try:
                    pick = self._pick(ev.item(r.parent_status), shown, ctx)
                except Exception as e:  # noqa: BLE001 - BudgetExceeded included: the reply waits for the next cycle
                    ctx.log(f"[instruments] {self.name}: _sgu_ {r.action_id} waits: {type(e).__name__}: {e}")
                    waiting += 1
                    continue
                cache.put(key, {"human": None if pick is None else float(pick != at + 1)})
            values.append((r.action_id, cache.get(key)["human"], r.percentile))
        return _spearman(values, {"score": "1 when the detector named another reply, 0 when it named ours",
                                  "waiting": waiting})


class ActionVeto:
    name, role = "action_veto", "gate"
    what = "the action lineup's veto guardrail, Opus 5.5 on one draft (harness/iterate/fitness/action.py)"

    def version(self) -> str:
        from harness.iterate.fitness.action import VETO_MODEL, VETO_SYSTEM

        return fingerprint(VETO_MODEL, VETO_SYSTEM)

    def cutoff(self, ev: Evidence, first_seen: datetime) -> tuple[datetime, str]:
        return first_seen, "a prompt: scored only on outcomes after the ledger first saw this version"

    def _judge(self, ctx: Ctx, profile: str, root, text: str, others: list[str]) -> bool | None:
        """True passes, False vetoed, None when the answer says neither. API errors raise."""
        from harness.iterate.fitness.action import VETO_MODEL, VETO_SYSTEM, clean

        with config.use_profile(profile):
            mission = config.mission()
        user = (f"MISSION:\n{mission}\n\nPOST:\n@{root.author} posted:\n{clean(root.text)}\n\nOTHER REPLIES:\n"
                + "\n".join(f"- {clean(t)}" for t in others) + f"\n\nDRAFT REPLY:\n{clean(text)}")
        verdict = ctx.llm_for(VETO_MODEL, self.name).complete_json(VETO_SYSTEM, user,
                                                                   mock=lambda: {"veto": False, "reasons": []})
        vetoed = verdict.get("veto") if isinstance(verdict, dict) else None
        return (not vetoed) if isinstance(vetoed, bool) else None

    def score(self, ev: Evidence, cutoff: datetime, ctx: Ctx) -> dict[str, Score]:
        cache = Cache(ctx.cache_dir / f"{self.name}-{self.version()}.jsonl")
        pairs_o = ev.reply_pairs(cutoff)
        deferred = 0
        for sid in sorted({s for p, _ in pairs_o for s in (p.winner, p.loser)}):
            if sid in cache:
                continue
            root_id = ev.item(sid).thread_root
            try:
                cache.put(sid, self._judge(ctx, ev.profile(root_id), ev.item(root_id), ev.item(sid).text,
                                           _others(ev, root_id, {sid})))
            except Exception as e:  # noqa: BLE001 - BudgetExceeded included: the reply waits for the next cycle
                if not isinstance(e, BudgetExceeded):
                    ctx.log(f"[instruments] {self.name}: {sid} waits: {type(e).__name__}: {e}")
                deferred += 1
        verdicts = {s: v for s, v in cache.rows.items() if not s.startswith("sgu:")}
        values, waiting = [], 0
        for r in ev.sgu_after(cutoff):
            key = f"sgu:{r.action_id}"
            if key not in cache:
                parent = ev.item(r.parent_status)
                if parent is None:
                    waiting += 1
                    continue
                try:
                    cache.put(key, self._judge(ctx, ev.profile(r.parent_status), parent, r.text,
                                               _others(ev, r.parent_status, {r.status_id})))
                except Exception:  # noqa: BLE001 - waits for the next cycle
                    waiting += 1
                    continue
            v = cache.get(key)
            values.append((r.action_id, None if v is None else float(v), r.percentile))
        return {THREADS: _screen_threads(pairs_o, verdicts, deferred), SGU: _spearman(values, {"waiting": waiting})}


@contextmanager
def _metered_routing(ctx: Ctx, name: str):
    """The lane veto builds its own AnthropicLLM through harness.minds.routing; charge those calls here."""
    from harness.minds import routing

    llm = ctx.llm_for("claude-opus-5-5", name)
    if getattr(llm, "is_mock", False):
        yield llm
        return
    from harness.iterate.llm import metered_anthropic_class

    saved = routing.AnthropicLLM
    routing.AnthropicLLM = metered_anthropic_class(ctx.meter, name)
    try:
        yield llm
    finally:
        routing.AnthropicLLM = saved


class LaneVeto:
    name, role = "lane_veto", "gate"
    what = ("the reply lane's production veto, Opus 5.5 in two orders (harness/minds/veto.py); no experiment reads "
            "it, so its status tells a person whether to keep trusting it")

    def version(self) -> str:
        from harness.minds import settings, veto

        cfg = settings.ActionConfig()
        return fingerprint(veto.VETO_SYSTEM, cfg.veto_model, cfg.veto_max_p_ai)

    def cutoff(self, ev: Evidence, first_seen: datetime) -> tuple[datetime, str]:
        return first_seen, "a prompt: scored only on outcomes after the ledger first saw this version"

    def _judge(self, llm, texts: list[str], context: str, profile: str) -> list[bool | None]:
        from harness.minds import settings, veto

        with config.use_profile(profile):
            results = veto.veto(llm, settings.ActionConfig(), texts, lane="reply", context=context,
                                mission=config.mission())
        by_text = {r.text: r for r in results}
        # A batch the judge answered in the wrong shape comes back with no verdicts: an abstention.
        return [None if t not in by_text or (by_text[t].fwd is None and by_text[t].rev is None)
                else by_text[t].passed for t in texts]

    def score(self, ev: Evidence, cutoff: datetime, ctx: Ctx) -> dict[str, Score]:
        cache = Cache(ctx.cache_dir / f"{self.name}-{self.version()}.jsonl")
        pairs_o = ev.reply_pairs(cutoff)
        by_root = defaultdict(set)
        for p, _ in pairs_o:
            by_root[p.group] |= {p.winner, p.loser}
        values, waiting = [], 0
        with _metered_routing(ctx, self.name) as llm:
            for root_id, sids in sorted(by_root.items()):
                todo = sorted(s for s in sids if s not in cache)[:MAX_BATCH]
                if not todo:
                    continue
                root = ev.item(root_id)
                context = (f"POST by @{root.author}:\n{root.text}\n\nTHREAD:\n"
                           + "\n".join(f"- {t}" for t in _others(ev, root_id, set(todo))))
                try:
                    verdicts = self._judge(llm, [reply_body(ev.item(s).text) for s in todo], context, ev.profile(root_id))
                except Exception as e:  # noqa: BLE001 - BudgetExceeded included: the batch waits for the next cycle
                    if not isinstance(e, BudgetExceeded):
                        ctx.log(f"[instruments] {self.name}: thread {root_id} waits: {type(e).__name__}: {e}")
                    continue
                for s, v in zip(todo, verdicts):
                    cache.put(s, v)
            for r in ev.sgu_after(cutoff):
                key = f"sgu:{r.action_id}"
                if key not in cache:
                    try:
                        (v,) = self._judge(llm, [r.text], f"POST:\n{r.parent_text}", ev.profile(r.parent_status))
                    except Exception:  # noqa: BLE001 - waits for the next cycle
                        waiting += 1
                        continue
                    cache.put(key, v)
                v = cache.get(key)
                values.append((r.action_id, None if v is None else float(v), r.percentile))
        verdicts = {s: v for s, v in cache.rows.items() if not s.startswith("sgu:")}
        # Replies still unjudged: their batch failed or ran out of budget, or past MAX_BATCH in one thread.
        remaining = sum(len(sids - set(cache.rows)) for sids in by_root.values())
        return {THREADS: _screen_threads(pairs_o, verdicts, remaining), SGU: _spearman(values, {"waiting": waiting})}


class SlopGate:
    name, role = "slop_gate", "gate"
    what = "the deterministic slop and grounding gate (harness/minds/gate.py)"

    def version(self) -> str:
        from harness.minds import gate, settings

        return fingerprint(inspect.getsource(gate), settings.GateLimits())

    def cutoff(self, ev: Evidence, first_seen: datetime) -> tuple[datetime, str]:
        return first_seen, "rules tuned by hand: scored only on outcomes after the ledger first saw this version"

    @staticmethod
    def check(text: str, post: str, others: list[str]) -> bool:
        from harness.minds import gate

        context = f"POST:\n{post}\n\nTHREAD:\n" + "\n".join(f"- {t}" for t in others)
        return gate.check(reply_body(text), "reply", context=context, post=post).passed

    def score(self, ev: Evidence, cutoff: datetime, ctx: Ctx) -> dict[str, Score]:
        pairs_o = ev.reply_pairs(cutoff)
        passed, threads = {}, {}
        for p, _ in pairs_o:
            root = ev.item(p.group)
            if p.group not in threads:
                threads[p.group] = _thread_replies(ev, p.group)
            for sid in (p.winner, p.loser):
                if sid not in passed:
                    others = [r.text for r in threads[p.group] if r.status_id != sid][:MAX_BATCH]
                    passed[sid] = self.check(ev.item(sid).text, root.text, others)
        values = [(r.action_id, float(self.check(r.text, r.parent_text, [])), r.percentile)
                  for r in ev.sgu_after(cutoff)]
        return {THREADS: _screen_threads(pairs_o, passed, 0), SGU: _spearman(values, {})}


class Panel:
    name, role = "panel", "report"
    what = "the audience panel's mean engagement guess for a post (harness/qualify.py)"
    model_id = "claude-sonnet-5-5"

    def version(self) -> str:
        from harness.qualify import PANEL_PROMPT, PANEL_SYSTEM

        panels = [config.profile_file("panel.json", p).read_text(encoding="utf-8") for p in ("devtools", "wnba")]
        return fingerprint(self.model_id, PANEL_SYSTEM, PANEL_PROMPT, *panels)

    def cutoff(self, ev: Evidence, first_seen: datetime) -> tuple[datetime, str]:
        return first_seen, "a prompt: scored only on posts after the ledger first saw this version"

    def fit_evidence(self) -> dict | None:
        r = _bench_result(RESULTS / "judges-posts.json", "accuracy", "panel")
        return r and {"source": "data/bench/results/judges-posts.json (test authors)",
                      "test_accuracy": r.get("estimate"), "ci95": r.get("ci95")}

    def score(self, ev: Evidence, cutoff: datetime, ctx: Ctx) -> dict[str, Score]:
        from harness.models import Action
        from harness.qualify import load_panel, panel_mean, panel_scores

        pairs = ev.posts_after(cutoff)
        out = {SGU: empty(SGU, SPEARMAN, 0.0, NO_SGU_POSTS)}
        if not pairs:
            out[THREADS] = empty(THREADS, PAIRWISE, 0.5, "no within-account post pairs after the cutoff")
            return out
        cache = Cache(ctx.cache_dir / f"{self.name}-{self.version()}.jsonl")
        llm, deferred = ctx.llm_for(self.model_id, self.name), 0
        for sid in sorted({s for p in pairs for s in (p.winner, p.loser)}):
            if sid in cache:
                continue
            with config.use_profile(ev.profile(sid)):
                panel = load_panel()
                action = Action(type="post", text=ev.item(sid).text, mind="instrument", narrative_id="", id="p0")
                try:
                    s = panel_scores(llm, [action], panel).get("p0", {})
                except Exception as e:  # noqa: BLE001 - BudgetExceeded included: the post waits for the next cycle
                    if not isinstance(e, BudgetExceeded):
                        ctx.log(f"[instruments] panel: {sid} waits: {type(e).__name__}: {e}")
                    deferred += 1
                    continue
            # An answer without every panelist is an abstention.
            cache.put(sid, panel_mean({k: float(v) for k, v in s.items() if k in panel}) if set(s) >= set(panel) else None)
        done = [p for p in pairs if p.winner in cache and p.loser in cache]
        out[THREADS] = _pairwise(done, dict(cache.rows), lambda p: p.group, {"deferred_posts": deferred})
        return out


ALL = (ReplyRanker, TfidfPostRanker, LabelModel, LineupDetector, ActionVeto, LaneVeto, SlopGate, Panel)


def rescore(ev: Evidence, ctx: Ctx, ledger_path: Path, now: datetime,
            instruments: tuple[type, ...] = ALL) -> tuple[dict, list[dict], dict[str, str]]:
    """Score every instrument and update the ledger. Returns (ledger, events, {instrument: error}).
    An instrument that fails keeps its previous entry, and the error goes to the morning report."""
    from harness.improve import instruments as ledger_mod

    ledger = ledger_mod.load(ledger_path)
    events, errors = [], {}
    for cls in instruments:
        inst = cls()
        try:
            version = inst.version()
            cutoff, basis = inst.cutoff(ev, ledger_mod.first_seen(ledger, inst.name, version, now))
            scores = inst.score(ev, cutoff, ctx)
            fit_evidence = inst.fit_evidence() if hasattr(inst, "fit_evidence") else None
        except Exception as e:  # noqa: BLE001 - one broken instrument must not stop the others' scoring
            errors[inst.name] = f"{type(e).__name__}: {e}"
            ctx.log(f"[instruments] {inst.name} not scored: {errors[inst.name]}")
            continue
        events += ledger_mod.record(ledger, inst.name, role=inst.role, what=inst.what, version=version,
                                    cutoff=cutoff, cutoff_basis=basis, scores=scores, now=now,
                                    fit_evidence=fit_evidence)
    ledger_mod.save(ledger, ledger_path)
    return ledger, events, errors
