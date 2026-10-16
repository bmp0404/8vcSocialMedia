"""Human review as learning signal: verdicts become episode outcomes, edits become preference pairs.

`memory.review_history()` returns every verdict in order. The latest non-pending verdict labels the
episode (approved is a success, killed a failure), the notes travel with it as the reason, and each edit
becomes an EditPair: the draft a mind wrote and the text a human preferred. Edit pairs are grouped into
one procedure note per mind. The labels match Aarav's `harness/reward.py` thresholded at 0.5: approved
(1.0) and approved after an edit (0.7) are successes, killed (0.0) is a failure.
"""

import difflib
import re
from collections import defaultdict
from dataclasses import dataclass, field

from harness.learning.kinds import FAILURE, SUCCESS, EditPair, Episode, Outcome, Procedure, day, slug

VERDICTS = {"approved": SUCCESS, "killed": FAILURE}


@dataclass
class ReviewSummary:
    action_id: str
    final: str = ""  # "approved", "killed", or "" when the latest event reset it to pending
    flips: int = 0  # times the verdict switched between approved and killed
    notes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)  # kill reason codes from the latest verdict
    edits: list[dict] = field(default_factory=list)  # {"event_id", "status", "original", "edited", "note", "at"}


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def summarize(events: list[dict]) -> dict[str, ReviewSummary]:
    """Fold ordered review events into one summary per action."""
    out: dict[str, ReviewSummary] = {}
    last_verdict: dict[str, str] = {}
    for ev in events:
        aid = ev["action_id"]
        s = out.setdefault(aid, ReviewSummary(action_id=aid))
        status = ev["status"]
        if status in VERDICTS:
            if last_verdict.get(aid) and last_verdict[aid] != status:
                s.flips += 1
            last_verdict[aid] = status
            s.final = status
            s.reasons = sorted(set(ev.get("reasons") or []))
        else:
            s.final = ""
            s.reasons = []
        note = (ev.get("note") or "").strip()
        if note and note not in s.notes:
            s.notes.append(note)
        edited, original = _norm(ev.get("edited_text")), _norm(ev.get("original_text"))
        if status in VERDICTS and edited and edited != original:
            if not s.edits or _norm(s.edits[-1]["edited"]) != edited:
                s.edits.append({"event_id": ev.get("id"), "status": status, "original": ev.get("original_text") or "",
                                "edited": ev["edited_text"], "note": note, "at": ev.get("reviewed_at") or ""})
    return out


def _tokens(pattern: str, text: str) -> set[str]:
    return set(re.findall(pattern, text))


def edit_diff(original: str, edited: str) -> dict:
    """What a human changed, word by word, plus the changes the mission rules care about."""
    a, b = original.split(), edited.split()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    removed, added = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("replace", "delete"):
            removed += a[i1:i2]
        if op in ("replace", "insert"):
            added += b[j1:j2]
    tags_a, tags_b = _tokens(r"#\w+", original), _tokens(r"#\w+", edited)
    at_a, at_b = _tokens(r"@\w+", original), _tokens(r"@\w+", edited)
    return {
        "chars_before": len(original),
        "chars_after": len(edited),
        "similarity": round(sm.ratio(), 3),
        "words_removed": removed[:30],
        "words_added": added[:30],
        "hashtags_removed": sorted(tags_a - tags_b),
        "hashtags_added": sorted(tags_b - tags_a),
        "mentions_removed": sorted(at_a - at_b),
        "mentions_added": sorted(at_b - at_a),
        "verify_added": "[VERIFY" in edited and "[VERIFY" not in original,
        "verify_removed": "[VERIFY" in original and "[VERIFY" not in edited,
    }


