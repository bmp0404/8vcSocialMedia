"""A virality model fitted on the dev-tool dataset: one more vote next to the audience panel.

Target: each post's log engagement as a z-score *within its own account*. Big accounts get more reach no
matter what they write, so comparing a post with the same account's other posts isolates the writing.
Features: word and character n-grams plus a few shape features (length, lines, questions, numbers).
Media is left out on purpose: drafts have none yet, and it would dominate the fit.

Validated with GroupKFold over accounts, so every score comes from a model that never saw that company.
Two metrics: Spearman of held-out predictions vs. the target, and same-account pair accuracy (viral vs.
flop from one account), the same test the tournament judge gets in `backtest` and `tune`.

`uv run python -m harness train` fits it and saves data/virality_model.joblib (not committed). qualify adds
its percentile to each public draft's panel as "dataset_model".
"""

import random
import re
from collections import defaultdict
from functools import lru_cache

import joblib
import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import Ridge
from sklearn.model_selection import GroupKFold

from harness import config, dataset
from harness.metrics import spearman

MODEL_FILE = "virality_model.joblib"
MIN_POSTS_PER_ACCOUNT = 5
ALPHAS = (1.0, 3.0, 10.0, 30.0)


def model_path():
    return config.DATA_DIR / MODEL_FILE


def _shape(texts: list[str]) -> np.ndarray:
    rows = []
    for t in texts:
        lines = [l for l in t.split("\n") if l.strip()]
        rows.append([
            np.log1p(len(t)), np.log1p(len(t.split())), len(lines), t.count("?") > 0, t.count("!") > 0,
            bool(re.search(r"\d", t)), bool(re.search(r"[^\x00-\x7f]", t)), t.count("@"),
            bool(re.search(r"^\s*([-*•]|\d+[.)])\s", t, re.M)),
        ])
    return np.asarray(rows, dtype=float)


class ViralityModel:
    def __init__(self, alpha: float = 10.0):
        self.alpha = alpha
        self.word = TfidfVectorizer(ngram_range=(1, 2), min_df=3, sublinear_tf=True, max_features=20000)
        self.char = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=5, sublinear_tf=True,
                                    max_features=30000)
        self.ridge = Ridge(alpha=alpha)
        self.shape_mean = self.shape_std = None
        self.train_preds: np.ndarray | None = None
        self.meta: dict = {}

    def _matrix(self, texts: list[str], fit: bool = False):
        shape = _shape(texts)
        if fit:
            self.shape_mean, self.shape_std = shape.mean(0), shape.std(0) + 1e-9
            w, c = self.word.fit_transform(texts), self.char.fit_transform(texts)
        else:
            w, c = self.word.transform(texts), self.char.transform(texts)
        return sparse.hstack([w, c, sparse.csr_matrix((shape - self.shape_mean) / self.shape_std)]).tocsr()

    def fit(self, texts: list[str], y: np.ndarray) -> "ViralityModel":
        self.ridge.fit(self._matrix(texts, fit=True), y)
        self.train_preds = np.sort(self.ridge.predict(self._matrix(texts)))
        return self

    def predict(self, texts: list[str]) -> np.ndarray:
        return self.ridge.predict(self._matrix(texts))

    def percentile(self, texts: list[str]) -> list[float]:
        """0-1: where each text would rank among the training posts."""
        preds = self.predict(texts)
        return [round(float(np.searchsorted(self.train_preds, p) / len(self.train_preds)), 3) for p in preds]

    def top_terms(self, n: int = 15) -> dict[str, list[str]]:
        names = self.word.get_feature_names_out()
        coef = self.ridge.coef_[: len(names)]
        order = np.argsort(coef)
        return {"lifts": [names[i] for i in order[::-1][:n]], "sinks": [names[i] for i in order[:n]]}


def training_set() -> tuple[list[str], np.ndarray, list[str], list[dataset.Post]]:
    """Texts, within-account z-scores, account per row, and the posts themselves."""
    by_acct = defaultdict(list)
    for p in dataset.load():
        if len(p.clean_text) >= 10:
            by_acct[p.account].append(p)
    posts, y = [], []
    for acct, ps in by_acct.items():
        if len(ps) < MIN_POSTS_PER_ACCOUNT:
            continue
        logs = np.array([p.log_engagement for p in ps])
        sd = logs.std() or 1.0
        posts.extend(ps)
        y.extend((logs - logs.mean()) / sd)
    return [p.clean_text for p in posts], np.asarray(y), [p.account for p in posts], posts


def same_account_pairs(posts: list[dataset.Post], n: int = 2000, seed: int = 0) -> list[tuple[int, int]]:
    """(viral index, flop index) pairs from the same account."""
    rng = random.Random(seed)
    by_acct = defaultdict(lambda: ([], []))
    for i, p in enumerate(posts):
        if p.category in dataset.VIRAL:
            by_acct[p.account][0].append(i)
        elif p.category == "Deadweight":
            by_acct[p.account][1].append(i)
    candidates = [(h, f) for hits, flops in by_acct.values() for h in hits for f in flops]
    rng.shuffle(candidates)
    return candidates[:n]


def _oof(texts, y, groups, alpha: float) -> np.ndarray:
    oof = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=5).split(texts, y, groups):
        m = ViralityModel(alpha).fit([texts[i] for i in tr], y[tr])
        oof[te] = m.predict([texts[i] for i in te])
    return oof


def train() -> dict:
    texts, y, groups, posts = training_set()
    pairs = same_account_pairs(posts)
    results = {}
    for alpha in ALPHAS:
        oof = _oof(texts, y, groups, alpha)
        pair_acc = float(np.mean([oof[h] > oof[f] for h, f in pairs])) if pairs else None
        results[alpha] = {"spearman": spearman(list(oof), list(y)), "pair_accuracy": round(pair_acc, 3)}
        print(f"[train] alpha {alpha:>4}: held-out spearman {results[alpha]['spearman']:+.3f}, "
              f"same-account pair accuracy {results[alpha]['pair_accuracy']}")
    best = max(results, key=lambda a: results[a]["spearman"])
    model = ViralityModel(best).fit(texts, y)
    model.meta = {"alpha": best, "n_posts": len(texts), "n_accounts": len(set(groups)),
                  "n_pairs": len(pairs), **results[best], "dataset": str(config.DATASET_PATH.name)}
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, model_path())
    load.cache_clear()
    terms = model.top_terms(12)
    print(f"[train] kept alpha {best}: {model.meta}")
    print(f"[train] words that lift engagement: {', '.join(terms['lifts'])}")
    print(f"[train] words that sink it:         {', '.join(terms['sinks'])}")
    print(f"[train] saved -> {model_path()}")
    return model.meta


@lru_cache(maxsize=1)
def load() -> ViralityModel | None:
    path = model_path()
    return joblib.load(path) if path.exists() else None


def score(texts: list[str]) -> list[float] | None:
    """Percentiles for each text, or None when no model has been trained."""
    model = load()
    return model.percentile(texts) if model and texts else None
