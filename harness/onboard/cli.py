"""`harness onboard --handle X [--site URL] --profile NAME` and `harness onboard check --profile NAME`."""

import os

from harness import config
from harness.context.http import PoliteClient
from harness.context.xreader import FxTwitterReader
from harness.facts import store
from harness.llm import get_llm
from harness.memstore.note import store_root
from harness.onboard import claims, profile
from harness.watcher import modelrec


def store_mismatch(provider: str | None) -> str | None:
    """Why the site facts would be lost, or None. The memory store follows HARNESS_PROVIDER (config.PROVIDER), not
    --provider, and under mock it is a temporary folder deleted at exit; facts.store.refresh refuses the same case."""
    drafting = (provider or config.PROVIDER).lower()
    if drafting != "mock" and config.PROVIDER == "mock" and not os.getenv("MEMSTORE_PATH"):
        return (f"--provider {drafting} drafts for real, but HARNESS_PROVIDER is mock, so the site facts would go to a "
                "temporary memory store; set HARNESS_PROVIDER as for a real run, or MEMSTORE_PATH")
    return None


def _draft(args) -> None:
    if not args.handle:
        raise SystemExit("[onboard] --handle is required to draft a profile")
    profile.check_free(args.profile, config.PROFILES_DIR)  # before any request or LLM call
    if problem := store_mismatch(args.provider):
        raise SystemExit(f"[onboard] {problem}")
    # A 2-second interval on the throttle every process shares keeps this read to half of fxtwitter's 1 request a
    # second, so the watcher's polls still get their turns. Cached for a day, so a rerun reads nothing new.
    reader = FxTwitterReader(PoliteClient(min_interval_s=2.0), max_age_s=86400)
    modelrec.install()
    with modelrec.recording() as calls:
        d = profile.draft(args.handle, args.profile, reader=reader, llm=get_llm(args.provider), site=args.site,
                          max_posts=args.max_posts)
    d.models = calls
    target = profile.write(d)
    print(f"[onboard] wrote {target}")
    tabs = {k: d.read_log.get(k, d.read_log.get("replies_tab_error", "failed")) for k in ("posts_tab", "replies_tab")}
    print(f"[onboard] read {tabs['posts_tab']} posts-tab and {tabs['replies_tab']} replies-tab statuses; "
          f"{len(d.pages)} pages; watch list {len(d.watch)} accounts; "
          f"{sum(len(v) for v in d.items.values())} cited mission lines, {len(d.left_out)} left out")
    if d.facts:
        with config.use_profile(args.profile):
            n = store.save([claims.memory_fact(f, d.handle, d.today) for f in d.facts])
            where = store_root()  # a temporary folder under the mock provider
        print(f"[onboard] saved {n} site facts to the {args.profile} memory store at {where}")
    failed = [c for c in profile.check(args.profile) if c.problems]
    print(f"[onboard] claim check: {'every line traces' if not failed else f'{len(failed)} lines fail'}; "
          f"read {target / 'ONBOARDING.md'} before using the profile")


def _check(args) -> None:
    results = profile.check(args.profile)
    failed = [c for c in results if c.problems]
    for c in failed:
        print(f"[onboard] FAIL {c.section}: {c.text[:120]}")
        for p in c.problems:
            print(f"    {p}")
    print(f"[onboard] {len(results) - len(failed)} of {len(results)} cited lines trace to what they cite")
    if failed:
        raise SystemExit(1)


def add_commands(sub) -> None:
    p = sub.add_parser("onboard", help="draft a provisional mission profile from a brand's X timeline and site; "
                                       "`onboard check` re-traces its mission's claims")
    p.add_argument("action", nargs="?", choices=["draft", "check"], default="draft")
    p.add_argument("--handle", help="the brand's X handle")
    p.add_argument("--site", help="the brand's homepage; the homepage and up to 10 linked pages are read")
    p.add_argument("--profile", required=True, help="name of the new profile under mission/profiles/; never overwritten")
    p.add_argument("--provider", help="override HARNESS_PROVIDER for the drafting calls")
    p.add_argument("--max-posts", type=int, default=800, help="statuses to read per timeline tab (default 800)")
    p.set_defaults(func=lambda a: _check(a) if a.action == "check" else _draft(a))
