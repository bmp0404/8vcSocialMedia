"""The watcher's hot targets, read as review cards, and the memory records a verdict on one needs.

The watcher (`harness/watcher/hot.py` on sam/watch) writes out/hot/<status_id>.json for each post it
caught under 10 minutes old. A file offers a draft (the continuation or styled arm), a prompt for the
operator's own one-liner (the human arm), or an abstention, which offers nothing and is not a card.

Hot targets have no row in memory until a person gives a verdict. The first verdict creates one run
for the target, naming the account the watcher drafted for, and one action in it, so `learn`, which reads a profile's runs, and `export_approved`
see it like any round's draft. The action's id is `hot-<status_id>`, the id the outcomes ledger gives
the same target (`hot_action_id` in `harness/outcomes/handoff.py` on sam/watch). Both files then name
one action, so `outcomes sync` keeps a single ledger row per target, with its arm.
"""

import json
import sqlite3
from pathlib import Path

from harness import config, memory
from harness.models import Action
from harness.sources.watch import account_handle

# The keys `run_hot` always writes, as `load_hot` in harness/outcomes/handoff.py (sam/watch) requires them.
REQUIRED = ("status_id", "url", "offer", "arm", "profile", "finished_at")
OFFERS = ("draft", "human_prompt")
PREFIX = "hot-"


class HotFileError(ValueError):
    """A hot file is missing a field or isn't JSON. The message names the file."""


def hot_dir() -> Path:
    return config.OUT_DIR / "hot"


def action_id(status_id: str) -> str:
    return f"{PREFIX}{status_id}"


def status_id(action_id: str) -> str | None:
    """The status id inside a hot action id, or None for any other action."""
    return action_id.removeprefix(PREFIX) if action_id.startswith(PREFIX) else None


def load(path: Path) -> dict:
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HotFileError(f"{path.name} is not valid JSON: {e}") from None
    missing = [k for k in REQUIRED if not rec.get(k)]
    if missing:
        raise HotFileError(f"{path.name} has no {', '.join(missing)}; the watcher's run_hot writes them")
    return rec


def offered() -> tuple[list[dict], list[str]]:
    """(every hot file that offers a draft or a prompt, the problems with the rest). A bad file is named,
    not skipped silently, and doesn't hide the good ones."""
    records, problems = [], []
    if not hot_dir().is_dir():
        return records, problems
    for path in sorted(hot_dir().glob("*.json")):
        try:
            rec = load(path)
        except HotFileError as e:
            problems.append(str(e))
            continue
        if rec["offer"] in OFFERS:
            records.append(rec)
    return records, problems


def to_action(rec: dict) -> dict:
    """The memory action for a hot target: a reply whose text is the draft, or empty for the human arm."""
    sid = str(rec["status_id"])
    target = rec.get("target") or {}
    human = rec["arm"] == "human"
    return Action(
        type="reply",
        text=rec.get("draft") or "",
        # The reply lane's own drafts are mind "reply_lane" (harness/minds/lanes.py); a one-liner is a person's.
        mind="human" if human else "reply_lane",
        narrative_id=f"hot-{sid}",  # the narrative id run_hot gives the post
        rationale=(rec.get("plan") or {}).get("intent", ""),
        target_url=rec["url"],
        target_handle=target.get("author"),
        lane="reply",
        target_text=target.get("text", ""),
        draft_meta={**(rec.get("draft_meta") or {}), "method": rec.get("method") or ("human" if human else rec["arm"]),
                    "arm": rec["arm"], "hot_file": f"hot/{sid}.json"},
        id=action_id(sid),
    ).to_dict()


def offer(sid: str) -> dict:
    """The hot file for `sid` that offers something to decide. KeyError when there is no such file."""
    path = hot_dir() / f"{sid}.json"
    if not path.is_file():
        raise KeyError(action_id(sid))
    rec = load(path)
    if rec["offer"] not in OFFERS:
        raise ValueError(f"{path.name} abstained ({rec.get('abstain_reason')}), so there is nothing to decide")
    return rec


def account_of(rec: dict) -> str | None:
    """The account the watcher drafted for. Files from before run_hot wrote "account" name only a profile,
    so for those it is that profile's account now."""
    if "account" in rec:
        return (rec["account"] or "").lstrip("@") or None
    with config.use_profile(rec["profile"]):
        return account_handle()[0] or None


def ensure_action(sid: str) -> None:
    """Create the run and action for hot target `sid` unless they exist. KeyError when no hot file offers it.

    The rows are written here rather than through memory.save_run, which would stamp the run with the
    review server's provider and sources and the review time instead of the watcher's. The run, its account
    and profile rows and the action go in one transaction, so two requests racing on one target can't
    leave a second run: the second action insert fails on the actions primary key and rolls its run back.
    """
    aid = action_id(sid)
    with memory.connect() as conn:
        if conn.execute("SELECT 1 FROM actions WHERE id = ?", (aid,)).fetchone():
            return
    rec = offer(sid)
    action, account = to_action(rec), account_of(rec)
    try:
        with memory.connect() as conn:
            run_id = conn.execute(
                "INSERT INTO runs (started_at, provider, sources) VALUES (?, ?, ?)",
                (rec.get("started_at") or rec["finished_at"], rec.get("provider", ""), "watch"),
            ).lastrowid
            conn.execute("INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?)",
                         (aid, run_id, action["mind"], action["type"], 0.0, json.dumps(action)))
            if account:
                conn.execute("INSERT INTO run_accounts VALUES (?, ?)", (run_id, account))
            conn.execute("INSERT INTO run_profiles VALUES (?, ?)", (run_id, rec["profile"]))
    except sqlite3.IntegrityError:
        pass  # another request created it between the check and the insert; this transaction rolled back
