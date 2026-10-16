"""Qualify fitness: does the audience panel score an account's viral posts above its Deadweight posts?

The metric is the eval worker's headline, the within-account viral-vs-Deadweight AUC, on its protocol
sample (harness/evals/backtest.py: eligible posts, up to 4 per tier from each account with both
tiers). The account prior alone reaches AUC 0.90 across accounts (research/context.md), so only the
within-account comparison says anything about the text.

How a config maps onto a predictor:
- qualify.panel: the persona panel in eval/panels/devtools.json, or eval's one-line reader.
- qualify.model: the model the panel runs on.
- qualify.batch: 0 is production (harness/qualify.py scores every draft of a run in one call, in two
  orders, averaged), which here means one call per account per order. 1 scores each post alone.
- qualify.memory: off, or the account's own older posts with their outcomes in the prompt ("on"),
  or the same posts with their outcome labels shuffled ("shuffled", the control).

Arms the eval worker already scored under the same protocol and sample are imported from its report
instead of re-run, and the record says so. The champion repeat always runs live, because importing
it would give a noise floor of exactly zero.
"""

import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

from harness import config as hconfig
from harness.evals.backtest import eligible, sample
from harness.evals.dataset import Post, is_scorable, is_thread_continuation, load
from harness.evals.predictors import READER_PANEL, PanelPredictor
from harness.evals.settings import BacktestConfig
from harness.iterate.fitness import ArmResult, Item, LLMFor, Metric
from harness.iterate.knobs import Config
from harness.iterate.stats import Stat, within_cluster_auc_stat
from harness.models import Action
from harness.qualify import PANEL_PROMPT, PANEL_SYSTEM, _drafts, load_panel

PANEL_PATH = hconfig.ROOT / "eval" / "panels" / "devtools.json"
CACHE_REPORT = hconfig.ROOT / "eval" / "reports" / "backtest-20261015T205419-anthropic.json"
BATCH_ALL = 64  # larger than any account's sample (2 * per_tier), so PanelPredictor makes one chunk per account
MEMORY_HEADER = ("EARLIER POSTS FROM THIS ACCOUNT AND HOW THEY DID (context only; do not score these):\n{refs}\n\n")
LABELS = {True: "did well (100+ engagements)", False: "flopped (10 or fewer)"}


class MemoryPanelPredictor(PanelPredictor):
    """PanelPredictor whose prompt starts with the account's older posts and their outcomes.

    References predate the oldest post in the call (a rolling origin) and come from the same account,
    most recent first, up to `k`. "shuffled" keeps the same posts and permutes their labels."""

    def __init__(self, *args, history: list[Post], k: int, mode: str, **kw):
        super().__init__(*args, **kw)
        if mode not in ("on", "shuffled"):
            raise ValueError(f"memory mode must be on or shuffled, got {mode!r}")
        self.k, self.mode = k, mode
        self.by_account: dict[str, list[Post]] = defaultdict(list)
        for p in history:
            if is_scorable(p) and not is_thread_continuation(p):
                self.by_account[p.account].append(p)
        self.refs_used: dict[str, list[str]] = {}

    def references(self, chunk: list[Post]) -> list[tuple[Post, bool]]:
        oldest = min(p.created_at for p in chunk)
        ids = {p.id for p in chunk}
        older = sorted((q for q in self.by_account[chunk[0].account] if q.created_at < oldest and q.id not in ids),
                       key=lambda q: q.created_at, reverse=True)[: self.k]
        labels = [q.viral for q in older]
        if self.mode == "shuffled":
            random.Random(f"{self.seed}:{chunk[0].id}").shuffle(labels)
        return list(zip(older, labels))

    def _score(self, chunk: list[Post]) -> list[dict[str, float]]:
        refs = self.references(chunk)
        leaked = [q.id for q, _ in refs if q.created_at >= min(p.created_at for p in chunk)]
        if leaked:
            raise ValueError(f"memory references postdate the candidates: {leaked}")
        self.refs_used[chunk[0].id] = [q.id for q, _ in refs]
        block = MEMORY_HEADER.format(refs="\n".join(f"- [{LABELS[v]}] {q.clean_text}" for q, v in refs) or "(none)")
        actions = [Action(type="post", text=p.clean_text, mind="backtest", narrative_id="", id=f"p{i}")
                   for i, p in enumerate(chunk)]
        self.calls += 1
        data = self.llm.complete_json(
            PANEL_SYSTEM, block + PANEL_PROMPT.format(panel=json.dumps(self.panel, indent=1), drafts=_drafts(actions)),
            mock=lambda: {"scores": {a.id: {name: 0.5 for name in self.panel} for a in actions}})
        scores = data.get("scores", {}) if isinstance(data, dict) else {}
        out = []
        for a, p in zip(actions, chunk):
            s = {k: float(v) for k, v in scores.get(a.id, {}).items() if k in self.panel}
            if set(s) != set(self.panel):
                raise ValueError(f"panel returned panelists {sorted(s)} for {p.id}; expected {sorted(self.panel)}")
            out.append(s)
        return out


