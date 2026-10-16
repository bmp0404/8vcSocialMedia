"""A learned ranker for replies within one thread, and the benchmark that decides if its text features help.

The question it answers: once you know when a reply came and who wrote it, does its wording predict
which reply in a thread gets more likes? `fit(before)` trains a pairwise logistic model on reply
pairs from threads rooted before a cutoff; `benchmark(model, cutoff)` scores the week after the
cutoff and compares the model with a timing-and-reach model fitted on the same threads.

Feature blocks:
- timing: log minutes after the root post, log1p of the author's followers, verified.
- text: length, emoji, question mark, extra mentions, caps ratio, first person, all lowercase,
  word overlap with the root, link and media.
- ngrams: hashed word unigrams and bigrams. Kept out of `text` because on the dev cutoff
  (2026-09-29) they cost 0.06 against timing alone while the other text features added 0.02:
  with 60 training threads they memorize topics.
- labels: LLM label fields (harness/label), averaged over annotators.
"""

import math
import re
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from harness.corpus import Corpus, load
from harness.corpus.bench import WEEK, Pair, load_pairs, reply_body, window
from harness.corpus.stats import Interval, accuracy, pair_credit, paired_difference
from harness.rank import pairwise

BLOCKS = ("timing", "text", "ngrams", "labels")
HASH_DIM = 1 << 18
_WORD = re.compile(r"[a-z0-9][a-z0-9'_-]*")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9'_-]*|[^\sa-z0-9]")
_MENTION = re.compile(r"@\w+")
_FIRST_PERSON = re.compile(r"\b(i|i'm|im|i've|i'd|i'll|me|my|mine|myself)\b")
_LINK = re.compile(r"https?://")


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return o >= 0x1F000 or 0x2600 <= o <= 0x27BF


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) >= 3}


def timing_features(minutes_after: float, followers: int | None, verified: bool) -> dict[str, float]:
    return {"t:log_minutes": math.log1p(max(minutes_after, 0.0)), "t:log_followers": math.log1p(followers or 0),
            "t:verified": 1.0 if verified else 0.0}


def text_features(parent: str, text: str, media: bool = False, links: int = 0) -> dict[str, float]:
    body = reply_body(text)
    letters = [c for c in body if c.isalpha()]
    upper = sum(c.isupper() for c in letters)
    pw, bw = _words(parent), _words(body)
    feats = {
        "x:log_chars": math.log1p(len(body)),
        "x:log_emoji": math.log1p(sum(_is_emoji(c) for c in body)),
        "x:question": 1.0 if "?" in body else 0.0,
        "x:log_mentions": math.log1p(len(_MENTION.findall(body))),
        "x:caps_ratio": upper / len(letters) if letters else 0.0,
        "x:first_person": 1.0 if _FIRST_PERSON.search(body.lower()) else 0.0,
        "x:all_lowercase": 1.0 if letters and upper == 0 else 0.0,
        "x:overlap": len(pw & bw) / len(pw | bw) if pw | bw else 0.0,
        "x:link": 1.0 if links or _LINK.search(body) else 0.0,
        "x:media": 1.0 if media else 0.0,
    }
    return feats


def ngram_features(text: str) -> dict[str, float]:
    """Hashed word unigrams and bigrams of the reply body, links removed."""
    tokens = _TOKEN.findall(_LINK.sub(" ", reply_body(text).lower()))
    grams = set(tokens) | {f"{a} {b}" for a, b in zip(tokens, tokens[1:])}
    return {f"h:{zlib.crc32(g.encode()) % HASH_DIM}": 1.0 for g in grams}


