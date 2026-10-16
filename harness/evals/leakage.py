"""Leakage probe: does the model already know these posts?

If the model memorized a post and how it performed, a backtest measures recall instead of judgment.
The probe gives the model the first half of each post and asks for the rest word for word. A model
that saw the post in training reproduces it; one that didn't writes something plausible. The result
is the count of verbatim continuations and of posts the model claims to recognize. Word overlap with
the real second half isn't reported: a post's second half shares names with its first, so overlap
measures that carry-over, not memory.
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
VERBATIM_WORDS = 8


def words(text: str) -> list[str]:
    return WORDS.findall(text.lower())


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


def probe(llm: LLM, posts: list[Post], batch: int = 10) -> dict:
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

    rows = []
    for it in items:
        p, guess = it["post"], it["continuation"]
        rest = halves(p.clean_text)[1]
        rows.append({"id": p.id, "account": p.account, "category": p.category, "recognized": it["recognized"],
                     "verbatim": verbatim(rest, guess), "truth": rest, "continuation": guess})
    return {
        "n_posts": len(rows),
        "n_super_viral": sum(r["category"] == "Super Viral" for r in rows),
        "n_verbatim": sum(r["verbatim"] for r in rows),
        "n_recognized": sum(r["recognized"] for r in rows),
        "rows": rows,
    }
