"""Adapt existing evaluation components without changing Action's contract."""

import json
from dataclasses import asdict

from harness.llm import LLM
from harness.models import Action, Narrative, Signal
from harness.qualify import qualify


def panel_evaluate(llm: LLM, mission: str, actions: list[Action],
                   narratives: list[Narrative], signals: list[Signal]) -> tuple[list[Action], dict]:
    return qualify(llm, mission, actions), {"backend": "panel"}


def reference_evaluate(llm: LLM, mission: str, actions: list[Action],
                       narratives: list[Narrative], signals: list[Signal]) -> tuple[list[Action], dict]:
    # The optional generated SDK is only needed when this backend is selected.
    from baml_sdk import CandidatePost, Verdict
    from harness.post_judge.judge import grade_post

    by_id = {n.id: n for n in narratives}
    evidence = {}
    for action in actions:
        if action.type not in {"post", "reply", "quote"}:
            action.vetoed = True
            action.critique = "Reference judge supports post, reply, and quote actions only."
            evidence[action.id] = {"status": "unsupported_action_type"}
            continue
        narrative = by_id.get(action.narrative_id)
        urls = set(narrative.signal_urls if narrative else [])
        if action.target_url:
            urls.add(action.target_url)
        context = {"source_signals": [asdict(s) for s in signals if s.url and s.url in urls],
                   "reply_parent": getattr(action, "target_text", "")}
        result = grade_post(CandidatePost(text=action.text, mission=mission,
                                         context=json.dumps(context), lane=action.type))
        action.predicted_score = result.score_100 / 100
        action.panel = {"reference_judge": action.predicted_score}
        action.vetoed = result.grade.verdict != Verdict.Strong
        action.critique = "; ".join(result.grade.improvements + result.grade.limitations)
        evidence[action.id] = result.model_dump(mode="json")
    kept = sorted((a for a in actions if not a.vetoed), key=lambda a: a.predicted_score, reverse=True)
    return kept, {"backend": "reference", "grades": evidence}
