"""Find real dev-tool posts similar to a draft, with how they performed, as evidence for the judge.

TF-IDF cosine similarity over the dataset, built once per process and run locally (no API calls).
Callers can exclude accounts, so a backtest on dataset posts can't look up the post's own company.
"""

from functools import lru_cache

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

from harness import dataset

MIN_SIMILARITY = 0.08  # below this, "similar" posts share little more than stopwords
DUPLICATE = 0.95  # treat as the same post


class Index:
    def __init__(self, posts: list[dataset.Post]):
        self.posts = posts
        self.vec = TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True, stop_words="english")
        self.matrix = self.vec.fit_transform([p.clean_text for p in posts])

    def similar(self, text: str, k: int = 3, exclude_accounts: set[str] = frozenset()) -> list[dict]:
        sims = linear_kernel(self.vec.transform([text]), self.matrix).ravel()
        out = []
        for i in np.argsort(-sims):
            if sims[i] < MIN_SIMILARITY or len(out) == k:
                break
            p = self.posts[i]
            if sims[i] >= DUPLICATE or p.account in exclude_accounts:
                continue
            out.append({"text": p.clean_text[:160], "account_size": p.segment, "engagement": p.engagement,
                        "outcome": p.category, "similarity": round(float(sims[i]), 2)})
        return out


@lru_cache(maxsize=1)
def index() -> Index | None:
    try:
        posts = [p for p in dataset.load() if len(p.clean_text) >= 20]
    except FileNotFoundError:
        return None
    return Index(posts)


def similar(text: str, k: int = 3, exclude_accounts: set[str] = frozenset()) -> list[dict]:
    idx = index()
    return idx.similar(text, k, exclude_accounts) if idx else []
