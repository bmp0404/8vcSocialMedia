"""The post benchmark's null check: do format features or the words predict which of an account's own
posts did better?

Two baselines, both scored on the frozen post pairs (data/bench/posts.jsonl):
- format: pairwise logistic on media, quote, link, all-lowercase, length and the gap since the
  author's previous status, fitted on dev-author pairs and scored on test authors.
- tfidf: ridge regression on word and character TF-IDF plus shape features, with the target log
  engagement z-scored within each account. It mirrors `harness/virality.py` on
  `origin/aarav/self-improving` (0.756 same-account pair accuracy claimed) in pure Python, because
  numpy and scikit-learn are not installed here.
"""

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from harness.corpus import Corpus
from harness.corpus.bench import Pair, eligible_posts
from harness.corpus.stats import accuracy, pair_credit
from harness.rank import pairwise

_URL = re.compile(r"https?://\S+")
_WORD = re.compile(r"(?u)\b\w\w+\b")  # scikit-learn's default token pattern


def clean_text(text: str) -> str:
    return _URL.sub(" ", text).strip()


def format_features(item, gap_minutes: float | None) -> dict[str, float]:
    text = clean_text(item.text)
    letters = [c for c in text if c.isalpha()]
    return {
        "f:media": 1.0 if item.media_types else 0.0,
        "f:quote": 1.0 if item.quoted_id else 0.0,
        "f:link": 1.0 if item.n_links or "http" in item.text else 0.0,
        "f:all_lowercase": 1.0 if letters and not any(c.isupper() for c in letters) else 0.0,
        "f:log_chars": math.log1p(len(text)),
        "f:log_gap": math.log1p(gap_minutes) if gap_minutes is not None else 0.0,
        "f:gap_known": 1.0 if gap_minutes is not None else 0.0,
    }


def gaps(corpus: Corpus) -> dict[str, float | None]:
    """Status ID -> minutes since the same author's previous status of any kind in the corpus."""
    by_author = defaultdict(list)
    for it in corpus.items:
        by_author[it.author.lower()].append(it)
    out = {}
    for items in by_author.values():
        items.sort(key=lambda i: i.created_at)
        prev = None
        for it in items:
            out[it.status_id] = (it.created_at - prev.created_at).total_seconds() / 60 if prev else None
            prev = it
    return out


def fit_dev_scores(pairs: list[Pair], feats: dict[str, dict[str, float]]) -> tuple[dict[str, float], dict, float]:
    """Pairwise logistic fitted on the dev pairs whose two items have features, z-scored on dev items.
    Returns (status ID -> score for every item in `feats`, weights, L2 strength)."""
    dev = [p for p in pairs if p.split == "dev" and p.winner in feats and p.loser in feats]
    if not dev:
        raise ValueError("no dev pairs with features")
    keys = sorted({k for f in feats.values() for k in f})
    vals = {k: [feats[sid].get(k, 0.0) for p in dev for sid in (p.winner, p.loser)] for k in keys}
    mean = {k: sum(v) / len(v) for k, v in vals.items()}
    std = {k: (math.sqrt(sum((x - mean[k]) ** 2 for x in v) / len(v)) or 1.0) for k, v in vals.items()}

    def z(sid):
        return {k: (feats[sid].get(k, 0.0) - mean[k]) / std[k] for k in keys}
    diffs = [pairwise.diff(z(p.winner), z(p.loser)) for p in dev]
    l2, _ = pairwise.choose_l2(diffs, [p.group for p in dev])
    w = pairwise.fit(diffs, l2)
    return {sid: pairwise.dot(w, z(sid)) for sid in feats}, w, l2


def format_features_for(pairs: list[Pair], corpus: Corpus) -> dict[str, dict[str, float]]:
    g = gaps(corpus)
    return {sid: format_features(corpus.get(sid), g.get(sid)) for p in pairs for sid in (p.winner, p.loser)}


