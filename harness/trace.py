"""Run tracing for the observability console (`/console`).

`instrument()` wraps whichever loop stages `harness.cli` imports (observe, orient, decide, qualify,
and tournament / sharpen_hooks / evolve when present), every LLM call, and the mind bandit's draw.
When a run is saved it writes out/trace-run<id>.json. Nothing in the loop reads the trace back, and
`run()` itself is untouched, so a tracing bug can lose a trace but never change a recommendation.
"""

import functools
import importlib
import io
import json
import re
import threading
import time
from contextlib import redirect_stdout
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from harness import config
from harness.llm import LLM

STAGES = ("evolve", "observe", "orient", "decide", "qualify", "tournament", "sharpen_hooks")
PREVIEW = 4000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plain(x: Any) -> Any:
    return asdict(x) if is_dataclass(x) else x


class Trace:
    def __init__(self):
        self.reset()
        self._lock = threading.Lock()

    def reset(self) -> None:
        self.t0 = time.perf_counter()
        self.started_at = _now()
        self.provider = config.PROVIDER
        self.stages: list[dict] = []
        self.sources: list[dict] = []
        self.calls: list[dict] = []
        self.bandit: dict | None = None
        self.signals: list = []
        self.narratives: list = []
        self.ranked: list = []
        self.stage_name = ""

    def ms(self, t: float) -> int:
        return round((t - self.t0) * 1000)

    def span(self, name: str, fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            if name in ("evolve", "observe") and not any(s["name"] == "evolve" for s in self.stages):
                self.reset()  # a new run starts here (`cycle` starts with evolve)
            self.stage_name = name
            start = time.perf_counter()
            out = self._observe(*args, **kwargs) if name == "observe" else fn(*args, **kwargs)
            self.stages.append({"name": name, "start_ms": self.ms(start), "ms": self.ms(time.perf_counter()) - self.ms(start),
                                "n_out": len(out) if isinstance(out, list) else None})
            if name == "observe":
                self.signals = out
            elif name == "orient":
                self.narratives = out
            elif name in ("qualify", "tournament", "sharpen_hooks"):
                self.ranked = out
            return out

        if name == "observe":
            self._observe_fn = fn
        return wrapped

    def _observe(self, names: list[str], mission: str):
        """Run each source on its own so the trace gets a count, a timing, and the error per source."""
        signals = []
        for name in names:
            start = time.perf_counter()
            log = io.StringIO()
            with redirect_stdout(log):
                got = self._observe_fn([name], mission)
            print(log.getvalue(), end="")
            failed = re.search(r"(?:failed: |unknown source).*", log.getvalue())
            self.sources.append({"name": name, "n": len(got), "start_ms": self.ms(start),
                                 "ms": self.ms(time.perf_counter()) - self.ms(start),
                                 "error": failed.group(0).strip() if failed else ""})
            signals.extend(got)
        return signals

    def write(self, run_id: int, actions: list) -> None:
        data = {
            "run_id": run_id,
            "started_at": self.started_at,
            "finished_at": _now(),
            "total_ms": self.ms(time.perf_counter()),
            "provider": self.provider,
            "model": config.MODEL,
            "stages": self.stages,
            "sources": self.sources,
            "llm_calls": self.calls,
            "bandit": self.bandit,
            "signals": [{"id": f"s{i}", **_plain(s)} for i, s in enumerate(self.signals)],
            "narratives": [_plain(n) for n in self.narratives],
            "n_drafts": len(actions),
            "ranked_ids": [getattr(a, "id", None) for a in self.ranked],
        }
        config.OUT_DIR.mkdir(parents=True, exist_ok=True)
        (config.OUT_DIR / f"trace-run{run_id}.json").write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")


def seed_minds() -> dict[str, str]:
    """Seed mind prompts by name, whichever way this checkout stores them."""
    if callable(getattr(config, "seed_minds", None)):
        return config.seed_minds()
    from harness import minds

    return minds.load_minds() if callable(getattr(minds, "load_minds", None)) else {}


# Modules whose *_SYSTEM prompts name an LLM call in the trace; missing modules are skipped.
LABELLED = ("orient", "qualify", "tournament", "hooks", "evolve", "learn")


def _label(system: str) -> str:
    try:
        for name, persona in seed_minds().items():
            if system.startswith(persona.strip()):
                return name
        for mod_name in LABELLED:
            try:
                mod = importlib.import_module(f"harness.{mod_name}")
            except ImportError:
                continue
            for attr, value in vars(mod).items():
                if attr.endswith("SYSTEM") and isinstance(value, str) and system.startswith(value):
                    return mod_name if attr == "SYSTEM" else f"{mod_name} · {attr[:-len('_SYSTEM')].lower().replace('_', ' ')}"
    except Exception:  # noqa: BLE001 - labelling is cosmetic
        pass
    return system.strip().split("\n", 1)[0][:60]


class TracingLLM(LLM):
    """Times each call and keeps a preview of what went in and came out."""

    def __init__(self, inner: LLM, trace: Trace):
        self.inner = inner
        self.trace = trace

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _call(self, fn, system: str, prompt: str, *args, **kwargs):
        start, error, out = time.perf_counter(), "", None
        try:
            out = fn(system, prompt, *args, **kwargs)
            return out
        except Exception as e:
            error = str(e)[:500]
            raise
        finally:
            try:
                self._record(system, prompt, start, out, error)
            except Exception as e:  # noqa: BLE001 - a tracing bug must never fail the LLM call
                print(f"[trace] could not record LLM call: {e}")

    def _record(self, system: str, prompt: str, start: float, out: Any, error: str) -> None:
        text = out if isinstance(out, str) else json.dumps(out, indent=1, default=str)
        call = {
            "stage": self.trace.stage_name, "label": _label(system),
            "start_ms": self.trace.ms(start), "ms": self.trace.ms(time.perf_counter()) - self.trace.ms(start),
            "ok": not error, "error": error,
            "system_chars": len(system), "prompt_chars": len(prompt), "response_chars": len(text or ""),
            "prompt_preview": prompt[:PREVIEW], "response_preview": (text or "")[:PREVIEW],
        }
        with self.trace._lock:
            self.trace.calls.append(call)

    def complete(self, system: str, prompt: str, *args, **kwargs) -> str:
        return self._call(self.inner.complete, system, prompt, *args, **kwargs)

    def complete_json(self, system: str, prompt: str, *args, **kwargs) -> Any:
        return self._call(self.inner.complete_json, system, prompt, *args, **kwargs)


def instrument() -> None:
    from harness import cli, memory

    trace = Trace()
    for name in STAGES:
        if callable(getattr(cli, name, None)):
            setattr(cli, name, trace.span(name, getattr(cli, name)))

    get_llm = cli.get_llm
    cli.get_llm = functools.wraps(get_llm)(lambda *a, **k: TracingLLM(get_llm(*a, **k), trace))

    bandit = getattr(cli, "bandit", None)
    if bandit and callable(getattr(bandit, "choose", None)):
        choose = bandit.choose

        @functools.wraps(choose)
        def traced_choose(arms, stats, *args, **kwargs):
            chosen = choose(arms, stats, *args, **kwargs)
            trace.bandit = {
                "arms": [{"id": a, "n": getattr(stats.get(a), "n", 0), "mean": round(getattr(stats.get(a), "mean", 0.0), 3)}
                         for a in arms],
                "chosen": list(chosen),
            }
            return chosen

        bandit.choose = traced_choose

    save_run = memory.save_run

    @functools.wraps(save_run)
    def traced_save_run(actions, *args, **kwargs):
        run_id = save_run(actions, *args, **kwargs)
        try:
            trace.write(run_id, actions)
        except Exception as e:  # noqa: BLE001 - a lost trace must never fail the run
            print(f"[trace] could not write trace for run {run_id}: {e}")
        return run_id

    memory.save_run = traced_save_run
