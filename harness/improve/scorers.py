"""How each instrument is scored against the outcomes in harness/improve/outcomes.py.

Every scorer returns {source: Score} for "sgu" and "threads". A source the instrument can't be scored on
says why in an empty Score rather than disappearing.

- Rankers (reply ranker, TF-IDF ridge, label model, panel) score both items of a pair; credit goes to
  the one with more likes or engagement. On `_sgu_` replies the ranker's score is correlated with the
  reply's rank percentile.
- Screens (slop gate, veto) pass or fail each real reply. Only discordant pairs count, where one reply
  passes and the other fails: did the passing reply get more likes? The pass rate on real replies is
  reported beside it, because a screen that fails real human replies is costing the lane good drafts.
- The lineup detector names one of five real replies as AI-written. Each pair of the named reply with
  another reply in the lineup counts: did the other one get more likes? Ties in likes are skipped.

LLM verdicts are cached per instrument version in out/improve/cache/, keyed by status or thread ID, so
no item is ever judged twice by one version. When the cycle's call budget runs out, the rest are left
for the next cycle (`deferred`), not counted as abstentions. An item the instrument was asked about and
gave no usable answer for is an abstention and scores as its worst rank, with coverage reported.
"""

import hashlib
import inspect
import json
import random
import threading
from collections import defaultdict
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from harness import config
from harness.corpus.bench import Pair, reply_body
from harness.corpus.stats import accuracy
from harness.improve.instruments import Score, empty, ids_digest
from harness.improve.outcomes import Evidence, pair_credits, spearman, spearman_interval
from harness.iterate.llm import BudgetExceeded, Meter

THREADS, SGU = "threads", "sgu"
PAIRWISE, SPEARMAN = "pairwise_accuracy", "spearman"
NO_SGU_POSTS = "_sgu_ replies by hand and posts no originals yet, so there is nothing for a post ranker to rank"
LINEUP_SIZE = 5
MAX_BATCH = 12  # replies one veto call judges together


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


def _pairwise(pairs: list[Pair], scores: dict[str, float | None], clusters_of: Callable[[Pair], str],
              detail: dict) -> Score:
    if not pairs:
        none = empty(THREADS, PAIRWISE, 0.5, detail.get("note", "no pairs after the cutoff"))
        return Score(**{**none.__dict__, "detail": {**detail, **none.detail}})
    credits = pair_credits(pairs, scores)
    groups = [clusters_of(p) for p in pairs]
    iv = accuracy(credits, groups)
    items = {s for p in pairs for s in (p.winner, p.loser)}
    coverage = sum(scores.get(s) is not None for s in items) / len(items)
    return Score(THREADS, PAIRWISE, 0.5, iv.n_clusters, len(pairs), iv.estimate, (iv.low, iv.high), coverage,
                 ids_digest(p.pair_id for p in pairs), detail)


def _spearman(values: list[tuple[str, float | None, float]], detail: dict) -> Score:
    """values: (outcome id, instrument score or None for an abstention, outcome percentile)."""
    if not values:
        return empty(SGU, SPEARMAN, 0.0, detail.get("note", "no ranked _sgu_ replies yet"))
    scored = [v for v in values if v[1] is not None]
    worst = min((v[1] for v in scored), default=0.0) - 1.0
    xs = [v[1] if v[1] is not None else worst for v in values]
    ys = [v[2] for v in values]
    return Score(SGU, SPEARMAN, 0.0, len(values), len(values), spearman(xs, ys), spearman_interval(xs, ys),
                 len(scored) / len(values), ids_digest(v[0] for v in values), detail)


def _discordant(pairs: list[Pair], passed: dict[str, bool]) -> list[Pair]:
    return [p for p in pairs if p.winner in passed and p.loser in passed and passed[p.winner] != passed[p.loser]]


def _screen_threads(pairs_with_origin, passed: dict[str, bool], deferred: int) -> Score:
    pairs = [p for p, _ in pairs_with_origin]
    judged = {s for p in pairs for s in (p.winner, p.loser) if s in passed}
    rate = sum(passed[s] for s in judged) / len(judged) if judged else None
    disc = _discordant(pairs, passed)
    detail = {"pass_rate_on_real_replies": None if rate is None else round(rate, 4), "replies_judged": len(judged),
              "discordant_pairs": len(disc), "deferred": deferred, "origins": _origins(pairs_with_origin, disc)}
    if not disc:
        detail["note"] = "no pair where one reply passed and the other failed"
    return _pairwise(disc, {s: float(v) for s, v in passed.items()}, lambda p: p.group, detail)


