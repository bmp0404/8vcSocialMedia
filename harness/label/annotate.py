"""Run LLM annotators over label items through the host gateway, one item per call.

Each annotator run appends one row per item to `data/labels/llm/<run_id>.jsonl` (the label-store
contract) and one row per HTTP attempt to `data/labels/calls/<run_id>.jsonl`. The rationale can quote
the item, and the repo is public, so it goes to the gitignored `data/labels/rationales/<run_id>.jsonl`
and the committed row keeps an empty `rationale`; the store joins the two locally. A rerun skips items
that already have a good row, so an interrupted run resumes where it stopped.

Limits from the brief: at most 40,000 calls across every run, at most 16 in flight, back off on
429 and 5xx, and stop when more than 20% of the last 200 calls failed.
"""

import asyncio
import json
import random
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from harness.llm import parse_json
from harness.label.prompt import prompt_sha, system_prompt, user_message
from harness.label.schema import LABELS_DIR, SCHEMA, validate_labels

LLM_DIR = LABELS_DIR / "llm"
CALLS_DIR = LABELS_DIR / "calls"
RATIONALES_DIR = LABELS_DIR / "rationales"
CALL_BUDGET = 40_000
MAX_IN_FLIGHT = 16
FAIL_WINDOW = 200
FAIL_SHARE = 0.20
MAX_ATTEMPTS = 6  # per item, across transient errors and malformed answers
MAX_TOKENS = 2000


class BudgetExceeded(RuntimeError):
    pass


class FailureRateStop(RuntimeError):
    pass


@dataclass(frozen=True)
class Annotator:
    model: str
    effort: str | None = "low"  # output_config.effort; the gateway passes it on

    def request_extra(self) -> dict:
        return {"output_config": {"effort": self.effort}} if self.effort else {}


def calls_used(calls_dir: Path = CALLS_DIR) -> int:
    if not calls_dir.is_dir():
        return 0
    return sum(sum(1 for _ in p.open(encoding="utf-8")) for p in calls_dir.glob("*.jsonl"))


def done_items(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    done = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if not row.get("error"):
                done.add(row["item_id"])
    return done


@dataclass
class Guard:
    """Shared across concurrent runs: the call budget, the in-flight cap and the failure-rate stop.

    The failure window is kept per model. Pooled over models, a healthy annotator's successes hid a
    GPT model failing 85 of its last 200 calls on 2026-10-16, which is the hammering the rule exists
    to prevent. A model that trips the rule stops; the others keep going.
    """

    budget_left: int
    in_flight: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(MAX_IN_FLIGHT))
    recent: dict = field(default_factory=dict)  # model -> deque of ok flags
    stopped_models: dict = field(default_factory=dict)  # model -> reason

    @property
    def stopped(self) -> str | None:
        return "; ".join(f"{m}: {r}" for m, r in self.stopped_models.items()) or None

    def take_call(self, model: str = "") -> None:
        if model in self.stopped_models:
            raise FailureRateStop(self.stopped_models[model])
        if self.budget_left <= 0:
            raise BudgetExceeded(f"call budget of {CALL_BUDGET} reached")
        self.budget_left -= 1

    def seed(self, calls_dir: Path = CALLS_DIR) -> None:
        """Start each model's window from its last logged calls, so a restart doesn't forget a failing
        streak. Only the latest window counts: a model that failed an hour ago and has answered its
        last 200 calls is not stopped."""
        rows = []
        if calls_dir.is_dir():
            for path in calls_dir.glob("*.jsonl"):
                with path.open(encoding="utf-8") as f:
                    rows += [(r["at"], r["model"], bool(r.get("ok"))) for r in map(json.loads, f)]
        latest = {}
        for _, model, ok in sorted(rows):
            latest.setdefault(model, deque(maxlen=FAIL_WINDOW)).append(ok)
        for model, window in latest.items():
            self.recent[model] = deque(window, maxlen=FAIL_WINDOW)
            failed = window.count(False)
            if len(window) == FAIL_WINDOW and failed > FAIL_SHARE * FAIL_WINDOW:
                self.stopped_models[model] = f"{failed} of its last {FAIL_WINDOW} calls failed"

    def record(self, ok: bool, model: str = "") -> None:
        window = self.recent.setdefault(model, deque(maxlen=FAIL_WINDOW))
        window.append(ok)
        if len(window) == FAIL_WINDOW and model not in self.stopped_models:
            failed = window.count(False)
            if failed > FAIL_SHARE * FAIL_WINDOW:
                self.stopped_models[model] = f"{failed} of its last {FAIL_WINDOW} calls failed"


