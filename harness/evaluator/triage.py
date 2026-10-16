"""Cheap triage gate for the reply lane: is this post worth drafting a reply for at all?

Two choice questions per post, batched (~10 posts per call) on EvalContext.triage_tier (claude: the
judge model by default, since Haiku over-declined; or a decision model): the decision (engage / wait /
skip) and a typed reason (models.REASONS, or worth_it). On a decision model `p_engage` is the calibrated
P(engage); on claude it is 1/0 from the hard choice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from harness.evaluator.candidates import EvalContext
from harness.evaluator.decide import Question, decide_batch
from harness.models import Signal

SYSTEM = """You triage X posts for the brand account in the MISSION: should it spend effort drafting a
reply to this post right now? Drafts are scored and vetoed later, so this gate only filters out posts
not worth an attempt.
- engage: the post is on the MISSION's core subjects, or comes from a watchlist account (see
  `watchlist`: relationship and notes) posting about their launch or a take near those subjects, and
  a concrete take, tip, joke or specific congratulation could fit without inventing facts. The MISSION
  asks the account to congratulate peers' launches and riff on their posts; that counts as adding something.
- wait: on-subject, but the thread is still forming or nothing sharp fits yet.
- skip: off the MISSION's subjects (however popular, and even from a watchlist account), a promo with
  nothing to riff on, stale, risky, or a reply would need facts the MISSION and the post don't give.
Read the core subjects broadly: a post about a tool, product or workflow the MISSION names (or its
watchlist peers' agent launches) is on-subject even if it doesn't mention the brand's exact angle.

MISSION:
{mission}"""

DECISION = Question("decision", "Should the brand account draft a reply to this post?", {
    "engage": "on the MISSION's subjects (or a watchlist peer's launch near them) and a take, tip, joke or "
              "specific congratulation could fit",
    "wait": "on-subject, but the thread is still forming or nothing sharp fits yet",
    "skip": "off the MISSION's subjects, a promo with nothing to riff on, stale, risky, or would need invented facts",
})
REASON = Question("reason", "What is the main reason for that decision?", {
    "worth_it": "relevant post where the account can add value now",
    "off_tone": "engaging would push the account off its voice or into a fight",
    "stale": "the conversation is old or over",
    "wrong_target": "wrong audience or account for the brand",
    "nothing_to_add": "relevant, but nothing new or useful to say",
    "factual_risk": "a good reply would need facts the MISSION does not support",
    "other": "anything else",
})
DECLINE = {"wait": "wait", "skip": "no_response"}


@dataclass
class Triage:
    decision: str  # engage | wait | skip
    reason: str  # worth_it or one of models.REASONS
    p_engage: float
    why: str = ""
    probs: dict[str, float] | None = None

    def decline(self) -> tuple[str, str] | None:
        """(models.DECLINE_TYPES type, reason) when the gate says don't draft; None for engage."""
        if self.decision == "engage":
            return None
        return DECLINE.get(self.decision, "no_response"), (self.reason if self.reason != "worth_it" else "nothing_to_add")

    def to_dict(self) -> dict:
        return {"decision": self.decision, "reason": self.reason, "p_engage": round(self.p_engage, 3), "why": self.why}


def _render(s: Signal | dict, note: dict | None) -> str:
    d = s if isinstance(s, dict) else {"author": s.author, "text": s.text, "created_at": s.created_at,
                                       "metrics": {k: v for k, v in (s.metrics or {}).items()
                                                   if k in ("likes", "replies", "reposts", "quotes", "views")}}
    if note:
        d = {**d, "watchlist": note}
    return "POST:\n" + json.dumps(d, ensure_ascii=False, indent=1)


def key_of(s: Signal | dict) -> str:
    if isinstance(s, dict):
        return str(s.get("url") or s.get("id") or s.get("text", "")[:80])
    return s.url or s.id or s.text[:80]


def triage(posts: list[Signal | dict], ectx: EvalContext, backend: str | None = None,
           notes: dict[str, dict] | None = None) -> dict[str, Triage]:
    """{key_of(post): Triage}. `notes` maps the same keys to watchlist info (relationship, notes)."""
    if not posts:
        return {}
    notes = notes or {}
    keys = [key_of(p) for p in posts]
    answers = decide_batch([_render(p, notes.get(k)) for p, k in zip(posts, keys)], [DECISION, REASON],
                           backend or ectx.decider, llm=ectx.llm, system=SYSTEM.format(mission=ectx.mission),
                           tier=ectx.triage_tier)
    out = {}
    for k, ans in zip(keys, answers):
        d, r = ans["decision"], ans["reason"]
        out[k] = Triage(d.choice or "skip", r.choice or "other", d.p("engage"), d.why or r.why, d.probs)
    n = {x: sum(t.decision == x for t in out.values()) for x in ("engage", "wait", "skip")}
    print(f"[triage] {len(out)} posts: " + ", ".join(f"{k}={v}" for k, v in n.items()))
    return out