def label_features(labels: dict[str, list] | None, schema: dict) -> dict[str, float]:
    """`labels` maps a field to the values the annotators gave. Ordinal fields become the mean rank,
    enum fields the share of annotators that chose each value."""
    feats = {}
    if not labels:
        return feats
    for f in schema["fields"]:
        values = labels.get(f["name"])
        if not values:
            continue
        if f["kind"] == "ordinal":
            ranks = [f["values"].index(v) for v in values]
            feats[f"l:{f['name']}"] = sum(ranks) / len(ranks)
            feats[f"l:{f['name']}:present"] = 1.0
        elif f["kind"] == "enum":
            for v in values:
                feats[f"l:{f['name']}={v}"] = feats.get(f"l:{f['name']}={v}", 0.0) + 1.0 / len(values)
    return feats


@dataclass
class ReplyInput:
    """What the ranker sees about one reply. Nothing here is measured after the reply was posted."""

    parent: str
    text: str
    minutes_after: float
    followers: int | None
    verified: bool = False
    media: bool = False
    links: int = 0
    labels: dict | None = None


def reply_inputs(corpus: Corpus, labels: dict | None = None) -> dict[str, ReplyInput]:
    """Status ID -> ReplyInput for every reply that sits in a benchmark pair."""
    out = {}
    for p in load_pairs("replies"):
        root = corpus.get(p.group)
        for sid in (p.winner, p.loser):
            if sid in out:
                continue
            it = corpus.get(sid)
            out[sid] = ReplyInput(root.text, it.text, (it.created_at - root.created_at).total_seconds() / 60,
                                  it.author_followers, bool(it.author_verified), it.has_media, it.n_links,
                                  (labels or {}).get(f"x:{sid}"))
    return out


@dataclass
class ReplyRanker:
    blocks: tuple[str, ...]
    trained_before: datetime
    weights: dict[str, float]
    means: dict[str, float]
    stds: dict[str, float]
    l2: float
    n_pairs: int
    n_threads: int
    schema: dict | None = None
    cv: dict = field(default_factory=dict)

    def features(self, r: ReplyInput) -> dict[str, float]:
        raw = {}
        if "timing" in self.blocks:
            raw.update(timing_features(r.minutes_after, r.followers, r.verified))
        if "text" in self.blocks:
            raw.update(text_features(r.parent, r.text, r.media, r.links))
        if "ngrams" in self.blocks:
            raw.update(ngram_features(r.text))
        if "labels" in self.blocks:
            raw.update(label_features(r.labels, self.schema))
        return {k: (v - self.means[k]) / self.stds[k] if k in self.means else v for k, v in raw.items()}

    def score_input(self, r: ReplyInput) -> float:
        return pairwise.dot(self.weights, self.features(r))

    def score(self, parent: str, text: str, minutes_after: float, followers: int | None, verified: bool = False,
              media: bool = False, links: int = 0, labels: dict | None = None) -> float:
        """Higher means more likely to out-like other replies in the same thread."""
        return self.score_input(ReplyInput(parent, text, minutes_after, followers, verified, media, links, labels))

    def top_weights(self, n: int = 12) -> list[tuple[str, float]]:
        named = [(k, v) for k, v in self.weights.items() if not k.startswith("h:")]
        return sorted(named, key=lambda kv: -abs(kv[1]))[:n]


def _standardize(inputs: list[dict[str, float]]) -> tuple[dict, dict]:
    """Mean and std of every dense (non-hashed) feature over the training replies."""
    keys = {k for x in inputs for k in x if not k.startswith("h:")}
    means, stds = {}, {}
    for k in keys:
        vals = [x.get(k, 0.0) for x in inputs]
        m = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
        means[k], stds[k] = m, sd if sd > 0 else 1.0
    return means, stds


