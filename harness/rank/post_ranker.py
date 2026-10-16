"""The post ranker qualify uses: harness/rank/post.py's TF-IDF ridge, fitted once on the post benchmark's dev
authors and saved with the SHA-256 of its bytes.

Why this ranker. On the benchmark's held-out authors the ridge, cross-fitted over all accounts, scored 0.711
and the LLM panel 0.723, a difference of +0.017 [-0.044, 0.087]; the BAML judge scored 0.639
(data/bench/results/judges-posts.json). The panel costs a call per draft and the ridge costs none.

What is saved. The ridge is fitted on every eligible post by a dev author of the post benchmark, with the
target log engagement z-scored within each account, so it learns what makes a post beat the same account's
other posts. The test authors stay out, so the manifest's test numbers are held out for this exact model.
Its vocabulary keeps only word and character n-grams that MIN_ACCOUNTS or more of those accounts used,
because the repository is public and an n-gram only one account used can be a person's handle or name.
`fit-post` measures what that filter costs and writes it to the manifest. Floats are rounded to DIGITS
significant digits, so the bytes and the hash don't hang on the last bit of a platform's math library.

Files: eval/rankers/post_ridge.json.gz holds the model; post_ridge.manifest.json holds the SHA-256 of the
model's JSON (after gunzip), how it was fitted and what it scored on the test authors. `load()` refuses a
model whose bytes don't hash to the manifest's value. Refit with `uv run python -m harness.rank fit-post`,
which needs the local corpus snapshot (`python -m harness.corpus rebuild`).

What the scores mean. `score` is the predicted within-account z-score of log engagement: above 0 means the
draft looks like the posts that beat their account's typical post. `percentile` places a score among the
training posts' own scores, 0 to 1, for readers that expect a 0-1 number. The model learned from dev-tool
brand accounts only, so only the devtools profile names it (mission/profiles/<name>/qualify.toml).
"""

import gzip
import hashlib
import json
import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from harness import config
from harness.rank import post

SCHEMA = "post_ridge/v1"
MANIFEST_SCHEMA = "post_ridge.manifest/v1"
NAME = "post_ridge"
DIR = config.ROOT / "eval" / "rankers"
MODEL_PATH = DIR / "post_ridge.json.gz"
MANIFEST_PATH = DIR / "post_ridge.manifest.json"
ALPHA = 10.0  # run_posts's ridge strength, the setting behind the 0.711 cross-fitted score
MIN_ACCOUNTS = 5  # the same rule run_posts applies before it publishes a word list
DIGITS = 6
QUANTILES = 100
REFIT = "uv run python -m harness.rank fit-post"


class RankerError(RuntimeError):
    """The saved post ranker is missing, malformed or doesn't match its manifest."""


def _r(x: float) -> float:
    return float(f"{x:.{DIGITS}g}")


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


@dataclass(frozen=True)
class PostRanker:
    model: post.RidgeModel
    sha256: str
    quantiles: tuple[float, ...]
    name: str = NAME

    def score(self, text: str) -> float:
        return self.model.predict(post.clean_text(text))

    def percentile(self, score: float) -> float:
        """Where `score` falls among the training posts' scores, 0 to 1, interpolated between stored quantiles."""
        q = self.quantiles
        if score <= q[0]:
            return 0.0
        if score >= q[-1]:
            return 1.0
        i = bisect_right(q, score)  # q[i - 1] <= score < q[i]
        frac = (score - q[i - 1]) / (q[i] - q[i - 1])
        return round((i - 1 + frac) / (len(q) - 1), 3)

    def describe(self, text: str) -> dict:
        """What qualify stores on a scored draft."""
        s = self.score(text)
        return {"name": self.name, "sha256": self.sha256, "score": round(s, 4), "percentile": self.percentile(s)}


# ---------------------------------------------------------------- the file format


def _vocab_json(t: post.Tfidf) -> dict:
    return {"min_df": t.min_df, "max_features": t.max_features, "idf": {g: _r(v) for g, v in t.idf.items()}}


def to_json(model: post.RidgeModel, quantiles: list[float] | None = None) -> dict:
    out = {"schema": SCHEMA, "intercept": _r(model.intercept),
           "weights": {k: w for k, v in model.weights.items() if (w := _r(v)) != 0.0},
           "word": _vocab_json(model.word), "char": _vocab_json(model.char),
           "shape": {"mean": [_r(v) for v in model.shape_mean], "std": [_r(v) for v in model.shape_std]}}
    if quantiles is not None:
        out["train_quantiles"] = [_r(v) for v in quantiles]
    return out


def _vocab(d: dict, analyzer, prefix: str) -> post.Tfidf:
    t = post.Tfidf(analyzer, d["min_df"], d["max_features"], prefix)
    t.idf = dict(d["idf"])
    return t


