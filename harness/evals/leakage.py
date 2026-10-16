"""Leakage probe: does the model already know these posts?

If the model memorized a post and how it performed, a backtest measures recall instead of judgment.
The probe gives the model the first half of each post and asks for the rest word for word. A model
that saw the post in training reproduces it; one that didn't writes something plausible. The control
compares each continuation with other posts from the same account, which measures how much overlap
house style alone produces.
"""

import random
import re

from harness.evals.dataset import Post
from harness.llm import LLM

PROBE_SYSTEM = """You are being tested on whether you remember specific posts from X. Answer from memory only.
If you don't remember a post, say so and still write your best guess, so the guess can be compared with the real text."""

PROBE_PROMPT = """Each item is the opening of a real post from a developer-tools company's X account, published
between 2026-09-15 and 2026-10-16. For each one, write how the post continued, word for word as best you can.
Set "recognized" to true only if you remember this specific post.

POSTS:
{posts}

Return JSON: {{"probes": {{"<id>": {{"recognized": false, "continuation": "..."}}}}}}"""

WORDS = re.compile(r"[a-z0-9]+")
MAX_WORDS = 30
VERBATIM_WORDS = 8


def words(text: str) -> list[str]:
    return WORDS.findall(text.lower())


def lcs_len(a: list[str], b: list[str]) -> int:
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def overlap(truth: str, guess: str) -> float:
    """Longest common word subsequence over the first MAX_WORDS words of the truth, as a share of them."""
    t, g = words(truth)[:MAX_WORDS], words(guess)[:MAX_WORDS]
    return lcs_len(t, g) / len(t) if t else 0.0


def verbatim(truth: str, guess: str, n: int = VERBATIM_WORDS) -> bool:
    """The first n words of the continuation match the real text exactly."""
    t = words(truth)[:n]
    return len(t) == n and words(guess)[:n] == t


def halves(text: str) -> tuple[str, str]:
    """Split at the word nearest the middle, keeping the original spacing of the prefix."""
    tokens = text.split()
    cut = len(tokens) // 2
    return " ".join(tokens[:cut]), " ".join(tokens[cut:])


def pick(posts: list[Post], n: int, seed: int, min_words: int = 20) -> list[Post]:
    """Every eligible Super Viral post first (the likeliest to be memorized), then a random fill."""
    rng = random.Random(seed)
    long = [p for p in posts if len(p.clean_text.split()) >= min_words]
    viral = [p for p in long if p.category == "Super Viral"]
    rest = [p for p in long if p.category != "Super Viral"]
    rng.shuffle(viral)
    rng.shuffle(rest)
    return (viral + rest)[:n]


def probe(llm: LLM, posts: list[Post], batch: int = 10, seed: int = 0) -> dict:
    items = []
    for start in range(0, len(posts), batch):
        chunk = posts[start:start + batch]
        shown = [{"id": f"q{start + i}", "account": p.account, "opening": halves(p.clean_text)[0]}
                 for i, p in enumerate(chunk)]
        data = llm.complete_json(
            PROBE_SYSTEM,
            PROBE_PROMPT.format(posts="\n".join(str(s) for s in shown)),
            mock=lambda shown=shown: {"probes": {s["id"]: {"recognized": False, "continuation": ""} for s in shown}},
        ).get("probes", {})
        for s, p in zip(shown, chunk):
            got = data.get(s["id"], {})
            items.append({"post": p, "recognized": bool(got.get("recognized")),
                          "continuation": str(got.get("continuation", ""))})

    rng = random.Random(seed)
    rows = []
    for it in items:
        p, guess = it["post"], it["continuation"]
        rest = halves(p.clean_text)[1]
        peers = [q for q in posts if q.account == p.account and q.id != p.id] or [q for q in posts if q.id != p.id]
        control = halves(rng.choice(peers).clean_text)[1] if peers else ""
        rows.append({
            "id": p.id, "account": p.account, "category": p.category, "recognized": it["recognized"],
            "overlap": overlap(rest, guess), "control_overlap": overlap(control, guess) if control else None,
            "verbatim": verbatim(rest, guess), "truth": rest, "continuation": guess,
        })
    n = len(rows)
    controls = [r["control_overlap"] for r in rows if r["control_overlap"] is not None]
    return {
        "n_posts": n,
        "n_super_viral": sum(r["category"] == "Super Viral" for r in rows),
        "mean_overlap": sum(r["overlap"] for r in rows) / n if n else None,
        "mean_control_overlap": sum(controls) / len(controls) if controls else None,
        "n_verbatim": sum(r["verbatim"] for r in rows),
        "n_recognized": sum(r["recognized"] for r in rows),
        "n_overlap_over_half": sum(r["overlap"] > 0.5 for r in rows),
        "rows": rows,
    }
