"""Bounded rounds with stage artifacts and explicit failure states."""

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from harness import config, memory
from harness.learn import learn
from harness.llm import LLM
from harness.minds import decide, lanes, write_decision_log
from harness.models import Action, Narrative, Signal
from harness.orient import orient
from harness.sources import observe
from harness.sources.watch import account_handle

from .evaluation import panel_evaluate


@dataclass(frozen=True)
class Options:
    rounds: int = 1
    interval: float = 0
    narratives: int = 2
    per_mind: int = 1
    top: int = 10
    retries: int = 2

    def __post_init__(self):
        if min(self.rounds, self.narratives, self.per_mind, self.top) < 1:
            raise ValueError("rounds, narratives, per_mind, and top must be positive")
        if self.retries < 0 or self.interval < 0:
            raise ValueError("retries and interval must be nonnegative")


@dataclass
class Components:
    read_memory: Callable[[], str] = config.lessons
    observe: Callable[[list[str], str], list[Signal]] = observe
    orient: Callable[..., list[Narrative]] = orient
    decide: Callable[..., list[Action]] = decide
    evaluate: Callable[..., tuple[list[Action], dict]] = panel_evaluate
    save_run: Callable[..., int] = memory.save_run
    # decide() queues every decision, abstentions included, in lanes.PENDING; this writes them out as
    # decisions-run<id>.json, the file `harness run` writes, so engine rounds keep their abstentions.
    log_decisions: Callable[[int, Path], Path | None] = write_decision_log
    finish_memory: Callable[[LLM], object] = learn


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    pending.replace(path)


def run_round(llm: LLM, mission: str, sources: list[str], options: Options,
              components: Components, output_dir: Path) -> dict:
    round_id = uuid.uuid4().hex
    folder = output_dir / "engine" / round_id
    journal = {"round_id": round_id, "status": "running", "stage": "memory_read", "run_id": None,
               "provider": config.PROVIDER, "sources": sources, "stages": [], "attempts": []}

    def stage(name: str, call):
        journal["stage"] = name
        write_json(folder / "round.json", journal)
        started = time.monotonic()
        result = call()
        journal["stages"].append({"name": name, "seconds": round(time.monotonic() - started, 4)})
        return result

    try:
        account, _ = account_handle()
        lessons = stage("memory_read", components.read_memory)
        observation_context = f"{mission}\n\nLESSONS FROM PREVIOUS ROUNDS:\n{lessons}"
        signals = stage("observe", lambda: components.observe(sources, observation_context))
        write_json(folder / "observations.json", {"signals": [asdict(s) for s in signals]})
        narratives = stage("orient", lambda: components.orient(llm, mission, signals, top=options.narratives)) if signals else []
        write_json(folder / "narratives.json", {"narratives": [asdict(n) for n in narratives]})
        all_actions, kept, evaluations = [], [], []
        previous = None
        if narratives:
            for attempt in range(options.retries + 1):
                kwargs = {"per_mind": options.per_mind}
                if previous:
                    kwargs["vetoed"] = previous
                actions = stage("action", lambda: components.decide(llm, mission, lessons, narratives, **kwargs))
                all_actions.extend(actions)
                write_json(folder / "actions.json", {"actions": [a.to_dict() for a in all_actions], "evaluations": evaluations})
                if not actions:
                    journal["attempts"].append({"attempt": attempt, "actions": 0, "kept": 0})
                    break
                kept, evidence = stage("eval", lambda: components.evaluate(llm, mission, actions, narratives, signals))
                if len({a.id for a in kept}) != len(kept) or any(a.id not in {draft.id for draft in actions} or a.vetoed for a in kept):
                    raise ValueError("Evaluator returned duplicate, unknown, or vetoed recommendations")
                evaluations.append(evidence)
                journal["attempts"].append({"attempt": attempt, "actions": len(actions), "kept": len(kept)})
                write_json(folder / "actions.json", {"actions": [a.to_dict() for a in all_actions], "evaluations": evaluations})
                if kept:
                    break
                previous = [a for a in actions if a.vetoed]
                if not previous:
                    break
        write_json(folder / "actions.json", {"actions": [a.to_dict() for a in all_actions], "evaluations": evaluations})
        if len({a.id for a in all_actions}) != len(all_actions):
            raise ValueError("Action IDs must be unique across retry attempts")
        run_id = stage("persist", lambda: components.save_run(all_actions, account=account or None))
        journal["run_id"] = run_id
        decisions_path = components.log_decisions(run_id, output_dir)
        journal["decisions_path"] = str(decisions_path) if decisions_path else None
        recommendation_path = output_dir / f"recommendations-engine-{round_id}.json"
        write_json(recommendation_path, {"run_id": run_id, "account": account or None,
                                         "recommendations": [a.to_dict() for a in kept[:options.top]]})
        journal["recommendations_path"] = str(recommendation_path)
        journal["recommendations"] = min(len(kept), options.top)
        journal["actions"] = len(all_actions)
        journal["reason"] = "no_signals" if not signals else "no_narratives" if not narratives else "no_eligible_actions" if not kept else "recommendations_ready"
        # Existing learn() only derives lessons from recorded real outcomes, not panel predictions.
        stage("memory_write", lambda: components.finish_memory(llm))
        journal["status"] = "completed" if kept else "abstained"
        journal["stage"] = "done"
        write_json(folder / "round.json", journal)
        return journal
    except Exception as exc:
        # A failed round's queued decisions would otherwise be written under the next round's run id.
        lanes.PENDING.clear()
        journal["status"] = "failed"
        journal["error_type"] = type(exc).__name__
        write_json(folder / "round.json", journal)
        raise


def run_loop(llm: LLM, mission: str, sources: list[str], options: Options,
             components: Components, output_dir: Path) -> list[dict]:
    if not mission.strip() or not sources:
        raise ValueError("A nonempty mission and at least one source are required")
    results = []
    for index in range(options.rounds):
        if index and options.interval:
            time.sleep(options.interval)
        result = run_round(llm, mission, sources, options, components, output_dir)
        results.append(result)
        print(f"[engine] round {index + 1}/{options.rounds}: {result['status']} (run {result['run_id']})")
    return results
