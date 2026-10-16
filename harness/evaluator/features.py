"""Cheap no-LLM draft features + a ridge model on them (stdlib only).

`extract(text)` computes text-only features that exist at recommendation time: length, links, mentions,
hashtags, emoji, question / exclaim, numbers, caps, list / thread markers, first person, hook types
(announce, feature tip, funding / deal, hiring, event, social proof, humor) and big-name mentions. No
author, timing or outcome features. `has_media` is a control: the training posts have it, drafts from the
harness are text-only, so it's always 0 at recommendation time (a constant shift that never changes a
ranking within a batch).

The model (data/feature_model.json) is fit by `scripts/backtest_eval.py --enriched ... --write-feature-model`
on eligible, non-boosted original posts from the NON-holdout accounts of data/context/devtools_enriched.csv;
target = log engagement ratio to the author's median (label_lift_window), centered within each account.
`score(text)` = predicted within-account log lift. Used by evaluate() only with HARNESS_EVAL_FEATURES=on.
"""

from __future__ import annotations

import json
import math
import random
import re
from pathlib import Path

from harness import config

MODEL_PATH = config.ROOT / "data" / "feature_model.json"

URL_RE = re.compile(r"https?://\S+")
MENTION_RE = re.compile(r"(?<![\w@])@\w{1,15}")
HASHTAG_RE = re.compile(r"(?<![\w#])#\w+")
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")
WORD = r"(?<![\w'])(?:{})(?![\w'])"

HOOKS = {
    "hook_announce": r"^(?:introducing|announcing|new:|new in|today,? we|we(?:'re| are|’re) (?:launching|releasing|"
                     r"excited|thrilled|announcing|introducing)|just (?:launched|shipped|released)|now (?:available|live|"
                     r"in beta|open)|is (?:now )?(?:live|out|here|available)|out now|launch(?:ing|ed)?\b|ship(?:ped|ping)\b)"
                     r"|\b(?:is|are) now (?:available|live|in beta|generally available|open|free)\b|\bjust (?:launched|shipped|"
                     r"released|dropped)\b|\bintroducing\b|\bnow available\b|\bout now\b|\bgenerally available\b",
    "hook_feature_tip": r"\byou can now\b|\bhow to\b|\bpro tip\b|\btip:|\blearn how\b|\bhere(?:'s|’s| is) how\b|\bguide\b"
                        r"|\bstep[- ]by[- ]step\b|\btutorial\b|\bwalk(?:s|ing)? (?:you )?through\b",
    "hook_funding_deal": r"\braised?\b|\bseries [a-e]\b|\bfunding\b|\bseed round\b|\bacqui(?:re|red|res|ring|sition)\b"
                         r"|\bjoin(?:s|ing) (?:@\w+|forces)\b|\bpartner(?:ship|ed|ing)?\b|\bvaluation\b",
    "hook_hiring": r"\bwe(?:'re|’re| are) hiring\b|\bhiring\b|\bopen roles?\b|\bjoin (?:our|the) team\b",
    "hook_event": r"\bjoin us\b|\bwebinar\b|\bmeetup\b|\blivestream\b|\blive now\b|\bgoing live\b|\bregister\b|\btickets?\b"
                  r"|\bconference\b|\bsummit\b|\bhackathon\b|\bbooth\b|\bkeynote\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|"
                  r"sept|oct|nov|dec)[a-z]* \d{1,2}\b",
    "hook_social_proof": r"\bcustomers?\b|\bteams? (?:at|like|use|using|trust)\b|\btrusted by\b|\b\d+(?:\.\d+)?[kKmM]\+? "
                         r"(?:users|developers|devs|stars|downloads|teams|customers)\b|\bcongrat\w*\b|\bcase study\b"
                         r"|\bthanks to\b|\bshout ?out\b",
    "hook_humor": r"\blol\b|\blmao\b|\bbe like\b|\bpov\b|\bnobody:|\bme when\b|[\U0001F602\U0001F923\U0001F480]",
}
BIG = (r"openai|anthropic|anthropicai|claudeai|claude|chatgpt|gpt-?\d|gemini|google|googledeepmind|deepmind|grok|xai"
       r"|nvidia|aws|awscloud|microsoft|github|vercel|cloudflare|meta|apple|mistral|stripe|cursor|cursor_ai|linear")
BIG_RE = re.compile(rf"(?:@|\b)(?:{BIG})\b", re.I)
LIST_RE = re.compile(r"(?:^|\s)(?:[-*•▪]|\d{1,2}[.)])\s+\S.*(?:\s(?:[-*•▪]|\d{1,2}[.)])\s+\S)", re.S)
THREAD_RE = re.compile(r"^\s*1\s*/|\U0001F9F5|\bthread\b|\(1/\d+\)", re.I)

FEATURES = [
    "log_chars", "short_lt_60", "long_gt_240", "n_links", "n_mentions", "starts_with_mention", "n_hashtags",
    "n_emoji", "has_exclaim", "has_question", "has_number", "caps_ratio", "has_list", "is_thread",
    "first_person_singular", "first_person_plural", "all_lowercase", "mentions_big_name", "short_no_link",
    *HOOKS, "has_media",
]


def body(text: str) -> str:
    return re.sub(r"\s+", " ", URL_RE.sub(" ", text or "")).strip()