def _origins(pairs_with_origin, kept: list[Pair]) -> dict:
    ids = {p.pair_id for p in kept}
    out = defaultdict(set)
    for p, origin in pairs_with_origin:
        if p.pair_id in ids:
            out[origin].add(p.group)
    return {k: len(v) for k, v in sorted(out.items())}


def _thread_replies(ev: Evidence, root_id: str) -> list:
    """Direct replies to a watched or corpus root, oldest first."""
    corpus = ev.watch if ev.watch is not None and ev.watch.get(root_id) is not None else ev.frozen
    return sorted((it for it in corpus.items if it.thread_root == root_id and it.parent_id == root_id
                   and reply_body(it.text)), key=lambda it: it.created_at)


# ---------------------------------------------------------------- fitted rankers


class ReplyRanker:
    name, role = "reply_ranker", "gate"
    what = "pairwise logistic on a reply's timing, reach and text (harness/rank/reply.py)"
    blocks = ("timing", "text")

    def __init__(self):
        from harness.corpus.bench import DEV_BEFORE

        self.fit_before = DEV_BEFORE
        self._model = None

    def version(self) -> str:
        from harness.rank import pairwise, reply

        return fingerprint(self.blocks, self.fit_before.isoformat(), inspect.getsource(reply.timing_features),
                           inspect.getsource(reply.text_features), inspect.getsource(pairwise.fit))

    def cutoff(self, ev: Evidence, first_seen: datetime) -> tuple[datetime, str]:
        return self.fit_before, f"trained on reply pairs from threads rooted before {self.fit_before.date()}"

    def model(self, ev: Evidence):
        if self._model is None:
            from harness.rank import reply

            self._model = reply.fit(self.fit_before, self.blocks, ev.frozen, pairs=ev.frozen_pairs)
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
        values = []
        if ev.sgu:
            model = self.model(ev)
            values = [(r.action_id, model.score(r.parent_text, r.text, r.minutes_after or 0.0, r.followers),
                       r.percentile) for r in ev.sgu]
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

    def fit(self):
        """(weights, means, stds) of a pairwise logistic on Claude's label features, z-scored on dev items."""
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
        labels = (parse_json(text) or {}).get("labels")
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
            except Exception as e:  # noqa: BLE001 - one failed label is an abstention, not a crashed cycle
                ctx.log(f"[instruments] {self.name}: labeling {sid} failed: {type(e).__name__}: {e}")
                cache.put(sid, None)
        w, z, schema = self.fit()
        scores = {s: (None if cache.get(s) is None else pairwise.dot(w, z(label_features(
            {k: [v] for k, v in cache.get(s).items()}, schema)))) for s in cache.rows}
        done = [p for p in pairs if p.winner in cache and p.loser in cache]
        out[THREADS] = _pairwise(done, scores, lambda p: p.group, {"deferred_posts": deferred})
        return out


# ---------------------------------------------------------------- LLM and rule instruments


