"""Round end: turn a run's actions into episode notes. No LLM call, safe to rerun.

Each action becomes one `ep-<action_id>` note. <store>/.ingested.json records the
review and outcome state each note was written from, so an action is rewritten
only when a review or outcome changed since the last call.
"""

import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from harness import config, memory
from harness.models import Action
from harness.memstore.note import TYPES, Note, dump, parse, store_root

LEDGER = ".ingested.json"
MAX_TEXT = 2000  # keeps a long draft well under the 8,000-character body cap

try:
    from harness.memstore.store import write_note
except ImportError:  # P1 (store.py) hasn't merged yet; remove this shim once it has.

    def write_note(note: Note) -> Path:
        path = store_root() / TYPES[note.type] / f"{note.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(dump(note))
        return path


@contextmanager
def _provider(provider: str):
    """Point store_root() at the store for this provider: a temp store under mock."""
    saved = config.PROVIDER
    config.PROVIDER = provider.lower()
    try:
        yield
    finally:
        config.PROVIDER = saved


def _episode_id(action_id: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", str(action_id).lower()).strip("-")
    return f"ep-{slug}"[:80]


def _one_line(text: str | None, limit: int = MAX_TEXT) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _outcomes(action_ids: list[str]) -> dict[str, dict]:
    if not action_ids:
        return {}
    marks = ",".join("?" * len(action_ids))
    with memory.connect() as conn:
        rows = conn.execute(
            f"SELECT action_id, recorded_at, metrics FROM outcomes WHERE action_id IN ({marks})",
            action_ids,
        ).fetchall()
    return {r["action_id"]: {"at": r["recorded_at"], "metrics": json.loads(r["metrics"])} for r in rows}


def _run_day(run_id: int) -> str:
    with memory.connect() as conn:
        row = conn.execute("SELECT started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    return (row["started_at"] or "")[:10] if row else date.today().isoformat()


def _episode(action: dict, outcome: dict | None, run_id: int, day: str) -> Note:
    review = action["review"]
    mind, kind, narrative = action["mind"], action["type"], action["narrative_id"]
    stated = f"stated on {day}."
    body = [
        f"- **Mind:** {mind}; {stated}",
        f"- **Type:** {kind}; {stated}",
        f"- **Narrative:** {narrative}; {stated}",
        f"- **Predicted score:** {action['predicted_score']:.2f}; {stated}",
        f"- **Draft text:** {_one_line(action['text'])}; {stated}",
        f"- **Critique:** {_one_line(action.get('critique')) or 'none'}; {stated}",
    ]
    meta = {"source": "run", "tags": [mind, kind], "run_id": str(run_id)}
    if review["reviewed_at"]:
        meta["review_status"] = review["status"]
        line = f"- **Review:** {review['status']}"
        if review["note"]:
            line += f", note: {_one_line(review['note'])}"
        body.append(f"{line}; stated on {review['reviewed_at'][:10]}.")
        if review["text"]:
            body.append(f"- **Edited text:** {_one_line(review['text'])}; stated on {review['reviewed_at'][:10]}.")
    if outcome:
        metrics = " ".join(f"{k}={v}" for k, v in sorted(outcome["metrics"].items()))
        meta["outcome"] = metrics or "none"
        body.append(f"- **Outcome:** {metrics or 'no metrics'}; stated on {outcome['at'][:10]}.")
    today = date.today().isoformat()
    return Note(
        id=_episode_id(action["id"]),
        type="episode",
        title=f"{kind} by {mind} on {narrative}",
        aliases=list(dict.fromkeys(a for a in (mind, kind, narrative) if a)),
        created=today,
        updated=today,
        body="\n".join(body) + "\n",
        meta=meta,
    )


def _read_ledger(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _write_ledger(path: Path, ledger: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ingested.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def round_end(run_id: int, *, provider: str) -> list[str]:
    """Write one episode note per action in the run; return the ids written."""
    actions = memory.run_actions(run_id)
    outcomes = _outcomes([a["id"] for a in actions])
    day = _run_day(run_id)
    written: list[str] = []
    with _provider(provider):
        ledger_path = store_root() / LEDGER
        ledger = _read_ledger(ledger_path)
        try:
            for action in actions:
                outcome = outcomes.get(action["id"])
                state = {
                    "review_at": action["review"]["reviewed_at"],
                    "outcome_at": outcome["at"] if outcome else None,
                    "status": action["review"]["status"],
                }
                if ledger.get(action["id"]) == state:
                    continue
                note = _episode(action, outcome, run_id, day)
                write_note(note)
                ledger[action["id"]] = state
                written.append(note.id)
        finally:
            # Written only after write_note ran, so the ledger never makes an unseeded store look full.
            if written:
                _write_ledger(ledger_path, ledger)
    return written


def _tree(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    return {str(p.relative_to(path)): p.stat().st_mtime for p in path.rglob("*")}


def selftest_episodes() -> None:
    live = config.ROOT / "memory" / "store"
    before = _tree(live)
    saved_db, saved_store = config.DB_PATH, os.environ.get("MEMSTORE_PATH")
    tmp = Path(tempfile.mkdtemp(prefix="memstore-episodes-"))
    config.DB_PATH = tmp / "fixture.db"
    os.environ["MEMSTORE_PATH"] = str(tmp / "store")
    try:
        draft = Action(type="post", text="Line one\nline two — café", mind="hype", narrative_id="clark-logo-three",
                       predicted_score=0.71, critique="Needs a [VERIFY: stat]")
        reply = Action(type="reply", text="Short reply", mind="analyst", narrative_id="clark-logo-three",
                       predicted_score=0.42)
        run_id = memory.save_run([draft, reply])
        memory.record_outcomes([{"action_id": draft.id, "metrics": {"likes": 3, "replies": 1}}])

        first = round_end(run_id, provider="mock")
        assert sorted(first) == sorted([f"ep-{draft.id}", f"ep-{reply.id}"]), first
        assert round_end(run_id, provider="mock") == [], "second call writes nothing"

        path = tmp / "store" / "episodes" / f"ep-{draft.id}.md"
        with path.open(encoding="utf-8", newline="\n") as f:
            note = parse(f.read(), path)
        assert note.type == "episode" and note.meta["source"] == "run"
        assert note.meta["tags"] == ["hype", "post"]
        assert note.aliases == ["hype", "post", "clark-logo-three"]
        assert note.meta["outcome"] == "likes=3 replies=1"
        assert "review_status" not in note.meta, "pending review is not a verdict"
        assert "Line one line two — café" in note.body and "0.71" in note.body

        ledger = _read_ledger(tmp / "store" / LEDGER)
        assert set(ledger) == {draft.id, reply.id}
        assert ledger[draft.id]["outcome_at"] and ledger[reply.id]["outcome_at"] is None

        memory.set_review(reply.id, "killed", note="off brand")
        assert round_end(run_id, provider="mock") == [f"ep-{reply.id}"], "a new review rewrites one note"
        assert round_end(run_id, provider="mock") == []
    finally:
        config.DB_PATH = saved_db
        if saved_store is None:
            os.environ.pop("MEMSTORE_PATH", None)
        else:
            os.environ["MEMSTORE_PATH"] = saved_store
        shutil.rmtree(tmp, ignore_errors=True)
    assert _tree(live) == before, "memory/store/ unchanged"
    print("selftest_episodes: ok")

