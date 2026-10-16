"""The human checkpoint's API: verdicts on drafts, abstentions a person can overrule, and the
questions the post lane asks a person ("your take").

Every verdict rewrites out/approved-run<id>.json, the hand-off file for the posting bot. The first
four routes keep the shapes the original dashboard used, so anything reading them still works.
"""

import hashlib
import json
import os
import re

from harness import config, memory
from harness.llm import DEFAULT_MODELS
from harness.models import ACTION_TYPES, Action
from harness.review.routes import HTTPError, Request, route

# Why a person struck a draft, with the label the page shows. The codes match
# memory.REVIEW_REASONS on sam/memory, where set_review(reasons=...) stores them in review_events.
KILL_REASONS = {
    "off_message": "off-message",
    "factually_shaky": "shaky fact",
    "stale_story": "stale story",
    "wrong_target": "wrong target",
    "slop": "reads as AI",
    "tone": "wrong tone",
    "other": "other",
}


def export_approved(run_id: int):
    config.OUT_DIR.mkdir(exist_ok=True)
    path = config.OUT_DIR / f"approved-run{run_id}.json"
    path.write_text(
        json.dumps({"run_id": run_id, "actions": memory.approved_actions(run_id)}, indent=2),
        encoding="utf-8",
    )
    return path


def save_verdict(action_id: str, status: str, text: str | None, note: str, reasons: list) -> int:
    """memory.set_review, passing kill reasons through when the store can keep them.

    sam/memory adds REVIEW_REASONS and a `reasons` argument. On a branch without them the reasons go
    into the note as "Reasons: <label>, <label>." so they still reach learning. Delete the fallback
    once sam/memory is on main.
    """
    if not isinstance(reasons, list) or any(r not in KILL_REASONS for r in reasons):
        raise HTTPError(400, f"reasons must be a list drawn from {sorted(KILL_REASONS)}")
    reasons = list(dict.fromkeys(reasons))
    if hasattr(memory, "REVIEW_REASONS"):
        return memory.set_review(action_id, status, text, note, reasons=reasons)
    if reasons:
        note = f"Reasons: {', '.join(KILL_REASONS[r] for r in reasons)}. {note}".strip()
    return memory.set_review(action_id, status, text, note)


def abstain_reasons() -> dict | None:
    """What each abstention code means, from the decide stage's schema when this branch has it."""
    try:
        from harness.minds.schema import ABSTAIN_REASONS
    except ImportError:
        return None
    return dict(ABSTAIN_REASONS)


@route("GET", r"/api/meta")
def get_meta(req: Request):
    return {
        "provider": config.PROVIDER,
        "model": config.MODEL or DEFAULT_MODELS.get(config.PROVIDER, ""),
        "sources": config.SOURCES,
        # The fan account's handle isn't known yet; the page labels drafts "Our account" without it.
        "account_handle": os.getenv("HARNESS_ACCOUNT_HANDLE", "").lstrip("@") or None,
        "kill_reasons": KILL_REASONS,
        "abstain_reasons": abstain_reasons(),
    }


@route("GET", r"/api/runs")
def get_runs(req: Request):
    return memory.list_runs()


@route("GET", r"/api/runs/(\d+)")
def get_run(req: Request):
    return memory.run_actions(int(req.match[1]))


@route("GET", r"/api/runs/(\d+)/approved")
def get_approved(req: Request):
    return memory.approved_actions(int(req.match[1]))


@route("POST", r"/api/actions/(\w+)/review")
def post_review(req: Request):
    body = req.body
    note = body.get("note", "") or ""
    if not isinstance(note, str):
        raise HTTPError(400, "note must be a string")
    try:
        run_id = save_verdict(req.match[1], body.get("status", ""), body.get("text"), note, body.get("reasons") or [])
    except KeyError:
        raise HTTPError(404, "unknown action") from None
    except ValueError as e:
        raise HTTPError(400, str(e)) from None
    path = export_approved(run_id)
    return {"ok": True, "export": str(path)}


# --- What the harness saw: signals and narratives from the run's trace ------------------------


def load_trace(run_id: int) -> dict | None:
    """out/trace-run<id>.json, written by harness/trace.py (will/ui) when the run was instrumented."""
    path = config.OUT_DIR / f"trace-run{run_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPError(500, f"{path.name} is not valid JSON: {e}") from None


SIGNAL_KEYS = ("id", "source", "kind", "text", "url", "author", "created_at", "metrics")


