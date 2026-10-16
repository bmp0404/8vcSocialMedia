"""Shared by both lanes: the run context and the Eval step (score, critique, rank, write to memory)."""

import re
from dataclasses import dataclass

from harness import config, memory
from harness.llm import LLM
from harness.models import Action
from harness.qualify import qualify

# Evaluator gates applied after ranking (HARNESS_EVALUATOR=on). The critic owns vetoes; the evaluator only
# adds a hard grounding veto and a quality bar. Softer rubric guardrails (voice / grounding / cringe below
# rubric.FLAG_BELOW) are flags in context["eval"]["flags"], shown in the hand-off, never vetoes.
HARD_GROUNDING = 1.5  # rubric grounding below this (anchor 1: "invents brand facts/stats") -> factual_risk veto
QUALITY_BAR_MIN = 6  # with at least this many live drafts in a lane, drop the tournament's bottom tercile


@dataclass
class LaneContext:
    llm: LLM
    mission: str
    run_id: int
    per_mind: int = 3  # max actions each mind may propose
    limit: int = 5  # reply lane: targets considered; post lane: narratives considered
    said: tuple[str, ...] = ()  # drafts earlier lanes kept in this run (cli fills it); count as already said


def recall(ctx: LaneContext, lane: str, signals) -> dict:
    """memory.recall, plus this run's drafts from earlier lanes as recent texts, so the post lane's minds,
    orient and critic don't restate a reply that is shipping in the same hand-off."""
    mem = memory.recall(lane, context_text=" ".join(s.text for s in signals))
    mem["recent_texts"] = list(dict.fromkeys([*ctx.said, *mem["recent_texts"]]))[:20]
    return mem


def gist(text: str, words: int = 8) -> str:
    """First few words of a signal, for mock drafts."""
    text = re.sub(r"^SAMPLE:\s*", "", text or "").strip()
    return " ".join(text.split()[:words]).rstrip(".,:;!?'\"")


def _live(actions: list[Action]) -> list[Action]:
    return [a for a in actions if not a.vetoed and a.text.strip() and a.type != "follow"]


def _gate(actions: list[Action]) -> tuple[int, int]:
    """Hard grounding veto + tournament quality bar on evaluated drafts. Returns (grounding, quality) counts."""
    grounding = 0
    for a in _live(actions):
        g = (((a.context.get("eval") or {}).get("rubric") or {}).get("scores") or {}).get("grounding")
        if g and g["score"] < HARD_GROUNDING:
            a.vetoed, a.reason = True, "factual_risk"
            a.critique = f"evaluator: rubric grounding {g['score']:.1f}/5 ({g.get('why') or 'invented facts'})"
            grounding += 1
    live = [a for a in _live(actions) if (a.context.get("eval") or {}).get("win_prob") is not None]
    if len(live) < QUALITY_BAR_MIN:
        return grounding, 0
    live.sort(key=lambda a: (a.context["eval"]["win_prob"], a.predicted_score))
    cut = live[: len(live) // 3]
    for a in cut:
        a.vetoed, a.reason = True, "other"
        a.critique = (f"quality bar: bottom tercile of the pairwise tournament "
                      f"(win_prob {a.context['eval']['win_prob']:.2f} of {len(live)} drafts)")
    return grounding, len(cut)


def evaluate(ctx: LaneContext, lane: str, actions: list[Action], mem: dict) -> list[Action]:
    """Eval: panel + critic + rules, then (HARNESS_EVALUATOR=on) the evaluator's panel+pairwise rank and gates,
    then one row per candidate in memory. Returns all, best first. Follows never reach this step."""
    for a in actions:
        a.lane, a.run_id = lane, ctx.run_id
    qualify(ctx.llm, ctx.mission, actions, mem.get("recent_texts", []))
    if config.EVALUATOR and _live(actions):
        from harness.evaluator import EvalContext
        from harness.evaluator import evaluate as rank

        # The reply lane already ran the triage gate before drafting (reply.run), so don't triage again here.
        rank([a for a in actions if a.type != "follow"], EvalContext.of(ctx, lane, triage=False), lane=lane)
        grounding, quality = _gate(actions)
        flagged = sum(bool((a.context.get("eval") or {}).get("flags")) for a in _live(actions))
        print(f"[evaluator] {lane}: kept {len(_live(actions))}; vetoed {grounding} on hard grounding, "
              f"cut {quality} below the quality bar; {flagged} kept draft(s) carry guardrail flags")
        actions.sort(key=lambda a: (not a.vetoed, a.predicted_score), reverse=True)
    memory.save_actions(actions)
    return actions