def extract(text: str, has_media: bool = False, strip_media_link: bool = False) -> dict[str, float]:
    """Features for one post / draft. `strip_media_link`: the text comes from an X export where an attached
    photo/video shows up as a trailing t.co link (training data); drafts never need it."""
    raw = re.sub(r"\s+", " ", text or "").strip()  # training text has no newlines; keep drafts comparable
    links = URL_RE.findall(raw)
    if strip_media_link and has_media and re.search(r"https?://t\.co/\S+\s*$", raw):
        links = links[:-1]
    b = body(raw)
    letters = [c for c in b if c.isalpha()]
    low = b.lower()
    n = len(b)
    f = {
        "log_chars": math.log1p(n),
        "short_lt_60": float(n < 60),
        "long_gt_240": float(n > 240),
        "n_links": float(min(len(links), 2)),
        "n_mentions": float(min(len(MENTION_RE.findall(b)), 3)),
        "starts_with_mention": float(b.startswith("@")),
        "n_hashtags": float(min(len(HASHTAG_RE.findall(b)), 3)),
        "n_emoji": float(min(len(EMOJI_RE.findall(b)), 3)),
        "has_exclaim": float("!" in b),
        "has_question": float("?" in b),
        "has_number": float(bool(re.search(r"\d", b))),
        "caps_ratio": sum(c.isupper() for c in letters) / len(letters) if letters else 0.0,
        "has_list": float(bool(LIST_RE.search(raw))),
        "is_thread": float(bool(THREAD_RE.search(raw))),
        "first_person_singular": float(bool(re.search(WORD.format(r"i|i'm|i’m|i've|i’ve|my|me"), low))),
        "first_person_plural": float(bool(re.search(WORD.format(r"we|we're|we’re|we've|we’ve|our|us"), low))),
        "all_lowercase": float(bool(letters) and not any(c.isupper() for c in letters)),
        "mentions_big_name": float(bool(BIG_RE.search(b))),
        "short_no_link": float(n < 100 and not links),
        "has_media": float(bool(has_media)),
    }
    for name, pat in HOOKS.items():
        f[name] = float(bool(re.search(pat, low)))
    return f


# --------------------------------------------------------------------------- ridge (stdlib)


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (a is small and symmetric positive definite)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        piv = m[c][c] or 1e-12
        for r in range(c + 1, n):
            k = m[r][c] / piv
            if k:
                for j in range(c, n + 1):
                    m[r][j] -= k * m[c][j]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][j] * x[j] for j in range(r + 1, n))) / (m[r][r] or 1e-12)
    return x


def fit(rows: list[dict[str, float]], y: list[float], lam: float) -> dict:
    """Ridge on standardized FEATURES (intercept unpenalized). Returns a JSON-able model."""
    n = len(rows)
    mu = {k: sum(r[k] for r in rows) / n for k in FEATURES}
    sd = {k: math.sqrt(sum((r[k] - mu[k]) ** 2 for r in rows) / n) or 1.0 for k in FEATURES}
    X = [[(r[k] - mu[k]) / sd[k] for k in FEATURES] for r in rows]
    ym = sum(y) / n
    yc = [v - ym for v in y]
    d = len(FEATURES)
    xtx = [[sum(x[i] * x[j] for x in X) + (lam if i == j else 0.0) for j in range(d)] for i in range(d)]
    xty = [sum(x[i] * v for x, v in zip(X, yc)) for i in range(d)]
    w = _solve(xtx, xty)
    return {"features": FEATURES, "mean": mu, "sd": sd, "coef": dict(zip(FEATURES, w)), "intercept": ym, "lambda": lam}


def predict(model: dict, f: dict[str, float]) -> float:
    return model["intercept"] + sum(c * (f.get(k, 0.0) - model["mean"][k]) / model["sd"][k]
                                    for k, c in model["coef"].items())


def center_within(y: list[float], groups: list[str]) -> list[float]:
    tot, cnt = {}, {}
    for v, g in zip(y, groups):
        tot[g] = tot.get(g, 0.0) + v
        cnt[g] = cnt.get(g, 0) + 1
    return [v - tot[g] / cnt[g] for v, g in zip(y, groups)]


def fit_cv(rows: list[dict[str, float]], y: list[float], groups: list[str], lams=(1, 3, 10, 30, 100, 300, 1000),
           folds: int = 5, seed: int = 0) -> tuple[dict, dict[float, float]]:
    """Pick lambda by account-grouped K-fold CV (within-account pairwise accuracy), refit on everything.
    `y` should already be centered within account."""
    from harness.evaluator.metrics import pairwise_accuracy

    accts = sorted(set(groups))
    random.Random(seed).shuffle(accts)
    fold_of = {a: k % folds for k, a in enumerate(accts)}
    cv = {}
    for lam in lams:
        pred = [0.0] * len(y)
        for k in range(folds):
            tr = [i for i, g in enumerate(groups) if fold_of[g] != k]
            te = [i for i, g in enumerate(groups) if fold_of[g] == k]
            m = fit([rows[i] for i in tr], [y[i] for i in tr], lam)
            for i in te:
                pred[i] = predict(m, rows[i])
        cv[lam] = pairwise_accuracy(pred, y, groups)
    best = max(cv, key=lambda lam: (round(cv[lam], 4), lam))
    return fit(rows, y, best), cv


# --------------------------------------------------------------------------- runtime

_model: dict | None = None


def load_model(path: Path = MODEL_PATH) -> dict | None:
    global _model
    if _model is None and path.exists():
        _model = json.loads(path.read_text(encoding="utf-8"))
    return _model


def score(text: str) -> float | None:
    """Predicted within-account log engagement lift for a text-only draft; None without a model file."""
    m = load_model()
    return predict(m, extract(text)) if m else None