def format_baseline(pairs: list[Pair], corpus: Corpus) -> dict:
    feats = format_features_for(pairs, corpus)
    dev = [p for p in pairs if p.split == "dev"]
    test = [p for p in pairs if p.split == "test"]
    keys = sorted({k for f in feats.values() for k in f})
    scores, w, l2 = fit_dev_scores(pairs, feats)
    cr = [pair_credit(scores[p.winner], scores[p.loser]) for p in test]
    single = {}
    for k in keys:
        if k == "f:gap_known":
            continue
        sign = 1.0 if sum(pair_credit(feats[p.winner][k], feats[p.loser][k]) for p in dev) / len(dev) >= 0.5 else -1.0
        single[k[2:]] = accuracy([pair_credit(sign * feats[p.winner][k], sign * feats[p.loser][k]) for p in test],
                                 [p.group for p in test]).to_dict()
    # Time of day: dev win rate per 4-hour UTC block, as for replies.
    wins, seen = Counter(), Counter()
    for p in dev:
        for sid, won in ((p.winner, 1), (p.loser, 0)):
            b = corpus.get(sid).created_at.hour // 4
            wins[b] += won
            seen[b] += 1
    rate = {b: (wins[b] + 1) / (seen[b] + 2) for b in range(6)}

    def tod(sid):
        return rate[corpus.get(sid).created_at.hour // 4]
    single["time_of_day"] = accuracy([pair_credit(tod(p.winner), tod(p.loser)) for p in test],
                                     [p.group for p in test]).to_dict()
    mol = {sid: max(f["f:media"], f["f:link"]) for sid, f in feats.items()}
    sign = 1.0 if sum(pair_credit(mol[p.winner], mol[p.loser]) for p in dev) / len(dev) >= 0.5 else -1.0
    single["media_or_link"] = accuracy([pair_credit(sign * mol[p.winner], sign * mol[p.loser]) for p in test],
                                       [p.group for p in test]).to_dict()
    single["constant"] = accuracy([0.5] * len(test), [p.group for p in test]).to_dict()
    return {"l2": l2, "weights": {k: round(v, 4) for k, v in sorted(w.items(), key=lambda kv: -abs(kv[1]))},
            "test": accuracy(cr, [p.group for p in test]).to_dict(), "single_features_test": single}


def label_features_for(pairs: list[Pair], votes: dict, schema: dict) -> dict[str, dict[str, float]]:
    from harness.rank.reply import label_features
    return {sid: label_features(votes[f"x:{sid}"], schema)
            for p in pairs for sid in (p.winner, p.loser) if f"x:{sid}" in votes}


def label_baseline(pairs: list[Pair], votes: dict, schema: dict, corpus: Corpus | None = None) -> dict:
    """Pairwise logistic on LLM label features (harness/rank/reply.label_features), dev authors -> test authors.
    Only pairs whose two posts both have labels count, so the number is on a subset of the test pairs."""
    feats = label_features_for(pairs, votes, schema)
    usable = [p for p in pairs if p.winner in feats and p.loser in feats]
    test = [p for p in usable if p.split == "test"]
    if not test:
        raise ValueError("no labeled post pairs in the test split")
    scores, w, l2 = fit_dev_scores(pairs, feats)
    cr = [pair_credit(scores[p.winner], scores[p.loser]) for p in test]
    vs_length = None
    if corpus is not None:
        from harness.corpus.stats import paired_difference
        cl = [pair_credit(len(clean_text(corpus.get(p.winner).text)), len(clean_text(corpus.get(p.loser).text)))
              for p in test]
        vs_length = paired_difference(cr, cl, [p.group for p in test]).to_dict()
    return {"l2": l2, "test": accuracy(cr, [p.group for p in test]).to_dict(), "minus_length_test": vs_length,
            "test_pairs_labeled": len(test), "test_pairs_total": sum(p.split == "test" for p in pairs),
            "weights": {k: round(v, 4) for k, v in sorted(w.items(), key=lambda kv: -abs(kv[1]))[:12]}}


# ---- TF-IDF ridge ------------------------------------------------------------------------------

def _char_wb(text: str, lo: int = 3, hi: int = 5) -> list[str]:
    """scikit-learn's char_wb analyzer: n-grams inside each space-padded word."""
    grams = []
    for word in text.lower().split():
        w = f" {word} "
        for n in range(lo, hi + 1):
            if len(w) < n:
                break
            grams.extend(w[i: i + n] for i in range(len(w) - n + 1))
    return grams


def _word_grams(text: str) -> list[str]:
    toks = _WORD.findall(text.lower())
    return toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]


def _shape(text: str) -> list[float]:
    lines = [l for l in text.split("\n") if l.strip()]
    return [math.log1p(len(text)), math.log1p(len(text.split())), float(len(lines)), float("?" in text),
            float("!" in text), float(bool(re.search(r"\d", text))), float(bool(re.search(r"[^\x00-\x7f]", text))),
            float(text.count("@")), float(bool(re.search(r"^\s*([-*•]|\d+[.)])\s", text, re.M)))]


class Tfidf:
    """Sublinear TF, smoothed IDF and L2-normalized rows, as TfidfVectorizer(sublinear_tf=True) does."""

    def __init__(self, analyzer, min_df: int, max_features: int, prefix: str):
        self.analyzer, self.min_df, self.max_features, self.prefix = analyzer, min_df, max_features, prefix
        self.idf: dict[str, float] = {}

    def fit(self, texts: list[str]) -> "Tfidf":
        df = Counter()
        for t in texts:
            df.update(set(self.analyzer(t)))
        kept = [g for g, c in df.items() if c >= self.min_df]
        total = Counter()
        for t in texts:
            total.update(g for g in self.analyzer(t) if df[g] >= self.min_df)
        kept = sorted(kept, key=lambda g: (-total[g], g))[: self.max_features]
        n = len(texts)
        self.idf = {g: math.log((1 + n) / (1 + df[g])) + 1 for g in kept}
        return self

    def transform(self, text: str) -> dict[str, float]:
        tf = Counter(g for g in self.analyzer(text) if g in self.idf)
        row = {f"{self.prefix}{g}": (1 + math.log(c)) * self.idf[g] for g, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in row.values())) or 1.0
        return {k: v / norm for k, v in row.items()}


