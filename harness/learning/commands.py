"""CLI subcommands for ingest, reflection and revalidation. `harness/cli.py` registers them with one call."""

import json
from pathlib import Path

from harness.learning.settings import load_settings
from harness.learning.store import flush, get_store
from harness.llm import DEFAULT_MODELS, AnthropicLLM, get_llm


def _ingest_dataset(args) -> None:
    from harness.learning.dataset import CONTEXT_PATH, DATASET_PATH, ingest_dataset

    store = get_store()
    ingest_dataset(store, Path(args.csv) if args.csv else DATASET_PATH,
                   Path(args.context) if args.context else CONTEXT_PATH, domain=args.domain,
                   per_category=load_settings().ingest.exemplars_per_category)
    flush(store)



def _ingest_brand(args) -> None:
    from harness.learning.brand import ingest_brand

    store = get_store()
    ingest_brand(store, llm_for(args.provider), args.sources, args.name, max_pages=args.max_pages)
    flush(store)



class RoomyAnthropicLLM(AnthropicLLM):
    """AnthropicLLM with room for thinking. harness/llm.py caps replies at 4096 tokens, and on the gateway's
    claude-sonnet-5-5 thinking alone can use that up: the brand ingest's answer call came back empty at 4096
    and used 8,248 output tokens at 16,000. Remove once llm.py raises its cap."""

    max_tokens = 16000

    def complete(self, system: str, prompt: str) -> str:
        resp = self.client.messages.create(model=self.model, max_tokens=self.max_tokens, system=system,
                                           messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in resp.content if b.type == "text")


def llm_for(provider: str | None, model: str = ""):
    """get_llm with an optional model override, and more output room for Anthropic."""
    from harness import config

    name = (provider or config.PROVIDER).lower()
    if name == "anthropic":
        return RoomyAnthropicLLM(model or config.MODEL or DEFAULT_MODELS["anthropic"])
    if not model:
        return get_llm(provider)
    saved = config.MODEL
    config.MODEL = model  # harness/llm.py reads config.MODEL; set it for this one call
    try:
        return get_llm(provider)
    finally:
        config.MODEL = saved


def _settings(args):
    settings = load_settings()
    r = settings.reflect
    for name in ("batches", "groups", "per_side", "hypotheses"):
        value = getattr(args, name, None)
        if value is not None:
            setattr(r, {"batches": "n_batches", "groups": "groups_per_batch", "hypotheses": "n_hypotheses"}.get(name, name), value)
    if getattr(args, "label_model", None):
        r.label_model = args.label_model
    if getattr(args, "allow_unfiltered", False):
        r.require_boost_flags = False
    return settings


def _dataset_episodes(args, settings):
    from harness.learning.dataset import CONTEXT_PATH, DATASET_PATH, attach_context, load_dataset

    episodes = load_dataset(Path(args.csv) if args.csv else DATASET_PATH,
                            thread_gap_seconds=settings.reflect.thread_gap_seconds)
    if CONTEXT_PATH.exists():
        print(f"[reflect] attached {attach_context(episodes, CONTEXT_PATH)} rows of {CONTEXT_PATH.name}")
    return episodes


def _reflect(args) -> None:
    from harness.learning.dataset import OUTCOME_DEFINITION
    from harness.learning.reflect import reflect, report_json

    settings = _settings(args)
    r = settings.reflect
    episodes = _dataset_episodes(args, settings)
    store = get_store()
    report = reflect(episodes, llm_for(args.provider, r.proposer_model), store, definition=OUTCOME_DEFINITION,
                     domain=args.domain, settings=r, labeler=llm_for(args.provider, r.label_model), seed=args.seed)
    flush(store)
    if args.json:
        Path(args.json).write_text(report_json(report), encoding="utf-8")
        print(f"[reflect] report -> {args.json}")
    for refl in report.reflections:
        print(f"\n[{refl.status}] {refl.outcome}: {refl.claim}\n  predicate: {refl.predicate}"
              + (f"\n  rule: {refl.rule}" if refl.rule else "")
              + f"\n  evidence: {', '.join(refl.evidence)}\n  contrast: {', '.join(refl.contrast)}"
              f"\n  held-out: {heldout_line(refl.heldout, refl.outcome)}")