class LineupDetector:
    name, role = "lineup_detector", "gate"
    what = "Opus 5.5 names the AI-written reply among five (harness/iterate/fitness/action.py)"

    def version(self) -> str:
        from harness.iterate.fitness.action import DETECTOR_MODEL, LINEUP_SYSTEM

        return fingerprint(DETECTOR_MODEL, LINEUP_SYSTEM)

    def cutoff(self, ev: Evidence, first_seen: datetime) -> tuple[datetime, str]:
        return first_seen, "a prompt: scored only on threads rooted after the ledger first saw this version"

    def _pick(self, ev: Evidence, root, shown: list[str], ctx: Ctx) -> int | None:
        from harness.iterate.fitness.action import DETECTOR_MODEL, LINEUP_SYSTEM, clean

        user = f"POST:\n@{root.author} posted:\n{clean(root.text)}\n\nREPLIES:\n" + "\n".join(
            f"{i + 1}. {clean(t)}" for i, t in enumerate(shown))
        out = ctx.llm_for(DETECTOR_MODEL, self.name).complete_json(
            LINEUP_SYSTEM, user, mock=lambda: {"ai_reply": 1 + int(hashlib.sha256(user.encode()).hexdigest(), 16) % 5})
        pick = int(out["ai_reply"])
        return pick if 1 <= pick <= len(shown) else None

    def score(self, ev: Evidence, cutoff: datetime, ctx: Ctx) -> dict[str, Score]:
        cache = Cache(ctx.cache_dir / f"{self.name}-{self.version()}.jsonl")
        pairs_o = ev.reply_pairs(cutoff)
        roots = sorted({p.group for p, _ in pairs_o})
        deferred, pairs, scores = 0, [], {}
        for root_id in roots:
            root = ev.item(root_id)
            replies = [r for r in _thread_replies(ev, root_id) if len(reply_body(r.text).split()) >= 3]
            if len(replies) < LINEUP_SIZE:
                continue
            shown = random.Random(f"lineup:{root_id}").sample(replies, LINEUP_SIZE)
            key = f"thread:{root_id}"
            if key not in cache:
                try:
                    pick = self._pick(ev, root, [r.text for r in shown], ctx)
                except BudgetExceeded:
                    deferred += 1
                    continue
                except Exception as e:  # noqa: BLE001 - an unusable answer is an abstention
                    ctx.log(f"[instruments] {self.name}: {root_id} failed: {type(e).__name__}: {e}")
                    pick = None
                cache.put(key, {"pick": pick, "shown": [r.status_id for r in shown]})
            row = cache.get(key)
            if row["pick"] is None:
                continue  # no reply was named, so no pair has a side to credit; coverage records it
            named = row["shown"][row["pick"] - 1]
            for other in row["shown"]:
                o, n = ev.item(other), ev.item(named)
                if other == named or o.metrics["likes"] == n.metrics["likes"]:
                    continue
                w, l = (other, named) if o.metrics["likes"] > n.metrics["likes"] else (named, other)
                pairs.append(Pair(f"{root_id}:{w}:{l}", "lineup", root_id, w, l, ev.item(w).metrics["likes"],
                                  ev.item(l).metrics["likes"], "rolling"))
                scores[other], scores[named] = 1.0, 0.0
        attempted = [k for k in (f"thread:{r}" for r in roots) if k in cache]
        named = sum(cache.get(k)["pick"] is not None for k in attempted)
        detail = {"lineups": len(attempted), "lineups_named": named, "deferred": deferred}
        out = {THREADS: _pairwise(pairs, scores, lambda p: p.group, detail)}
        out[SGU] = self._sgu(ev, ctx, cache)
        return out

    def _sgu(self, ev: Evidence, ctx: Ctx, cache: Cache) -> Score:
        values = []
        for r in ev.sgu:
            key = f"sgu:{r.action_id}"
            if key not in cache:
                others = [x for x in _thread_replies(ev, r.parent_status) if x.status_id != r.status_id]
                if len(others) < LINEUP_SIZE - 1 or ev.item(r.parent_status) is None:
                    cache.put(key, {"human": None, "why": "parent thread not read by the watcher"})
                else:
                    rng = random.Random(f"lineup:{r.action_id}")
                    shown = [x.text for x in rng.sample(others, LINEUP_SIZE - 1)]
                    at = rng.randrange(LINEUP_SIZE)
                    shown.insert(at, r.text)
                    try:
                        pick = self._pick(ev, ev.item(r.parent_status), shown, ctx)
                    except BudgetExceeded:
                        continue
                    cache.put(key, {"human": None if pick is None else float(pick != at + 1)})
            values.append((r.action_id, cache.get(key)["human"], r.percentile))
        return _spearman(values, {"score": "1 when the detector named another reply, 0 when it named ours"})


@contextmanager
def _metered_routing(ctx: Ctx, name: str):
    """The veto builds its own AnthropicLLM through harness.minds.routing; charge those calls to this instrument."""
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