def procedures(summaries: dict[str, ReviewSummary], rows: list[dict]) -> list[Procedure]:
    """One procedure note per mind: every edit a reviewer made to its drafts, oldest first.

    `review_events` stays the source of truth; these notes are rebuilt from it on every `learn`, with
    stable ids, so rerunning replaces them instead of adding copies.
    """
    actions = {r["action"]["id"]: r["action"] for r in rows}
    by_mind: dict[str, list[EditPair]] = defaultdict(list)
    for aid, s in sorted(summaries.items()):
        action = actions.get(aid, {})
        for e in s.edits:
            by_mind[action.get("mind") or "unknown"].append(
                EditPair(action_id=aid, original=e["original"], edited=e["edited"], verdict=e["status"],
                         note=e["note"], narrative_id=action.get("narrative_id", ""),
                         action_type=action.get("type", ""), diff=edit_diff(e["original"], e["edited"]),
                         at=e["at"])
            )
    out = []
    for mind, pairs in sorted(by_mind.items()):
        pairs.sort(key=lambda p: p.at)
        dates = [day(p.at) for p in pairs if day(p.at)]
        out.append(Procedure(id=f"procedure-review-edits-{slug(mind)}", mind=mind, pairs=pairs,
                             created=dates[0] if dates else "", updated=dates[-1] if dates else ""))
    return out


def run_episodes(rows: list[dict], summaries: dict[str, ReviewSummary], brand: str = "brand") -> list[Episode]:
    """One episode per drafted action, with eval, review and metrics outcomes kept apart.

    An approved draft counts as a success even when a human edited it first; the EditPair carries the
    finer signal. Real metrics only get a label when the recorded metrics include
    `"outcome": "success" | "failure"`, because what counts as success for a small account is Eval's call.
    """
    siblings: dict[tuple, list[dict]] = {}
    for r in rows:
        a = r["action"]
        siblings.setdefault((r["run_id"], a.get("narrative_id", "")), []).append(
            {"id": a["id"], "predicted_score": a.get("predicted_score"), "vetoed": bool(a.get("vetoed"))})
    episodes = []
    for r in rows:
        a = r["action"]
        score = a.get("predicted_score")
        outcomes = [Outcome(label=FAILURE if a.get("vetoed") else "", source="eval",
                            raw="" if score is None else str(score), note=a.get("critique", ""))]
        s = summaries.get(a["id"])
        if s and s.final:
            raw = s.final + (" (edited)" if s.edits else "")
            outcomes.append(Outcome(label=VERDICTS[s.final], source="review", raw=raw, note="; ".join(s.notes),
                                    reasons=s.reasons))
        if r.get("metrics"):
            m = dict(r["metrics"])
            label = m.pop("outcome", "")
            outcomes.append(Outcome(label=label if label in (SUCCESS, FAILURE) else "", source="metrics",
                                    metrics=m, at=r.get("recorded_at") or ""))
        episodes.append(
            Episode(
                id=f"ep-{a['id']}",
                source="run",
                author_username=brand,
                text=a.get("text", ""),
                action_type=a.get("type", "post"),
                created_at=r.get("started_at") or "",
                loop_id=f"run-{r['run_id']}",
                url=a.get("target_url"),
                features={"mind": a.get("mind", ""), "predicted_score": a.get("predicted_score"),
                          "vetoed": bool(a.get("vetoed")), "edited": bool(s and s.edits),
                          "flips": s.flips if s else 0, "media": "none"},
                observation={"narrative_id": a.get("narrative_id", ""), "target_url": a.get("target_url"),
                             "target_handle": a.get("target_handle"), "rationale": a.get("rationale", "")},
                decision={"mind": a.get("mind", ""), "provider": r.get("provider", ""),
                          "draft_meta": a.get("draft_meta") or {},
                          "siblings": [x for x in siblings[(r["run_id"], a.get("narrative_id", ""))]
                                       if x["id"] != a["id"]],
                          "chosen_by_bot": None},
                outcomes=outcomes,
            )
        )
    return episodes


def by_narrative(ep: Episode) -> str:
    """Group our own drafts by the story they answer, so approved and killed drafts are matched on topic."""
    return ep.observation.get("narrative_id") or ep.loop_id
