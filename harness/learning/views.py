"""Read-only views of memory for people: the review dashboard and the operator UI.

These shapes are the contract with `harness/review/` and the UI worker. Note fields may change as the
store evolves; these functions keep their output keys stable, so the UI reads them instead of raw notes.
"""

from datetime import date

from harness.learning.kinds import FAILURE, SUCCESS, Lesson, Procedure, Reflection, heldout_sentence, load, rule_text
from harness.learning.ranking import is_stale, today_utc


def lesson_rows(store, today: date | None = None) -> list[dict]:
    """Every lesson, active ones first, then by effect."""
    today = today or today_utc()
    rows = []
    for m in store.list_memories("lesson"):
        lesson: Lesson = load(m)
        outcome = FAILURE if lesson.polarity == "avoid" else SUCCESS
        rows.append({
            "id": lesson.id,
            "text": lesson.text,
            "polarity": lesson.polarity,
            "status": "stale" if lesson.status == "active" and is_stale(m, today) else lesson.status,
            "scope": lesson.scope,
            "domain": lesson.domain,
            "effect": lesson.confidence,
            "evidence_sentence": heldout_sentence(lesson.last_test, outcome),
            "tested_as": rule_text(lesson.rule) or lesson.predicate,
            "support": lesson.support,
            "contradict": lesson.contradict,
            "inconclusive": lesson.inconclusive,
            "valid_until": lesson.valid_until,
            "evidence": lesson.evidence,
            "reflections": lesson.reflections,
            "updated": lesson.updated,
        })
    rows.sort(key=lambda r: (r["status"] != "active", -r["effect"], r["id"]))
    return rows


def reflection_rows(store) -> list[dict]:
    """Every reflection, newest first: supported, rejected and inconclusive alike."""
    rows = []
    for m in store.list_memories("reflection"):
        r: Reflection = load(m)
        rows.append({
            "id": r.id,
            "claim": r.claim,
            "outcome": r.outcome,
            "status": r.status,
            "predicate": r.predicate,
            "tested_as": rule_text(r.rule) or r.predicate,
            "evidence": r.evidence,
            "contrast": r.contrast,
            "evidence_sentence": heldout_sentence(r.heldout, r.outcome),
            "p_value": r.heldout.get("p_value"),
            "group": r.group,
            "domain": r.domain,
            "created": r.created,
        })
    rows.sort(key=lambda r: (r["created"], r["id"]), reverse=True)
    return rows


def procedure_rows(store) -> list[dict]:
    """Per mind, every edit a reviewer made: the draft, the kept text, the verdict, the note and the diff."""
    out = []
    for m in store.list_memories("procedure"):
        p: Procedure = load(m)
        out.append({"id": p.id, "mind": p.mind, "updated": p.updated,
                    "pairs": [{"action_id": e.action_id, "original": e.original, "edited": e.edited,
                               "verdict": e.verdict, "note": e.note, "diff": e.diff, "at": e.at} for e in p.pairs]})
    return sorted(out, key=lambda r: r["mind"])
