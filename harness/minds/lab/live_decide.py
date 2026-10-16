"""Run the decide lanes on live X posts: the real check behind the PR's sample output.

Reads each account's latest posts through api.fxtwitter.com (read-only), turns them into reply
targets in Observe's shape with their real replies attached, and runs lanes.run on the
configured provider. Writes action/experiments/live_decide-<ts>.json with every decision.

    HARNESS_PROVIDER=anthropic uv run python -m harness.minds.lab.live_decide WNBA AtlantaDream --max-age 30
"""

import argparse
import json
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone

from harness import config
from harness.llm import get_llm
from harness.minds import lanes, settings, thread
from harness.minds.lab.fetch_x_threads import timeline
from harness.models import Narrative

OUT = config.ROOT / "action" / "experiments"


def targets_for(handle: str, now: float, max_posts: int) -> list[dict]:
    out = []
    for s in timeline(handle)[:max_posts]:
        if s.get("reposted_by") or not s.get("text"):
            continue
        age = (now - int(s["created_timestamp"])) / 60
        eng = sum(s.get(k, 0) or 0 for k in ("likes", "replies", "reposts", "quotes"))
        replies = thread.fetch_replies(s["url"]) if s.get("replies") else []
        # Observe's shape: `thread` is the reply list, `replies` the count.
        out.append({"url": s["url"], "author": handle, "author_followers": s["author"].get("followers"),
                    "text": s["text"], "age_minutes": round(age, 1), "engagement_per_hour": round(eng / max(age / 60, 0.1), 1),
                    "replies": s.get("replies", 0), "thread": replies[:15]})
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("handles", nargs="+")
    p.add_argument("--posts", type=int, default=2, help="latest posts per account")
    p.add_argument("--max-age", type=float, help="override reply_max_age_minutes for this run")
    args = p.parse_args()

    now = time.time()
    targets = [t for h in args.handles for t in targets_for(h, now, args.posts)]
    n = Narrative(id="live", title="Live posts from " + ", ".join(args.handles),
                  summary="; ".join(t["text"][:120] for t in targets), relevance=0.8, velocity=0.8,
                  signal_urls=[t["url"] for t in targets])
    n.reply_targets = targets
    cfg = settings.load()
    if args.max_age is not None:
        cfg = replace(cfg, reply_max_age_minutes=args.max_age)
    llm = get_llm()
    actions, decisions = lanes.run(llm, config.mission(), config.lessons(), [n], [], cfg=cfg, takes=[])
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "provider": type(llm).__name__,
              "config": cfg.to_dict(), "targets": [{k: v for k, v in t.items() if k != "thread"} | {"thread": len(t["thread"])}
                                                   for t in targets],
              "actions": [a.to_dict() for a in actions], "decisions": [asdict(d) for d in decisions]}
    path = OUT / f"live_decide-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    for d in decisions:
        print(f"\n[{d.lane}] {d.tool} {d.reason} {d.opportunity} :: {d.detail}")
        for c in d.candidates:
            v = c.get("veto") or {}
            flag = "PASS" if v.get("passed") else (v.get("why") or ("gate: " + ", ".join(f["rule"] for f in c.get("gate", []))) or c.get("schema_error", ""))
            print(f"    {flag[:40]:<40} {c.get('text', '')[:110]!r}")
    print("\nSHIPPED:")
    for a in actions:
        print(f"  {a.type:<6} {a.target_url or ''}\n         {a.text!r}")
    print(f"\n-> {path}")


if __name__ == "__main__":
    main()