def heldout_line(h: dict, outcome: str) -> str:
    from harness.learning.kinds import heldout_sentence

    return f"{heldout_sentence(h, outcome)}; effect {h['effect']}, p={h['p_value']}, checked by {h.get('checked_by')}"


def _revalidate(args) -> None:
    from harness.learning.reflect import revalidate

    settings = _settings(args)
    r = settings.reflect
    store = get_store()
    results = revalidate(_dataset_episodes(args, settings), llm_for(args.provider, r.proposer_model), store,
                         domain=args.domain, settings=r, labeler=llm_for(args.provider, r.label_model))
    flush(store)
    print(json.dumps(results, indent=1) if results else "[revalidate] no lessons are due")


def _settings_cmd(args) -> None:
    print(json.dumps(load_settings().to_dict(), indent=1))


def _exemplar_check(args) -> None:
    from harness.learning.dataset import DATASET_PATH, load_dataset
    from harness.learning.ranking import exemplar_check

    print(json.dumps(exemplar_check(load_dataset(Path(args.csv) if args.csv else DATASET_PATH), k=args.k,
                                    n_test=args.n_test, seed=args.seed), indent=1))


def add_commands(sub) -> None:
    p = sub.add_parser("ingest-dataset", help="write exemplar posts and account profiles from the data team's table")
    p.add_argument("--csv", help="defaults to data/devtools_virality_30d.csv")
    p.add_argument("--context", help="defaults to data/context/devtools_enriched.csv when it exists")
    p.add_argument("--domain", default="devtools")
    p.set_defaults(func=_ingest_dataset)

    p = sub.add_parser("ingest-brand", help="fetch the brand's own pages or files, question them, store verified facts")
    p.add_argument("sources", nargs="+", help="URLs or local files, e.g. https://resend.com mission/mission.md")
    p.add_argument("--name", required=True, help="the brand or account name")
    p.add_argument("--max-pages", type=int, default=6)
    p.add_argument("--provider")
    p.set_defaults(func=_ingest_brand)

    p = sub.add_parser("reflect", help="ask why same-account posts failed or succeeded; keep claims that pass a held-out test")
    p.add_argument("--provider")
    p.add_argument("--csv")
    p.add_argument("--domain", default="devtools")
    p.add_argument("--batches", type=int, default=None)
    p.add_argument("--groups", type=int, default=None, help="accounts per batch")
    p.add_argument("--per-side", type=int, default=None, help="failures and successes per account")
    p.add_argument("--hypotheses", type=int, default=None, help="hypotheses per batch")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--json", help="also write the full report to this path")
    p.add_argument("--label-model", help="model for answering predicates; use one other than the proposer's")
    p.add_argument("--allow-unfiltered", action="store_true",
                   help="run without boost_suspect flags, so boosted successes stay in")
    p.set_defaults(func=_reflect)

    p = sub.add_parser("revalidate", help="retest lessons that are due (new labeled episodes, or past valid_until)")
    p.add_argument("--provider")
    p.add_argument("--csv")
    p.add_argument("--domain", default="devtools")
    p.add_argument("--label-model")
    p.add_argument("--allow-unfiltered", action="store_true")
    p.set_defaults(func=_revalidate)

    p = sub.add_parser("memory-settings", help="print the memory stage's tunables, with memory/settings.json applied")
    p.set_defaults(func=_settings_cmd)

    p = sub.add_parser("exemplar-check", help="measure whether keyword-similar past posts predict outcomes (no LLM)")
    p.add_argument("--csv")
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--n-test", type=int, default=400)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=_exemplar_check)