def from_json(data: dict) -> tuple[post.RidgeModel, tuple[float, ...]]:
    if data.get("schema") != SCHEMA:
        raise RankerError(f"post ranker schema {data.get('schema')!r}; expected {SCHEMA!r}")
    try:
        model = post.RidgeModel(dict(data["weights"]), float(data["intercept"]), _vocab(data["word"], post._word_grams, "w:"),
                                _vocab(data["char"], post._char_wb, "c:"), list(data["shape"]["mean"]),
                                list(data["shape"]["std"]))
        quantiles = tuple(float(v) for v in data.get("train_quantiles", ()))
    except (KeyError, TypeError, ValueError) as e:
        raise RankerError(f"post ranker JSON is malformed: {e!r}") from e
    return model, quantiles


def load(model_path: Path = MODEL_PATH, manifest_path: Path = MANIFEST_PATH) -> PostRanker:
    """The saved ranker, after checking that its bytes hash to the manifest's SHA-256."""
    for p in (model_path, manifest_path):
        if not p.is_file():
            raise RankerError(f"no post ranker file at {p}; refit with `{REFIT}`")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise RankerError(f"{manifest_path} has schema {manifest.get('schema')!r}; expected {MANIFEST_SCHEMA!r}")
    raw = gzip.decompress(model_path.read_bytes())
    got, want = hashlib.sha256(raw).hexdigest(), str(manifest.get("sha256"))
    if got != want:
        raise RankerError(f"{model_path.name} hashes to {got}, but {manifest_path.name} records {want}: the model "
                          f"changed after it was fitted. Refit with `{REFIT}`, or restore both files from Git")
    model, quantiles = from_json(json.loads(raw))
    if len(quantiles) != QUANTILES + 1 or list(quantiles) != sorted(quantiles) or quantiles[0] == quantiles[-1]:
        raise RankerError(f"{model_path.name} needs {QUANTILES + 1} ascending train_quantiles")
    return PostRanker(model, got, quantiles, model_path.name.removesuffix(".json.gz"))


_NAME = re.compile(r"^[a-z0-9_]+$")


def available() -> list[str]:
    return sorted(p.name.removesuffix(".manifest.json") for p in DIR.glob("*.manifest.json"))


def load_named(name: str) -> PostRanker:
    """The model eval/rankers/<name>.json.gz, checked against <name>.manifest.json."""
    if not _NAME.match(name) or name not in available():
        raise RankerError(f"no post ranker named {name!r} in {DIR}; the ones there are {available()}")
    return load(DIR / f"{name}.json.gz", DIR / f"{name}.manifest.json")


def save(model: post.RidgeModel, quantiles: list[float], manifest: dict, model_path: Path = MODEL_PATH,
         manifest_path: Path = MANIFEST_PATH) -> str:
    """Write the model (gzip with no timestamp) and the manifest with the model's SHA-256. Returns the hash."""
    raw = canonical(to_json(model, quantiles))
    sha = hashlib.sha256(raw).hexdigest()
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
    manifest_path.write_text(json.dumps({"schema": MANIFEST_SCHEMA, "model_file": model_path.name, "sha256": sha,
                                         **manifest}, indent=1) + "\n", encoding="utf-8")
    return sha


def rounded(model: post.RidgeModel) -> post.RidgeModel:
    """The model as it will predict after a save and load: every float rounded to DIGITS digits."""
    return from_json(to_json(model))[0]


def quantiles_of(scores: list[float]) -> list[float]:
    s = sorted(scores)
    return [s[round(i * (len(s) - 1) / QUANTILES)] for i in range(QUANTILES + 1)]


# ---------------------------------------------------------------- fitting


def training_set(corpus) -> tuple[list[str], list[float], list[str]]:
    """(texts, within-account targets, accounts) for every eligible post by a dev author, in status ID order so a
    refit sums floats in the same order."""
    from harness.corpus.bench import author_split

    targets = post.within_account_targets(corpus)
    ids = sorted(s for s, (acct, _) in targets.items() if author_split(acct) == "dev")
    return ([post.clean_text(corpus.get(s).text) for s in ids], [targets[s][1] for s in ids],
            [targets[s][0] for s in ids])


def fit(corpus, alpha: float = ALPHA, min_accounts: int = MIN_ACCOUNTS) -> tuple[post.RidgeModel, list[float]]:
    """The rounded model and its training-score quantiles."""
    texts, y, accounts = training_set(corpus)
    model = rounded(post.ridge_fit(texts, y, alpha, groups=accounts, min_groups=min_accounts))
    return model, [_r(v) for v in quantiles_of([model.predict(t) for t in texts])]