@dataclass
class RunStats:
    calls: int = 0
    ok_items: int = 0
    error_items: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    errors: Counter = field(default_factory=Counter)
    response_models: Counter = field(default_factory=Counter)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["errors"], d["response_models"] = dict(self.errors), dict(self.response_models)
        return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _transient(e: Exception) -> bool:
    if isinstance(e, (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    return isinstance(e, anthropic.APIStatusError) and e.status_code >= 500


def _retry_after(e: Exception) -> float | None:
    resp = getattr(e, "response", None)
    value = resp.headers.get("retry-after") if resp is not None else None
    try:
        return float(value) if value else None
    except ValueError:
        return None


async def annotate_one(client, annotator: Annotator, run_id: str, item: dict, parent: dict | None, guard: Guard,
                       stats: RunStats, calls_log, schema: dict = SCHEMA) -> dict:
    system = [{"type": "text", "text": system_prompt(schema), "cache_control": {"type": "ephemeral"}}]
    message = user_message(item, parent, schema)
    usage_total = Counter()
    last_error = None
    response_model = None
    for attempt in range(MAX_ATTEMPTS):
        guard.take_call(annotator.model)
        t0 = time.monotonic()
        call = {"at": _now(), "run_id": run_id, "model": annotator.model, "item_id": item["item_id"], "attempt": attempt}
        try:
            async with guard.in_flight:
                resp = await client.messages.create(
                    model=annotator.model, max_tokens=MAX_TOKENS, system=system,
                    messages=[{"role": "user", "content": message}],
                    extra_body=annotator.request_extra() or None,
                )
        except Exception as e:  # noqa: BLE001 - every failure is logged, classified, then retried or recorded
            status = getattr(e, "status_code", None)
            kind = f"http_{status}" if status else type(e).__name__
            call.update(ok=False, error=kind, detail=str(e)[:300], seconds=round(time.monotonic() - t0, 2))
            calls_log.write(json.dumps(call) + "\n")
            stats.calls += 1
            stats.errors[kind] += 1
            guard.record(False, annotator.model)
            last_error = f"{kind}: {str(e)[:200]}"
            if not _transient(e):
                break
            await asyncio.sleep(_retry_after(e) or min(60.0, 2.0 ** attempt + random.random()))
            continue
        u = resp.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None) or 0,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", None) or 0}
        usage_total.update(usage)
        response_model = resp.model
        stats.calls += 1
        stats.input_tokens += usage["input_tokens"]
        stats.output_tokens += usage["output_tokens"]
        stats.cache_read += usage["cache_read_input_tokens"]
        stats.cache_write += usage["cache_creation_input_tokens"]
        stats.response_models[resp.model] += 1
        text = "".join(b.text for b in resp.content if b.type == "text")
        problem = None
        if resp.stop_reason == "refusal":
            problem = "refusal"
        else:
            try:
                answer = parse_json(text)
                labels = answer.get("labels") if isinstance(answer, dict) else None
                if not isinstance(labels, dict):
                    problem = "answer has no labels object"
                else:
                    issues = validate_labels(labels, item["kind"], schema)
                    if issues:
                        problem = "; ".join(issues)
            except ValueError as e:
                problem = "not JSON"  # the parser's message quotes the answer, which can quote the item
        call.update(ok=problem is None, error=problem, response_model=resp.model, stop=resp.stop_reason,
                    seconds=round(time.monotonic() - t0, 2), **usage)
        calls_log.write(json.dumps(call, ensure_ascii=False) + "\n")
        guard.record(problem is None, annotator.model)
        if problem is None:
            return {"item_id": item["item_id"], "run_id": run_id,
                    "annotator": {"model": annotator.model, "response_model": response_model,
                                  "prompt_sha": prompt_sha(schema), "effort": annotator.effort},
                    "labels": labels, "rationale": str(answer.get("rationale", ""))[:400],
                    "usage": dict(usage_total), "at": _now(), "error": None}
        stats.errors["invalid_answer" if problem != "refusal" else "refusal"] += 1
        last_error = f"invalid answer: {problem}"
        if problem == "refusal":
            break
    return {"item_id": item["item_id"], "run_id": run_id,
            "annotator": {"model": annotator.model, "response_model": response_model,
                          "prompt_sha": prompt_sha(schema), "effort": annotator.effort},
            "labels": {}, "rationale": "", "usage": dict(usage_total), "at": _now(), "error": last_error}


async def run_annotator(annotator: Annotator, run_id: str, items: list[dict], by_id: dict, guard: Guard,
                        concurrency: int, schema: dict = SCHEMA, llm_dir: Path = LLM_DIR,
                        calls_dir: Path = CALLS_DIR, client=None, rationales_dir: Path = RATIONALES_DIR) -> RunStats:
    for d in (llm_dir, calls_dir, rationales_dir):
        d.mkdir(parents=True, exist_ok=True)
    out_path, calls_path = llm_dir / f"{run_id}.jsonl", calls_dir / f"{run_id}.jsonl"
    why_path = rationales_dir / f"{run_id}.jsonl"
    done = done_items(out_path)
    todo = [it for it in items if it["item_id"] not in done]
    stats = RunStats()
    client = client or anthropic.AsyncAnthropic(max_retries=0, timeout=120)
    lane = asyncio.Semaphore(concurrency)
    queue = iter(todo)
    with out_path.open("a", encoding="utf-8") as out, calls_path.open("a", encoding="utf-8") as calls_log, \
            why_path.open("a", encoding="utf-8") as why:
        async def worker():
            for item in queue:
                if annotator.model in guard.stopped_models:
                    return
                parent = by_id.get(item["parent_id"]) if item.get("parent_id") else None
                async with lane:
                    try:
                        row = await annotate_one(client, annotator, run_id, item, parent, guard, stats, calls_log, schema)
                    except (BudgetExceeded, FailureRateStop):
                        return
                if row["rationale"]:
                    why.write(json.dumps({k: row[k] for k in ("item_id", "run_id", "at", "rationale")},
                                         ensure_ascii=False) + "\n")
                    why.flush()
                out.write(json.dumps(row | {"rationale": ""}, ensure_ascii=False) + "\n")
                out.flush()
                calls_log.flush()
                if row["error"]:
                    stats.error_items += 1
                else:
                    stats.ok_items += 1

        await asyncio.gather(*(worker() for _ in range(concurrency)))
    return stats