@dataclass
class RidgeModel:
    weights: dict[str, float]
    intercept: float
    word: Tfidf
    char: Tfidf
    shape_mean: list[float]
    shape_std: list[float]

    def row(self, text: str) -> dict[str, float]:
        return _row(text, self.word, self.char, self.shape_mean, self.shape_std)

    def predict(self, text: str) -> float:
        return self.intercept + pairwise.dot(self.weights, self.row(text))


def _row(text, word, char, mean, std) -> dict[str, float]:
    r = word.transform(text) | char.transform(text)
    for i, v in enumerate(_shape(text)):
        r[f"s:{i}"] = (v - mean[i]) / std[i]
    return r


def ridge_fit(texts: list[str], y: list[float], alpha: float, iters: int = 60, word_min_df: int = 3,
              char_min_df: int = 5) -> RidgeModel:
    """Solve (X'X + alpha I) w = X'(y - mean y) by conjugate gradient; the intercept is mean y.
    Vocabulary limits follow harness/virality.py: words min_df 3 (20,000 max), characters min_df 5 (30,000 max)."""
    word = Tfidf(_word_grams, word_min_df, 20000, "w:").fit(texts)
    char = Tfidf(_char_wb, char_min_df, 30000, "c:").fit(texts)
    shapes = [_shape(t) for t in texts]
    mean = [sum(s[i] for s in shapes) / len(shapes) for i in range(len(shapes[0]))]
    std = [math.sqrt(sum((s[i] - mean[i]) ** 2 for s in shapes) / len(shapes)) + 1e-9 for i in range(len(mean))]
    rows = [_row(t, word, char, mean, std) for t in texts]
    ybar = sum(y) / len(y)
    yc = [v - ybar for v in y]

    def xt(vec):  # X' vec
        out = defaultdict(float)
        for r, v in zip(rows, vec):
            if v:
                for k, x in r.items():
                    out[k] += x * v
        return out

    def a_mul(w):  # (X'X + alpha I) w
        xw = [sum(w.get(k, 0.0) * x for k, x in r.items()) for r in rows]
        out = xt(xw)
        for k, v in w.items():
            out[k] += alpha * v
        return out

    b = xt(yc)
    w = defaultdict(float)
    r = dict(b)
    p = dict(r)
    rs = sum(v * v for v in r.values())
    for _ in range(iters):
        ap = a_mul(p)
        denom = sum(p[k] * ap.get(k, 0.0) for k in p)
        if denom <= 0:
            break
        step = rs / denom
        for k, v in p.items():
            w[k] += step * v
        for k in set(r) | set(ap):
            r[k] = r.get(k, 0.0) - step * ap.get(k, 0.0)
        rs_new = sum(v * v for v in r.values())
        if rs_new < 1e-10 * (sum(v * v for v in b.values()) or 1.0):
            break
        p = {k: r.get(k, 0.0) + (rs_new / rs) * p.get(k, 0.0) for k in set(r) | set(p)}
        rs = rs_new
    return RidgeModel(dict(w), ybar, word, char, mean, std)


def within_account_targets(corpus: Corpus, min_posts: int = 5) -> dict[str, tuple[str, float]]:
    """Status ID -> (account, z-scored log1p engagement within the account) over eligible posts."""
    out = {}
    for acct, posts in eligible_posts(corpus).items():
        posts = [p for p in posts if len(clean_text(p.text)) >= 10]
        if len(posts) < min_posts:
            continue
        logs = [math.log1p(p.engagement) for p in posts]
        m = sum(logs) / len(logs)
        sd = math.sqrt(sum((v - m) ** 2 for v in logs) / len(logs)) or 1.0
        for p, v in zip(posts, logs):
            out[p.status_id] = (acct, (v - m) / sd)
    return out


def tfidf_oof(corpus: Corpus, alpha: float = 10.0, folds: int = 5, extra: dict | None = None) -> dict[str, float]:
    """Out-of-fold ridge predictions: each account's posts are scored by a model fitted on the other folds.

    Folds are accounts sorted by crc32 of the handle, dealt round-robin. `extra` maps an account to more
    of its items to score with the same held-out model (the run_posts easy-pairs check uses it).
    """
    import zlib
    targets = within_account_targets(corpus)
    accounts = sorted({a for a, _ in targets.values()}, key=lambda a: zlib.crc32(a.encode()))
    fold_of = {a: i % folds for i, a in enumerate(accounts)}
    to_score = defaultdict(list)
    for sid, (a, _) in targets.items():
        to_score[a].append(sid)
    for a, items in (extra or {}).items():
        if a in fold_of:
            to_score[a] += [i.status_id for i in items if i.status_id not in targets]
    oof = {}
    for f in range(folds):
        train = [sid for sid, (a, _) in targets.items() if fold_of[a] != f]
        model = ridge_fit([clean_text(corpus.get(s).text) for s in train], [targets[s][1] for s in train], alpha)
        for a in accounts:
            if fold_of[a] == f:
                for sid in to_score[a]:
                    oof[sid] = model.predict(clean_text(corpus.get(sid).text))
    return oof
