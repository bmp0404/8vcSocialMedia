"""Keyword search over the markdown store. Reads note files directly; never writes."""

import re
from pathlib import Path

from harness.memstore.note import TYPES, Note, parse, store_root

STOPWORDS = frozenset(
    """a an and are as at be but by for from has have how i if in into is it its of on or our so
    than that the their them then there these they this to was we were what when where which who
    why will with you your""".split()
)
SNIPPET_CHARS = 200
MAX_SNIPPETS = 3
MAX_BODY_LINE_POINTS = 3
EPISODE_WEIGHT = 0.5


def tokenize(text: str) -> list[str]:
    """Lowercase, split on [^a-z0-9+#.]+, drop stopwords. Edge dots are trimmed so 'launch.' is 'launch'."""
    tokens = (t.strip(".") for t in re.split(r"[^a-z0-9+#.]+", text.lower()))
    return [t for t in tokens if t and t not in STOPWORDS]


def phrases(text: str) -> list[str]:
    """The distinct 1-grams and 2-grams of a query, in order."""
    tokens = tokenize(text)
    grams = tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
    return list(dict.fromkeys(grams))


def _word_re(phrase: str) -> re.Pattern:
    # Whole word: no letter or digit on either side, so "art" never matches "start".
    words = r"[^a-z0-9]+".join(re.escape(w) for w in phrase.split())
    return re.compile(rf"(?<![a-z0-9]){words}(?![a-z0-9])")


def iter_notes(types: list[str] | None = None) -> list[Note]:
    """Every parseable note in the store, optionally of the given types. A missing store gives []."""
    root = store_root()
    notes = []
    for type_, folder in TYPES.items():
        if types is not None and type_ not in types:
            continue
        for path in sorted(Path(root, folder).glob("*.md")):
            try:
                notes.append(parse(path.read_text(encoding="utf-8"), path))
            except Exception as e:
                print(f"[memstore] skipping {path.name}: {e}")
    return notes


def score_note(note: Note, query_phrases: list[str]) -> tuple[float, list[str]]:
    """Score one note against query phrases. Returns (score, matching body lines)."""
    names = {" ".join(tokenize(n)) for n in [note.id, *note.aliases]}
    title = note.title.lower()
    lines = [line.strip() for line in note.body.splitlines() if line.strip()]
    lowered = [line.lower() for line in lines]

    score = 0.0
    matched = set()
    hit_lines = set()
    for phrase in query_phrases:
        pattern = _word_re(phrase)
        if phrase in names:
            score += 10
            matched.add(phrase)
        if pattern.search(title):
            score += 4
            matched.add(phrase)
        for i, line in enumerate(lowered):
            if pattern.search(line):
                hit_lines.add(i)
                matched.add(phrase)
    score += min(len(hit_lines), MAX_BODY_LINE_POINTS)
    score += 2 * len(matched)
    if note.type == "episode":
        score *= EPISODE_WEIGHT
    snippets = [lines[i][:SNIPPET_CHARS] for i in sorted(hit_lines)[:MAX_SNIPPETS]]
    return score, snippets


def search(query: str, k: int = 10, types: list[str] | None = None) -> list[dict]:
    """Ranked keyword search: [{"id", "type", "title", "score", "snippets"}], best first."""
    query_phrases = phrases(query)
    if not query_phrases:
        return []
    hits = []
    for note in iter_notes(types):
        score, snippets = score_note(note, query_phrases)
        if score > 0:
            hits.append((note, score, snippets))
    hits.sort(key=lambda h: h[0].id)
    hits.sort(key=lambda h: (h[1], h[0].updated), reverse=True)
    return [
        {"id": n.id, "type": n.type, "title": n.title, "score": s, "snippets": snips}
        for n, s, snips in hits[:k]
    ]


