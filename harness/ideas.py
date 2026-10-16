"""Idea bank: run the pipeline several times and append every new, usable post to out/ideas.csv.

Each round:
- the minds see the ideas already banked and are told to find new angles, so rounds don't repeat
  the same metaphor;
- drafts that are vetoed, over 280 characters, or that invent a named character are skipped;
- drafts too similar to a banked idea (TF-IDF cosine >= DUPLICATE) are skipped;
- the rest are appended with their scores, so the file only grows.

On first use the bank is seeded from out/coordigent-suggested-posts.csv when that exists, so earlier
picks count for de-duplication.
"""

import argparse
import csv
import re
from datetime import datetime

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from harness import config, memory, virality
from harness.export import _public

DUPLICATE = 0.45
NAMES = re.compile(r"Eager One|Waiter|Rewriter|Never Asks|Last Gasp")
COLUMNS = ["added_at", "source", "type", "text", "reply_or_quote_to", "chars", "mind", "panel_score",
           "dataset_model", "elo", "critic_note", "why", "action_id"]


def bank_path():
    return config.OUT_DIR / "ideas.csv"


def load_bank() -> list[dict]:
    path = bank_path()
    if path.exists():
        return list(csv.DictReader(open(path, encoding="utf-8-sig")))
    seed = config.OUT_DIR / "coordigent-suggested-posts.csv"
    rows = []
    if seed.exists():
        for r in csv.DictReader(open(seed, encoding="utf-8-sig")):
            rows.append({c: r.get(c, "") for c in COLUMNS} | {"added_at": "seed"})
        _write(rows, mode="w")
    return rows


def _write(rows: list[dict], mode: str = "a") -> None:
    path = bank_path()
    new_file = mode == "w" or not path.exists()
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w" if new_file else "a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new_file:
            w.writeheader()
        w.writerows(rows)


def _longest_post(text: str) -> int:
    return max(len(p.strip()) for p in re.split(r"^\s*---\s*$", text, flags=re.M))


def _avoid_note(bank: list[dict], n: int = 60) -> str:
    openers = [r["text"].strip().split("\n")[0][:110] for r in bank[-n:]]
    return ("\n\nIDEAS ALREADY IN THE BANK (find different angles, metaphors and formats; don't rework these):\n"
            + "\n".join(f"- {o}" for o in openers)) if openers else ""


def _new_rows(run_id: int, bank: list[dict]) -> tuple[list[dict], dict]:
    skipped = {"vetoed": 0, "length": 0, "names": 0, "duplicate": 0}
    candidates = []
    for a in memory.run_actions(run_id):
        if not _public(a) or not a.get("text"):
            continue
        if a.get("vetoed"):
            skipped["vetoed"] += 1
        elif _longest_post(a["text"]) > 280:
            skipped["length"] += 1
        elif NAMES.search(a["text"]):
            skipped["names"] += 1
        else:
            candidates.append(a)

    kept, texts = [], [r["text"] for r in bank]
    for a in candidates:
        if texts:
            vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, stop_words="english").fit(texts + [a["text"]])
            if cosine_similarity(vec.transform([a["text"]]), vec.transform(texts)).max() >= DUPLICATE:
                skipped["duplicate"] += 1
                continue
        texts.append(a["text"])
        kept.append(a)

    scores = virality.score([a["text"] for a in kept]) or [""] * len(kept)
    now = datetime.now().isoformat(timespec="seconds")
    rows = [{
        "added_at": now, "source": f"run {run_id} #{a.get('rank') or '-'}", "type": a["type"], "text": a["text"],
        "reply_or_quote_to": a.get("target_url") or (f"@{a['target_handle']}" if a.get("target_handle") else ""),
        "chars": _longest_post(a["text"]), "mind": a.get("variant", a["mind"]), "panel_score": a.get("predicted_score", ""),
        "dataset_model": s, "elo": a.get("elo") or "", "critic_note": a.get("critique", ""), "why": a.get("rationale", ""),
        "action_id": a["id"],
    } for a, s in zip(kept, scores)]
    return rows, skipped


def generate(run, run_args: argparse.Namespace, rounds: int) -> None:
    """`run` is cli.run; run_args are its parsed arguments."""
    bank = load_bank()
    print(f"[ideas] bank starts with {len(bank)} ideas -> {bank_path()}")
    for i in range(1, rounds + 1):
        print(f"\n[ideas] round {i}/{rounds}")
        run_args.avoid = _avoid_note(bank)
        try:
            run(run_args)
        except Exception as e:  # noqa: BLE001 - a failed round shouldn't end the batch
            print(f"[ideas] round {i} failed: {e}")
            continue
        rows, skipped = _new_rows(memory.list_runs()[0]["id"], bank)
        _write(rows)
        bank.extend(rows)
        print(f"[ideas] round {i}: +{len(rows)} ideas (skipped {skipped}); bank now {len(bank)}")
    print(f"\n[ideas] done: {len(bank)} ideas in {bank_path()}")