class QualifyAUC:
    name = "qualify_auc"
    stage = "qualify"
    unit = "post"
    cluster_unit = "account"
    primary = Metric("within_account_auc", higher_is_better=True)
    secondaries: tuple[Metric, ...] = ()
    knobs = ("qualify.panel", "qualify.model", "qualify.batch", "qualify.memory", "memory.retrieval_k")

    def __init__(self, per_tier: int = 4, seed: int = 0, cache_report: Path | None = CACHE_REPORT):
        self.cfg = BacktestConfig(per_tier=per_tier, seed=seed)
        self.posts = load()
        pool, self.dropped = eligible(self.posts, self.cfg)
        self.picked = sample(pool, self.cfg.per_tier, self.cfg.seed)
        self.cache = self._load_cache(cache_report)

    def _load_cache(self, path: Path | None) -> dict[tuple, dict[str, float]]:
        """{(panel, model, batch, memory): {post id: score}} from an eval report on this exact sample."""
        if path is None or not path.exists():
            return {}
        report = json.loads(path.read_text(encoding="utf-8"))
        if sorted(report.get("sample_ids", [])) != sorted(p.id for p in self.picked):
            print(f"[iterate] {path.name} scored a different sample; not importing its arms")
            return {}
        if report.get("config", {}).get("batch", 1) != 1:
            return {}
        if Path(report.get("panel_path", "")).name != PANEL_PATH.name:
            return {}
        self.cache_source = str(path.relative_to(hconfig.ROOT))
        out = {}
        for name, res in report["results"].items():
            base, _, model = name.partition("@")
            if base in ("panel", "reader") and model and model != "mean":
                key = ("personas" if base == "panel" else "single_reader", model, 1, "off")
                out[key] = {pid: pred["score"] for pid, pred in res["predictions"].items()}
        return out

    def _key(self, cfg: Config) -> tuple:
        return (cfg.get("qualify.panel"), cfg.get("qualify.model"), cfg.get("qualify.batch"), cfg.get("qualify.memory"))

    def items(self) -> list[Item]:
        return [Item(p.id, p.account, p) for p in self.picked]

    def instrument(self) -> dict:
        data = (hconfig.ROOT / "data" / "devtools_virality_30d.csv").read_bytes()
        return {"dataset": "data/devtools_virality_30d.csv", "dataset_sha256": hashlib.sha256(data).hexdigest(),
                "protocol": "within-account viral vs Deadweight AUC (harness/evals/backtest.py, protocol v2)",
                "backtest_config": {"per_tier": self.cfg.per_tier, "seed": self.cfg.seed,
                                    "min_age_hours": self.cfg.min_age_hours,
                                    "thread_gap_minutes": self.cfg.thread_gap_minutes, "min_chars": self.cfg.min_chars},
                "panel_path": str(PANEL_PATH.relative_to(hconfig.ROOT)), "dropped": self.dropped,
                "cache": getattr(self, "cache_source", None)}

    def estimate_calls(self, config: Config, n_items: int, repeat: bool = False) -> int:
        if not repeat and self._key(config) in self.cache:
            return 0
        accounts = len({p.account for p in self.picked})
        return n_items if config.get("qualify.batch") == 1 else 2 * accounts * -(-(2 * self.cfg.per_tier) // BATCH_ALL)

    def run_arm(self, config: Config, items: list[Item], llm_for: LLMFor, *, seed: int, repeat: bool) -> ArmResult:
        key = self._key(config)
        posts = [it.payload for it in items]
        if not repeat and key in self.cache:
            scores = self.cache[key]
            return ArmResult(values={p.id: scores[p.id] for p in posts if p.id in scores},
                             detail={p.id: {"score": scores.get(p.id), "source": self.cache_source} for p in posts})
        panel = load_panel(PANEL_PATH) if config.get("qualify.panel") == "personas" else READER_PANEL
        batch = 1 if config.get("qualify.batch") == 1 else BATCH_ALL
        if config.get("qualify.batch") not in (0, 1):
            raise ValueError("qualify_auc measures batch 0 (all drafts in one call) or 1 (each alone)")
        llm = llm_for(config.get("qualify.model"))
        run_seed = seed + 1000 if repeat else seed  # the repeat reshuffles every batch
        mode = config.get("qualify.memory")
        if mode == "off":
            pred = PanelPredictor(llm, panel, name="panel", batch=batch, workers=6, seed=run_seed)
        else:
            pred = MemoryPanelPredictor(llm, panel, name="panel", batch=batch, workers=6, seed=run_seed,
                                        history=self.posts, k=config.get("memory.retrieval_k"), mode=mode)
        preds = pred.predict(posts)
        res = ArmResult(values={pid: pr.score for pid, pr in preds.items()})
        for p in posts:
            res.detail[p.id] = {"score": preds[p.id].score if p.id in preds else None, "viral": p.viral,
                                "account": p.account, **({"panelists": preds[p.id].detail.get("panelists")}
                                                         if p.id in preds else {})}
        res.failures = {pid: "panel call failed" for pid in pred.failures}
        if isinstance(pred, MemoryPanelPredictor):
            res.detail["_memory_references"] = pred.refs_used
        return res

    def stat(self, items: list[Item], values: dict[str, float], metric: Metric) -> Stat:
        return within_cluster_auc_stat([values[it.id] for it in items], [it.payload.viral for it in items])

    @staticmethod
    def mock_respond(system: str, prompt: str):
        return {}