def fit(before: datetime, blocks: tuple[str, ...] = ("timing", "text"), corpus: Corpus | None = None,
        labels: dict | None = None, schema: dict | None = None, pairs: list[Pair] | None = None,
        l2: float | None = None, seed: int = 0) -> ReplyRanker:
    """Train on reply pairs whose thread root was posted before `before`."""
    unknown = set(blocks) - set(BLOCKS)
    if unknown:
        raise ValueError(f"unknown feature blocks {sorted(unknown)}")
    if "labels" in blocks and schema is None:
        raise ValueError("the labels block needs the label schema")
    corpus = corpus or load()
    train = window(pairs if pairs is not None else load_pairs("replies"), end=before)
    if not train:
        raise ValueError(f"no reply pairs rooted before {before}")
    inputs = reply_inputs(corpus, labels)
    probe = ReplyRanker(blocks, before, {}, {}, {}, 0.0, 0, 0, schema)
    raw = {sid: probe.features(inputs[sid]) for p in train for sid in (p.winner, p.loser)}
    means, stds = _standardize(list(raw.values()))
    model = ReplyRanker(blocks, before, {}, means, stds, 0.0, len(train), len({p.group for p in train}), schema)
    x = {sid: model.features(inputs[sid]) for sid in raw}
    diffs = [pairwise.diff(x[p.winner], x[p.loser]) for p in train]
    groups = [p.group for p in train]
    if l2 is None:
        l2, model.cv = pairwise.choose_l2(diffs, groups, seed=seed)
    model.l2 = l2
    model.weights = pairwise.fit(diffs, l2, seed=seed)
    return model


@dataclass
class Report:
    cutoff: datetime
    blocks: tuple[str, ...]
    baseline_blocks: tuple[str, ...]
    model: Interval
    baseline: Interval
    delta: Interval
    high_coverage: dict
    top_weights: list

    def to_dict(self) -> dict:
        return {"cutoff": self.cutoff.isoformat(), "eval_window_days": WEEK.days, "blocks": list(self.blocks),
                "baseline_blocks": list(self.baseline_blocks), "model": self.model.to_dict(),
                "baseline": self.baseline.to_dict(), "delta": self.delta.to_dict(),
                "high_coverage": self.high_coverage,
                "top_weights": [[k, round(v, 4)] for k, v in self.top_weights]}


def credits(model: ReplyRanker, pairs: list[Pair], inputs: dict[str, ReplyInput]) -> list[float]:
    cache = {}

    def s(sid):
        if sid not in cache:
            cache[sid] = model.score_input(inputs[sid])
        return cache[sid]
    return [pair_credit(s(p.winner), s(p.loser)) for p in pairs]


def benchmark(model: ReplyRanker, cutoff: datetime, baseline: ReplyRanker | None = None,
              corpus: Corpus | None = None, labels: dict | None = None, pairs: list[Pair] | None = None,
              b: int = 2000) -> Report:
    """Score the week of threads after `cutoff`, against a timing-and-reach model trained on the same threads.

    Refuses a model trained on threads at or after the cutoff, because then it has seen the answers.
    """
    if model.trained_before > cutoff:
        raise ValueError(f"model was trained on threads up to {model.trained_before}, after the cutoff {cutoff}")
    corpus = corpus or load()
    baseline = baseline or fit(cutoff, ("timing",), corpus, pairs=pairs)
    if baseline.trained_before > cutoff:
        raise ValueError("baseline was trained after the cutoff")
    test = window(pairs if pairs is not None else load_pairs("replies"), start=cutoff, end=cutoff + WEEK)
    if not test:
        raise ValueError(f"no reply pairs rooted in the week after {cutoff}")
    inputs = reply_inputs(corpus, labels)
    cm, cb = credits(model, test, inputs), credits(baseline, test, inputs)
    groups = [p.group for p in test]
    hi = [i for i, p in enumerate(test) if (p.coverage or 0) >= 0.8]
    high = {}
    if hi:
        hg = [groups[i] for i in hi]
        high = {"model": accuracy([cm[i] for i in hi], hg, b).to_dict(),
                "baseline": accuracy([cb[i] for i in hi], hg, b).to_dict(),
                "delta": paired_difference([cm[i] for i in hi], [cb[i] for i in hi], hg, b).to_dict()}
    return Report(cutoff, model.blocks, baseline.blocks, accuracy(cm, groups, b), accuracy(cb, groups, b),
                  paired_difference(cm, cb, groups, b), high, model.top_weights())
