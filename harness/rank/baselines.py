"""Cheap baselines for the reply benchmark. A scorer counts as better only if it beats the best of these.

Each baseline is one number per reply. Its direction (does more help or hurt?) is fitted on the
training pairs; time of day is a table of training win rates per four-hour UTC block.
"""

import math
from collections import Counter
from typing import Callable

from harness.corpus import Corpus
from harness.corpus.bench import Pair, reply_body
from harness.corpus.stats import pair_credit

Feature = Callable[[object, object], float]  # (reply item, root item) -> value


def _minutes(item, root) -> float:
    return (item.created_at - root.created_at).total_seconds() / 60


RAW: dict[str, Feature] = {
    "constant": lambda it, root: 0.0,
    "followers": lambda it, root: math.log1p(it.author_followers or 0),
    "verified": lambda it, root: 1.0 if it.author_verified else 0.0,
    "delay": lambda it, root: math.log1p(max(_minutes(it, root), 0.0)),
    "length": lambda it, root: float(len(reply_body(it.text))),
    "media_or_link": lambda it, root: 1.0 if it.media_types or it.n_links or "http" in it.text else 0.0,
}


def _hour_block(it) -> int:
    return it.created_at.hour // 4


def fit_baselines(train: list[Pair], corpus: Corpus) -> dict[str, Callable[[str], float]]:
    """Name -> scorer over status IDs, with every direction and table learned from `train` only."""
    scorers = {}
    for name, f in RAW.items():
        def raw(sid, f=f):
            it = corpus.get(sid)
            return f(it, corpus.get(it.thread_root))
        if name == "constant":
            scorers[name] = raw
            continue
        acc = sum(pair_credit(raw(p.winner), raw(p.loser)) for p in train) / len(train)
        sign = 1.0 if acc >= 0.5 else -1.0
        scorers[name] = lambda sid, raw=raw, sign=sign: sign * raw(sid)
    wins, seen = Counter(), Counter()
    for p in train:
        for sid, won in ((p.winner, 1), (p.loser, 0)):
            b = _hour_block(corpus.get(sid))
            wins[b] += won
            seen[b] += 1
    rate = {b: (wins[b] + 1) / (seen[b] + 2) for b in range(6)}
    scorers["time_of_day"] = lambda sid: rate[_hour_block(corpus.get(sid))]
    return scorers