@route("GET", r"/api/runs/(\d+)/context")
def get_context(req: Request):
    """The narratives and signals a run worked from, so a draft can be shown beside its source post."""
    run_id = int(req.match[1])
    trace = load_trace(run_id)
    if trace is None:
        return {
            "available": False,
            "reason": f"No trace for run {run_id}. Runs started through `python -m harness` with harness/trace.py "
            f"write out/trace-run<id>.json; older runs only have their drafts.",
        }
    return {
        "available": True,
        "started_at": trace.get("started_at"),
        "narratives": trace.get("narratives") or [],
        "signals": [{k: s.get(k) for k in SIGNAL_KEYS if k in s} for s in trace.get("signals") or []],
    }


# --- Decision log: abstentions and questions for a person -------------------------------------


def decisions_path(run_id: int):
    return config.OUT_DIR / f"decisions-run{run_id}.json"


def load_decisions(run_id: int) -> list[dict] | None:
    """The run's decision log, or None when the decide stage didn't write one for this run."""
    path = decisions_path(run_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPError(500, f"{path.name} is not valid JSON: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        raise HTTPError(500, f"{path.name} has no 'decisions' list")
    if data.get("run_id") not in (None, run_id):
        raise HTTPError(500, f"{path.name} says run {data['run_id']}, not {run_id}")
    return data["decisions"]


@route("GET", r"/api/runs/(\d+)/decisions")
def get_decisions(req: Request):
    run_id = int(req.match[1])
    decisions = load_decisions(run_id)
    if decisions is None:
        return {
            "available": False,
            "reason": f"No decision log for run {run_id}. The decide stage (sam/action) writes "
            f"out/decisions-run<id>.json; runs from branches without it have no abstentions to show.",
        }
    return {"available": True, "decisions": decisions}


def _decision(run_id: int, index) -> dict:
    decisions = load_decisions(run_id)
    if decisions is None:
        raise HTTPError(404, f"run {run_id} has no decision log")
    if not isinstance(index, int) or not 0 <= index < len(decisions):
        raise HTTPError(400, f"decision must be an index from 0 to {len(decisions) - 1}")
    return decisions[index]


def _text(body: dict) -> str:
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPError(400, "text must be a non-empty string")
    return text.strip()


def _stable_id(*parts) -> str:
    """Same inputs, same id, so a double click or a resubmitted take replaces instead of duplicating."""
    return hashlib.sha1(":".join(map(str, parts)).encode("utf-8")).hexdigest()[:8]


def add_approved_action(run_id: int, action: dict, note: str) -> dict:
    """Store a person-made action in the run and approve it. Columns follow memory.save_run."""
    with memory.connect() as conn:
        if not conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone():
            raise HTTPError(404, f"unknown run {run_id}")
        conn.execute(
            "INSERT OR REPLACE INTO actions VALUES (?, ?, ?, ?, ?, ?)",
            (action["id"], run_id, action["mind"], action["type"], action["predicted_score"], json.dumps(action)),
        )
    memory.set_review(action["id"], "approved", None, note)
    path = export_approved(run_id)
    return {"ok": True, "action": action, "export": str(path)}


X_STATUS = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/(\w{1,15})/status/\d+")


def handle_from_url(url: str | None) -> str | None:
    m = X_STATUS.match(url or "")
    return m[1] if m else None


def narrative_for_target(run_id: int, url: str | None) -> str:
    """The narrative whose reply targets include `url`, from the run's trace, or "" without one."""
    if not url:
        return ""
    for n in (load_trace(run_id) or {}).get("narratives") or []:
        if any(t.get("url") == url for t in n.get("reply_targets") or []):
            return n.get("id", "")
    return ""


@route("POST", r"/api/runs/(\d+)/overrule")
def post_overrule(req: Request):
    """Ship a draft the harness abstained on. The person picks one of its candidates or writes one."""
    run_id = int(req.match[1])
    body = req.body
    index = body.get("decision")
    d = _decision(run_id, index)
    if d.get("tool") != "abstain":
        raise HTTPError(400, f"decision {index} is a {d.get('tool')!r}, not an abstention")
    text = _text(body)
    lane = d.get("lane", "")
    cands = d.get("candidates") or []
    cand = body.get("candidate")
    picked = cands[cand] if isinstance(cand, int) and 0 <= cand < len(cands) else {}
    # The reply lane answers its post; in the post lane an X post as the opportunity is quoted
    # (the caption path), anything else becomes a plain post. queue.js mirrors this.
    kind = picked.get("type") or ("reply" if lane == "reply" else "quote" if handle_from_url(d.get("opportunity")) else "post")
    if kind not in ACTION_TYPES or kind == "follow":
        raise HTTPError(400, f"can't overrule into a {kind!r}; expected a text action")
    target_url = d.get("opportunity") if kind in ("reply", "quote") else None
    if kind in ("reply", "quote") and not (target_url or "").startswith("http"):
        raise HTTPError(400, f"decision {index} has no target post to reply to")
    reason = d.get("reason", "")
    action = Action(
        type=kind,
        text=text,
        mind=picked.get("mind") or f"{lane or 'unknown'}_lane",
        narrative_id=picked.get("narrative_id") or d.get("narrative_id") or narrative_for_target(run_id, target_url),
        rationale=f"Overruled abstention ({reason}): {d.get('detail', '')}".strip(),
        target_url=target_url,
        target_handle=picked.get("target_handle") or handle_from_url(target_url),
        id=_stable_id("overrule", run_id, index, text),
    ).to_dict()
    released = picked.get("text") == text  # the person shipped the harness's draft unchanged
    action |= {
        "lane": lane,
        "origin": "overrule",
        "overrule": {"decision": index, "candidate": cand, "reason": reason},
        # Learning tells a person's words from the harness's by draft_meta.method.
        "draft_meta": {"method": "released by a person" if released else "written by a person, overruling an abstention",
                       "gate_warnings": gate_warnings(text, kind)},
    }
    note = body.get("note") or f"Overruled the harness's abstention ({reason})."
    return add_approved_action(run_id, action, note)


@route("POST", r"/api/runs/(\d+)/takes")
def post_take(req: Request):
    """A person's answer to the post lane's ask_human question, cleared as a post in their words."""
    run_id = int(req.match[1])
    body = req.body
    index = body.get("decision")
    d = _decision(run_id, index)
    if d.get("tool") != "ask_human":
        raise HTTPError(400, f"decision {index} is a {d.get('tool')!r}, not a question for a person")
    text = _text(body)
    action = Action(
        type="post",
        text=text,
        mind="human",
        narrative_id=d.get("opportunity", ""),
        rationale=f"A person's answer to: {d.get('question', '')}".strip(),
        id=_stable_id("take", run_id, index),
    ).to_dict()
    # The same method string sam/action's lanes write for a take, so learning sees one label.
    action |= {
        "lane": "post",
        "origin": "take",
        "question": d.get("question", ""),
        "draft_meta": {"method": "human take, verbatim", "gate_warnings": gate_warnings(text, "post")},
    }
    return add_approved_action(run_id, action, body.get("note") or "Written by a person.")


def gate_warnings(text: str, kind: str) -> list[dict]:
    """What the slop gate flags in a person's words, kept with the action; [] without the gate."""
    try:
        from harness.minds import gate
    except ImportError:
        return []
    if kind not in gate.KINDS:
        return []
    return [{"rule": f.rule, "detail": f.detail, "start": f.start, "end": f.end} for f in gate.check(text, kind).findings]


# --- Slop gate, as the person types ---------------------------------------------------------


@route("POST", r"/api/gate")
def post_gate(req: Request):
    """sam/action's slop gate on one draft: `{text, kind, context?}`. `context` is the post being
    answered; with it the gate also runs its grounding rules, as the reply lane does."""
    text, kind, context = req.body.get("text"), req.body.get("kind", "post"), req.body.get("context")
    if not isinstance(text, str) or (context is not None and not isinstance(context, str)):
        raise HTTPError(400, "text must be a string, and context a string when given")
    try:
        from harness.minds import gate
        from harness.minds.settings import GateLimits
    except ImportError as e:
        return {
            "available": False,
            "reason": f"The slop gate (harness.minds.gate, from sam/action) isn't on this branch: {e}",
        }
    if kind not in gate.KINDS:
        raise HTTPError(400, f"the gate has no rules for {kind!r}; expected one of {list(gate.KINDS)}")
    res = gate.check(text, kind, context=context or None)
    findings = [
        {"rule": f.rule, "detail": f.detail, "start": f.start, "end": f.end}  # offsets are None for whole-text rules
        for f in res.findings
    ]
    return {"available": True, "passed": res.passed, "limit": GateLimits().max_chars.get(kind, 280), "findings": findings}
