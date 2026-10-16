"""Pairwise logistic regression on sparse feature differences, standard library only.

P(winner beats loser) = sigmoid(w . (x_winner - x_loser)). There is no intercept: swapping the two
items flips the sign, so the model is symmetric by construction, and an item's score is w . x.
Trained with AdaGrad SGD and an L2 penalty applied to the weights each example touches.
"""

import math
import random
from collections import defaultdict
from typing import Sequence

Vector = dict[str, float]


def diff(a: Vector, b: Vector) -> Vector:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) - v
    return {k: v for k, v in out.items() if v != 0.0}


def dot(w: Vector, x: Vector) -> float:
    return sum(w.get(k, 0.0) * v for k, v in x.items())


def _sigmoid(m: float) -> float:
    m = max(-35.0, min(35.0, m))
    return 1.0 / (1.0 + math.exp(-m))


def fit(diffs: Sequence[Vector], l2: float, epochs: int = 25, lr: float = 0.3, seed: int = 0) -> Vector:
    """`diffs` are x_winner - x_loser. Returns the weights."""
    if not diffs:
        raise ValueError("no training pairs")
    w: dict[str, float] = defaultdict(float)
    g2: dict[str, float] = defaultdict(float)
    order = list(range(len(diffs)))
    rng = random.Random(seed)
    for _ in range(epochs):
        rng.shuffle(order)
        for i in order:
            d = diffs[i]
            p = _sigmoid(sum(w[k] * v for k, v in d.items()))
            scale = p - 1.0  # derivative of -log(p) with respect to the margin
            for k, v in d.items():
                g = scale * v + l2 * w[k]
                g2[k] += g * g
                w[k] -= lr * g / (math.sqrt(g2[k]) + 1e-8)
    return {k: v for k, v in w.items() if v != 0.0}


def log_loss(w: Vector, diffs: Sequence[Vector]) -> float:
    return -sum(math.log(max(_sigmoid(dot(w, d)), 1e-12)) for d in diffs) / len(diffs)


def choose_l2(diffs: Sequence[Vector], groups: Sequence[str], grid: Sequence[float] = (1e-4, 1e-3, 1e-2, 1e-1),
              folds: int = 4, seed: int = 0) -> tuple[float, dict]:
    """Pick the L2 strength with the lowest held-out log loss, folding by group so no thread spans folds."""
    keys = sorted(set(groups))
    random.Random(seed).shuffle(keys)
    fold_of = {k: i % folds for i, k in enumerate(keys)}
    scores = {}
    for l2 in grid:
        losses = []
        for f in range(folds):
            train = [d for d, g in zip(diffs, groups) if fold_of[g] != f]
            test = [d for d, g in zip(diffs, groups) if fold_of[g] == f]
            if train and test:
                losses.append(log_loss(fit(train, l2, seed=seed), test))
        scores[l2] = sum(losses) / len(losses)
    best = min(scores, key=scores.get)
    return best, scores