class ActionVeto:
    name, role = "action_veto", "gate"
    what = "the reply lane's veto: Opus 5.5 in two orders (harness/minds/veto.py)"

    def version(self) -> str:
        from harness.minds import settings, veto

        cfg = settings.ActionConfig()
        return fingerprint(veto.VETO_SYSTEM, cfg.veto_model, cfg.veto_max_p_ai)

    def cutoff(self, ev: Evidence, first_seen: datetime) -> tuple[datetime, str]:
        return first_seen, "a prompt: scored only on threads rooted after the ledger first saw this version"

    def _judge(self, llm, texts: list[str], context: str, profile: str) -> list[bool | None]:
        from harness.minds import settings, veto

        with config.use_profile(profile):
            results = veto.veto(llm, settings.ActionConfig(), texts, lane="reply", context=context,
                                mission=config.mission())
        by_text = {r.text: r for r in results}
        # A batch the judge answered in the wrong shape comes back as all vetoed with the error as `why`.
        return [None if t not in by_text or (by_text[t].fwd is None and by_text[t].rev is None)
                else by_text[t].passed for t in texts]

    def score(self, ev: Evidence, cutoff: datetime, ctx: Ctx) -> dict[str, Score]:
        cache = Cache(ctx.cache_dir / f"{self.name}-{self.version()}.jsonl")
        pairs_o = ev.reply_pairs(cutoff)
        by_root = defaultdict(set)
        for p, _ in pairs_o:
            by_root[p.group] |= {p.winner, p.loser}
        deferred = 0
        with _metered_routing(ctx, self.name) as llm:
            for root_id, sids in sorted(by_root.items()):
                todo = sorted(s for s in sids if s not in cache)[:MAX_BATCH]
                if not todo:
                    continue
                root = ev.item(root_id)
                rest = [r.text for r in _thread_replies(ev, root_id) if r.status_id not in todo][:MAX_BATCH]
                context = f"POST by @{root.author}:\n{root.text}\n\nTHREAD:\n" + "\n".join(f"- {t}" for t in rest)
                texts = [reply_body(ev.item(s).text) for s in todo]
                try:
                    verdicts = self._judge(llm, texts, context, ev.profile(root_id))
                except BudgetExceeded:
                    deferred += len(todo)
                    continue
                for s, v in zip(todo, verdicts):
                    cache.put(s, v)
            sgu = self._sgu(ev, ctx, cache, llm)
        passed = {s: bool(v) for s, v in cache.rows.items() if v is not None and not s.startswith("sgu:")}
        # A reply the veto couldn't judge counts as vetoed, its worst outcome.
        passed.update({s: False for s, v in cache.rows.items() if v is None and not s.startswith("sgu:")})
        return {THREADS: _screen_threads(pairs_o, passed, deferred), SGU: sgu}

    def _sgu(self, ev: Evidence, ctx: Ctx, cache: Cache, llm) -> Score:
        values = []
        for r in ev.sgu:
            key = f"sgu:{r.action_id}"
            if key not in cache:
                try:
                    (v,) = self._judge(llm, [r.text], f"POST:\n{r.parent_text}", r.profile or "devtools")
                except BudgetExceeded:
                    continue
                cache.put(key, v)
            v = cache.get(key)
            values.append((r.action_id, None if v is None else float(v), r.percentile))
        return _spearman(values, {})


class SlopGate:
    name, role = "slop_gate", "gate"
    what = "the deterministic slop and grounding gate (harness/minds/gate.py)"

    def version(self) -> str:
        from harness.minds import gate, settings

        return fingerprint(inspect.getsource(gate), settings.GateLimits())

    def cutoff(self, ev: Evidence, first_seen: datetime) -> tuple[datetime, str]:
        return first_seen, "rules tuned by hand: scored only on threads rooted after the ledger first saw this version"

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
        values = [(r.action_id, float(self.check(r.text, r.parent_text, [])), r.percentile) for r in ev.sgu]
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
                    value = panel_mean({k: float(v) for k, v in s.items() if k in panel}) if set(s) >= set(panel) else None
                except BudgetExceeded:
                    deferred += 1
                    continue
                except Exception as e:  # noqa: BLE001 - one failed panel call is an abstention
                    ctx.log(f"[instruments] panel: {sid} failed: {type(e).__name__}: {e}")
                    value = None
            cache.put(sid, value)
        done = [p for p in pairs if p.winner in cache and p.loser in cache]
        out[THREADS] = _pairwise(done, dict(cache.rows), lambda p: p.group, {"deferred_posts": deferred})
        return out


ALL = (ReplyRanker, TfidfPostRanker, LabelModel, LineupDetector, ActionVeto, SlopGate, Panel)


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
        except Exception as e:  # noqa: BLE001 - one broken instrument must not stop the others' scoring
            errors[inst.name] = f"{type(e).__name__}: {e}"
            ctx.log(f"[instruments] {inst.name} not scored: {errors[inst.name]}")
            continue
        events += ledger_mod.record(ledger, inst.name, role=inst.role, what=inst.what, version=version,
                                    cutoff=cutoff, cutoff_basis=basis, scores=scores, now=now)
    ledger_mod.save(ledger, ledger_path)
    return ledger, events, errors
