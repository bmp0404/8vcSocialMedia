"""The saved post ranker qualify uses: its hash check, its file format and the vocabulary filter it was fitted with."""

import gzip
import hashlib
import json
import shutil

import pytest

from harness import config
from harness.rank import post, post_ranker
from harness.rank.post import Tfidf


def copy_ranker(tmp_path):
    shutil.copy(post_ranker.MODEL_PATH, tmp_path / post_ranker.MODEL_PATH.name)
    shutil.copy(post_ranker.MANIFEST_PATH, tmp_path / post_ranker.MANIFEST_PATH.name)
    return tmp_path / post_ranker.MODEL_PATH.name, tmp_path / post_ranker.MANIFEST_PATH.name


def test_the_committed_ranker_loads_with_the_hash_its_manifest_records():
    manifest = json.loads(post_ranker.MANIFEST_PATH.read_text(encoding="utf-8"))
    raw = gzip.decompress(post_ranker.MODEL_PATH.read_bytes())
    ranker = post_ranker.load()
    assert ranker.sha256 == manifest["sha256"] == hashlib.sha256(raw).hexdigest()
    assert manifest["fitted_on"]["split"] == "dev" and manifest["params"]["min_accounts"] == post_ranker.MIN_ACCOUNTS
    assert len(ranker.quantiles) == post_ranker.QUANTILES + 1


def test_the_committed_ranker_scores_known_texts_as_it_did_when_fitted():
    """Pins the analyzers and predict() to the saved weights: a code change that moves these numbers needs a refit."""
    r = post_ranker.load()
    assert r.score("Claude Opus 5.5 is out today, and it works with your MCP servers") == pytest.approx(0.488773, abs=1e-5)
    assert r.score("Come see us at our booth during tech week") == pytest.approx(-0.657435, abs=1e-5)
    # URLs are stripped before scoring, as they were from the training posts.
    assert r.score("via ESPN https://espn.com/x") == r.score("via ESPN")


def test_a_model_changed_after_fitting_is_refused(tmp_path):
    model_path, manifest_path = copy_ranker(tmp_path)
    data = json.loads(gzip.decompress(model_path.read_bytes()))
    data["intercept"] += 1.0
    model_path.write_bytes(gzip.compress(post_ranker.canonical(data), mtime=0))
    with pytest.raises(post_ranker.RankerError, match="changed after it was fitted") as e:
        post_ranker.load(model_path, manifest_path)
    assert "post_ridge.json.gz hashes to" in str(e.value) and "fit-post" in str(e.value)


def test_recompressing_the_same_json_keeps_the_hash(tmp_path):
    model_path, manifest_path = copy_ranker(tmp_path)
    model_path.write_bytes(gzip.compress(gzip.decompress(model_path.read_bytes()), compresslevel=1, mtime=12345))
    assert post_ranker.load(model_path, manifest_path).sha256 == post_ranker.load().sha256


@pytest.mark.parametrize("missing", ["model", "manifest"])
def test_a_missing_file_names_the_path_and_the_refit_command(tmp_path, missing):
    model_path, manifest_path = copy_ranker(tmp_path)
    gone = model_path if missing == "model" else manifest_path
    gone.unlink()
    with pytest.raises(post_ranker.RankerError, match="no post ranker file at") as e:
        post_ranker.load(model_path, manifest_path)
    assert str(gone) in str(e.value) and post_ranker.REFIT in str(e.value)


def test_a_manifest_of_another_schema_is_refused(tmp_path):
    model_path, manifest_path = copy_ranker(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps({**manifest, "schema": "post_ridge.manifest/v0"}), encoding="utf-8")
    with pytest.raises(post_ranker.RankerError, match="schema"):
        post_ranker.load(model_path, manifest_path)


def test_save_and_load_round_trip_predicts_like_the_rounded_model(tmp_path):
    texts = ["we shipped the new api today", "join our meetup next week", "the new api is fast", "meetup recap"]
    model = post_ranker.rounded(post.ridge_fit(texts, [1.0, -1.0, 0.8, -0.6], alpha=1.0, word_min_df=1, char_min_df=1))
    quantiles = sorted(model.predict(t) for t in texts)
    quantiles = post_ranker.quantiles_of(quantiles)
    sha = post_ranker.save(model, quantiles, {"note": "test"}, tmp_path / "m.json.gz", tmp_path / "m.manifest.json")
    loaded = post_ranker.load(tmp_path / "m.json.gz", tmp_path / "m.manifest.json")
    assert loaded.sha256 == sha
    for t in texts + ["an unseen text"]:
        assert loaded.score(t) == pytest.approx(model.predict(post.clean_text(t)), abs=1e-12)
    # A second save of the same model writes the same bytes.
    post_ranker.save(model, quantiles, {"note": "test"}, tmp_path / "n.json.gz", tmp_path / "n.manifest.json")
    assert (tmp_path / "n.json.gz").read_bytes() == (tmp_path / "m.json.gz").read_bytes()


def test_percentile_interpolates_between_quantiles_and_clamps_at_the_ends():
    r = post_ranker.PostRanker(model=None, sha256="x", quantiles=tuple(float(i) for i in range(101)))
    assert r.percentile(-5.0) == 0.0 and r.percentile(0.0) == 0.0
    assert r.percentile(50.0) == 0.5 and r.percentile(25.5) == 0.255
    assert r.percentile(100.0) == 1.0 and r.percentile(1e9) == 1.0


def test_the_vocabulary_filter_drops_ngrams_fewer_accounts_used():
    t = Tfidf(lambda s: s.split(), min_df=1, max_features=10, prefix="w:").fit(
        ["launch today", "launch now", "rauchg said", "launch rauchg"], groups=["a", "b", "c", "c"], min_groups=2)
    assert set(t.idf) == {"launch"}  # "rauchg" appears twice but from one account
    with pytest.raises(ValueError, match="2 groups for 3 texts"):
        Tfidf(str.split, 1, 10, "w:").fit(["a", "b", "c"], groups=["x", "y"])


@pytest.mark.skipif(not (config.ROOT / "data" / "corpus" / "raw" / "author_timelines.jsonl.gz").exists(),
                    reason="needs the local corpus snapshot: python -m harness.corpus rebuild")
def test_refitting_on_the_corpus_reproduces_the_committed_model():
    from harness.corpus import load
    model, quantiles = post_ranker.fit(load())
    assert hashlib.sha256(post_ranker.canonical(post_ranker.to_json(model, quantiles))).hexdigest() == post_ranker.load().sha256
