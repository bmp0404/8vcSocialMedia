"""Pre-posting evaluator: rubric judge, pairwise tournament ranker and triage gate.

    from harness.evaluator import evaluate, triage, EvalContext
    ranked = evaluate(actions, lane_ctx, lane="reply")   # best first; sets predicted_score

All fast decisions go through `decide()` (decide.py), which runs on Claude (Haiku / the configured
judge model) or a purpose-built decision model (HARNESS_DECIDER=jev | openai_decisions).
Validate with `scripts/backtest_eval.py --feed <path>`.
"""

from harness.evaluator.candidates import Candidate, EvalContext, audience_from_mission
from harness.evaluator.decide import Decision, Question, available_backends, backend_name, decide, decide_batch
from harness.evaluator.pipeline import Ranked, evaluate
from harness.evaluator.rubric import CRITERIA, RubricResult, rubric
from harness.evaluator.tournament import Tournament, run_grouped, run_tournament
from harness.evaluator.triage import Triage, triage

__all__ = [
    "CRITERIA", "Candidate", "Decision", "EvalContext", "Question", "Ranked", "RubricResult", "Tournament",
    "Triage", "audience_from_mission", "available_backends", "backend_name", "decide", "decide_batch",
    "evaluate", "rubric", "run_grouped", "run_tournament", "triage",
]