def selftest_search() -> None:
    """Acceptance checks 4 and 5 against a temp MEMSTORE_PATH of hand-made notes."""
    import os
    import tempfile

    from harness.memstore.prompt import context_for, prompt_block

    def put(root: Path, type_: str, id_: str, title: str, aliases: str, body: str, extra: str = "") -> None:
        folder = Path(root, TYPES[type_])
        folder.mkdir(parents=True, exist_ok=True)
        text = (
            f"---\nid: {id_}\ntype: {type_}\ntitle: {title}\naliases: [{aliases}]\n"
            f"created: 2026-10-01\nupdated: 2026-10-15\n{extra}---\n{body}\n"
        )
        Path(folder, f"{id_}.md").write_text(text, encoding="utf-8", newline="\n")

    saved = os.environ.get("MEMSTORE_PATH")
    try:
        with tempfile.TemporaryDirectory(prefix="memstore-search-") as tmp:
            # 5. Empty or missing store.
            os.environ["MEMSTORE_PATH"] = str(Path(tmp, "missing"))
            assert prompt_block() == "", "prompt_block on a missing store"
            assert context_for("") == "", "context_for('') on a missing store"
            assert search("launch") == [], "search on a missing store"
            empty = Path(tmp, "empty")
            empty.mkdir()
            os.environ["MEMSTORE_PATH"] = str(empty)
            assert prompt_block() == "" and context_for("") == "", "prompt text on an empty store"

            root = Path(tmp, "store")
            os.environ["MEMSTORE_PATH"] = str(root)
            put(root, "lesson", "demo-video-launches", "Example: demo videos on launch day", "launch, ship, release",
                "- **Example:** a made-up lesson about demo clips; stated on 2026-10-15.",
                "confidence: 0.8\nevidence: [post-1, post-2, post-3]\n")
            put(root, "entity", "changelog-notes", "Changelog notes", "changelog",
                "- **Note:** we mention the launch in passing; stated on 2026-10-15.\n- **Note:** start small.")
            put(root, "episode", "post-1", "Dataset post about a launch", "launch",
                "- **Text:** launch thread; stated on 2026-10-15.", "source: dataset\n")
            put(root, "procedure", "no-invented-stats", "Never invent stats", "stats, numbers",
                "- **Rule:** write [VERIFY: ...] instead of a number; stated on 2026-10-15.")
            put(root, "lesson", "weak-lesson", "Thin lesson", "thin",
                "- **Example:** too little evidence; stated on 2026-10-15.", "confidence: 0.9\nevidence: [post-1]\n")

            # 4. Search.
            hits = search("launch")
            ids = [h["id"] for h in hits]
            assert ids[0] == "demo-video-launches", f"alias match should rank first: {ids}"
            assert ids[-1] == "changelog-notes", f"body-only match should rank last: {ids}"
            assert all(set(h) == {"id", "type", "title", "score", "snippets"} for h in hits), "result keys"
            assert "changelog-notes" not in [h["id"] for h in search("art")], "'art' must not match 'start'"
            assert [h["id"] for h in search("start")] == ["changelog-notes"], "'start' matches whole word"
            assert {h["type"] for h in search("launch", types=["entity"])} == {"entity"}, "types filter"
            assert search("the and of") == [], "stopword-only query"
            assert all(len(s) <= SNIPPET_CHARS for h in hits for s in h["snippets"]), "snippet length"

            # 5. Prompt text on a populated store.
            block = prompt_block()
            assert block and len(block) <= 6000, "prompt_block non-empty and capped"
            assert "Never invent stats" in block and "demo videos" in block, "procedure and confident lesson"
            assert "Thin lesson" not in block, "lesson with under 3 evidence ids is excluded"
            assert block.index("Never invent stats") < block.index("demo videos"), "procedures come first"
            assert len(prompt_block(max_chars=50)) <= 50, "custom cap"
            ctx = context_for("launch")
            assert ctx.startswith("### Example: demo videos on launch day (lesson, demo-video-launches)"), ctx
            assert len(ctx) <= 2000, "context_for cap"
    finally:
        if saved is None:
            os.environ.pop("MEMSTORE_PATH", None)
        else:
            os.environ["MEMSTORE_PATH"] = saved
    print("[memstore] selftest_search ok")
