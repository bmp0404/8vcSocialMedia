"""The hot path: one fresh post through the reply lane, written to out/hot/<status_id>.json.

A post under HOT_MAX_AGE old is a hot target. Top replies land a median 28 minutes after the post,
53 of 72 within an hour (action/DESIGN.md), so the draft has to exist while the thread is young.
The path skips observe's batch and orient and runs only the reply lane's own functions on this one
post: `lanes.plan_targets` (reply or abstain, and with what intent), then `lanes.answer_target`
(the page's real replies, the writer, the slop gate and the two-order veto). Nothing here forks them.

Two inputs are adapted rather than reused:
- The arm (`arms.py`) forces the writer by setting `min_page_replies` on a copy of ActionConfig:
  0 always continues the page, `page_replies + 1` never does. `answer_target` reads only that knob
  to choose, so the lane code stays as it is. A rival thread under `rival_thread_mode = "banter"`
  still gets the banter writer whatever the arm; the file's `method` says what actually ran.
- The thread is read through the watcher's XReader, inside the shared fxtwitter throttle, instead
  of `thread.fetch_replies`, which keeps its own per-process limit.
- The same copy sets `veto_parallel_orders`, so the veto's forward and reversed passes run at once.

There is no orient narrative, so the facts block the gate and veto read is the mission plus this
post. The human arm runs the plan, so an off-mission post is still abstained on, and then asks the
operator for a one-liner without showing any draft.
"""

import json
import os
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import config
from harness.llm import LLM
from harness.minds import lanes, routing, thread, veto, writer
from harness.minds.settings import ActionConfig
from harness.models import Narrative
from harness.sources.watch import account_handle
from harness.watcher import modelrec
from harness.watcher.arms import arm_for

HOT_MAX_AGE = timedelta(minutes=10)


def is_hot(created_at: datetime, seen_at: datetime, max_age: timedelta = HOT_MAX_AGE) -> bool:
    """True when the post was under `max_age` old when seen. A post dated after `seen_at` (clock skew) is fresh."""
    return seen_at - created_at < max_age


def target_for(post, now: datetime) -> dict:
    """The reply-target dict the lane reads (Narrative.reply_targets shape), built from an xreader XPost."""
    return {
        "url": post.url, "author": post.author_handle, "text": post.text,
        "created_at": post.created_at.isoformat(), "conversation_id": post.id,
        "age_minutes": round(max((now - post.created_at).total_seconds(), 0) / 60, 2),
        "likes": post.likes, "reposts": post.reposts, "replies": post.replies, "quotes": post.quotes,
        "views": post.views, "author_followers": post.author_followers, "thread": [],
    }


def reader_fetch(reader):
    """A `fetch` for `answer_target` that reads the conversation through `reader`. Same shape as
    thread.fetch_replies: direct replies with words in them, most-liked first."""

    def fetch(url: str) -> list[dict]:
        sid = thread.status_id(url)
        try:
            page = reader.conversation(sid, "likes")
        except RuntimeError as e:  # xreader's FetchError
            raise thread.ThreadUnavailable(f"fxtwitter conversation {sid}: {e}") from e
        out = []
        for r in page:
            text = thread.clean(r.text)
            # A reply whose parent link fxtwitter dropped counts as direct, as Observe's thread_replies does.
            if r.replying_to_status in (None, sid) and r.id != sid and thread.usable(text):
                out.append({"text": text, "likes": r.likes, "followers": r.author_followers, "author": r.author_handle})
        return sorted(out, key=lambda r: -r["likes"])

    return fetch


def hot_path(out_dir: Path, status_id: str) -> Path:
    return out_dir / f"{status_id}.json"


def _human_prompt(author: str, intent: str, account: str) -> str:
    who = f"@{account}" if account else "the account"
    return (f"Reply to @{author}'s post from {who} with a one-liner of your own"
            + (f". The plan's read on a good reply: {intent}" if intent else "") + ". Post it by hand, soon.")


def run_hot(llm: LLM, profile: str, post, *, reader, out_dir: Path, cfg: ActionConfig | None = None,
            seed: str, detected_at: datetime, clock=lambda: datetime.now(timezone.utc)) -> dict:
    """Plan, write, gate and veto one post under `profile`; write out_dir/<status_id>.json and return its content."""
    cfg = cfg or ActionConfig()
    t0, started_at = time.monotonic(), clock()
    arm = arm_for(post.id, seed)
    target = target_for(post, started_at)
    narrative = Narrative(id=f"hot-{post.id}", title=f"@{post.author_handle} just posted", summary=post.text)
    record = {"status_id": post.id, "url": post.url, "profile": profile, "arm": arm, "arm_seed": seed,
              "provider": "mock" if routing.is_mock(llm) else type(llm).__name__,
              "target": lanes.target_meta(target), "plan": None, "plan_error": "", "offer": "abstain",
              "draft": None, "prompt": None, "abstain_reason": None, "abstain_detail": None, "method": None,
              "decision": None, "draft_meta": None}
    with config.use_profile(profile), modelrec.recording() as calls:
        mission, lessons, framing = config.mission(), config.lessons(), writer.load_framing()
        account, _ = account_handle()
        plans, plan_error = lanes.plan_targets(llm, mission, lessons, [target])
        t_plan = time.monotonic()
        plan = plans.get(post.url)
        record.update(plan=plan, plan_error=plan_error)
        if plan is None:
            record.update(abstain_reason="no_angle", abstain_detail=plan_error or "the plan skipped this post")
        elif plan["tool"] == "abstain":
            record.update(abstain_reason=plan["reason"], abstain_detail=plan.get("detail", ""))
        elif arm == "human":
            record.update(offer="human_prompt", prompt=_human_prompt(post.author_handle, plan.get("intent", ""), account))
        else:
            arm_cfg = replace(cfg, min_page_replies=0 if arm == "continuation" else cfg.page_replies + 1,
                              veto_parallel_orders=True)
            grounding = None if routing.is_mock(llm) else veto.grounding_checker(cfg)
            decision, action = lanes.answer_target(
                llm, mission, lanes.facts_block(mission, [narrative]), {**target, "narrative": narrative}, arm_cfg,
                reader_fetch(reader), plan.get("intent", ""), grounding, side=plan.get("side", "neutral"),
                framing=framing)
            record["decision"] = asdict(decision)
            if action is None:
                record.update(abstain_reason=decision.reason, abstain_detail=decision.detail)
            else:
                record.update(offer="draft", draft=action.text, method=action.draft_meta.get("method"),
                              draft_meta=action.draft_meta)
        t_end = time.monotonic()
    finished_at = clock()
    record.update(
        models=calls, detected_at=detected_at.isoformat(), started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        timings={"plan_s": round(t_plan - t0, 2), "draft_s": round(t_end - t_plan, 2), "total_s": round(t_end - t0, 2),
                 "detected_to_finished_s": round((finished_at - detected_at).total_seconds(), 2),
                 "post_age_at_detection_s": round((detected_at - post.created_at).total_seconds(), 1),
                 "post_age_at_finish_s": round((finished_at - post.created_at).total_seconds(), 1)},
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    path = hot_path(out_dir, post.id)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)  # the file appearing is the commit point a restart checks
    return record
