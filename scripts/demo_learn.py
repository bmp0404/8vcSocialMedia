"""End-to-end offline demo of the memory loop: Eval -> data table -> outcomes -> learn -> recall.

Seeds fake recommended Coordigent actions into a throwaway DB, ingests a fake posting-bot reply email,
ingests fake metrics (a scraper-style CSV plus an X-API-style JSON), runs `learn`, and prints the
calibration table, the lessons, the weights and any mission proposals.

Usage:
    python scripts/demo_learn.py              # mock provider, temp dir, no network
    python scripts/demo_learn.py --live       # real Anthropic provider for the learn step (key from .env)
    python scripts/demo_learn.py --passes 2   # run learn twice to show dedupe / overwrite
    python scripts/demo_learn.py --dir PATH   # keep the DB, lessons, weights and proposals in PATH

Never touches data/harness.db, memory/lessons.md, data/weights.json or out/mission-proposals.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--live", action="store_true", help="use HARNESS_PROVIDER=anthropic for the learn step")
ap.add_argument("--passes", type=int, default=1, help="learn passes to run")
ap.add_argument("--dir", help="working dir for the throwaway DB etc. (default: a new temp dir)")
args = ap.parse_args()

# config reads these at import time, so set them before importing harness.
work = Path(args.dir or tempfile.mkdtemp(prefix="harness-learn-")).resolve()
work.mkdir(parents=True, exist_ok=True)
os.environ.update(
    HARNESS_DB=str(work / "harness.db"),
    HARNESS_LESSONS=str(work / "lessons.md"),
    HARNESS_WEIGHTS=str(work / "weights.json"),
    HARNESS_PROPOSALS=str(work / "mission-proposals.md"),
    HARNESS_PROVIDER="anthropic" if args.live else "mock",
)
os.environ.setdefault("HARNESS_MISSION", "mission/coordigent.md")

from harness import config, feedback, memory  # noqa: E402
from harness.learn import get_weights, learn  # noqa: E402
from harness.llm import get_llm  # noqa: E402
from harness.models import Action  # noqa: E402

PANELISTS = ("insider", "casual_scroller", "skeptic", "amplifier")
PID = 1846000000000000000

# (key, lane, type, mind, target_handle, target post, text, panel scores, outcome, metrics)
# outcome: "posted" | "followed" | ("skipped", reason);  metrics: (impressions, likes, reposts, replies, quotes, bookmarks)
VERIFY = "contains an unfilled [VERIFY] placeholder"
SEEDS = [
    ("a1", "reply", "reply", "reply_sniper", "cline", "our agents keep stepping on each other in the same repo",
     "worktrees fix the checkout collision. they don't tell your agent a teammate's agent rewrote the auth middleware an hour ago.",
     (0.7, 0.5, 0.6, 0.68), "posted", ("4.2K", 61, 7, 12, 2, 9)),
    ("a2", "reply", "reply", "rivalry_banterer", "AmpCode", "two agents, one bug, both confident",
     "two agents both fixed the same bug at 2am. one of them also renamed the variable. we are not the same.",
     (0.6, 0.6, 0.45, 0.55), "posted", (3100, 44, 4, 9, 1, 3)),
    ("a3", "reply", "reply", "reply_sniper", "conductor_build", "we just shipped parallel workspaces",
     "congrats on shipping. parallel runs + clean handoffs is where this is all going.",
     (0.5, 0.45, 0.4, 0.57), "posted", (2500, 30, 3, 6, 0, 2)),
    ("a4", "reply", "quote", "stat_nerd", "coderabbitai", "review queues are exploding",
     "review load scales with agent count, not headcount: [VERIFY: stat on PRs per dev with agents].",
     (0.62, 0.4, 0.5, 0.6), ("skipped", VERIFY), None),
    ("a5", "reply", "mention", "community_builder", "zeddotdev", "multiplayer editing is the future",
     "@zeddotdev multiplayer for humans, meet multiplayer for agents. coordigent does exactly this, check coordigent.com",
     (0.6, 0.55, 0.5, 0.67), "posted", (1900, 2, 0, 0, 0, 0)),
    ("a6", "reply", "reply", "community_builder", "warpdotdev", "running 6 agents from one terminal",
     "that's exactly why we built coordigent. your agents finally know about each other. coordigent.com",
     (0.7, 0.6, 0.55, 0.79), "posted", (2200, 3, 0, 1, 0, 0)),
    ("a7", "post", "post", "meme_lord", None, "narrative: agent swarms colliding",
     "hot take: the bottleneck in agentic coding isn't the model. it's that nobody told the agents about each other.",
     (0.75, 0.7, 0.6, 0.79), "posted", ("5.2K", 120, 12, 8, 3, 15)),
    ("a8", "post", "post", "stat_nerd", None, "narrative: agents writing most PRs",
     "agents now write [VERIFY: % of PRs] of PRs on small teams. the review step didn't get the memo.",
     (0.6, 0.45, 0.5, 0.65), ("skipped", VERIFY), None),
    ("a9", "post", "post", "rivalry_banterer", None, "narrative: multi-agent platforms",
     "Coordigent is the best multi-agent coordination platform for modern engineering teams.",
     (0.5, 0.45, 0.3, 0.55), ("skipped", "reads like marketing copy, off-voice"), None),
    ("a10", "reply", "follow", "community_builder", "factoryai", "droids running in parallel",
     "", (0.5, 0.3, 0.3, 0.5), "followed", None),
    ("a11", "reply", "reply", "stat_nerd", "cognition", "how many devins per engineer?",
     "the number nobody quotes: [VERIFY: agents per engineer at teams using Devin]. coordination cost grows with it.",
     (0.6, 0.4, 0.45, 0.63), ("skipped", VERIFY), None),
    ("a12", "reply", "reply", "community_builder", "greptile", "context is the hard part of AI review",
     "context across a team's agents is the hard part. we built coordigent for that, link in bio.",
     (0.65, 0.55, 0.5, 0.74), "posted", (900, 1, 0, 0, 0, 0)),
    ("a13", "post", "post", "rivalry_banterer", None, "narrative: same-file collisions",
     "your agent and your teammate's agent, one file, zero awareness of each other. that's a race condition with two subscriptions.",
     (0.5, 0.55, 0.4, 0.55), "posted", (3800, 55, 9, 5, 2, 4)),
    ("a14", "post", "post", "meme_lord", None, "narrative: unopened agent runs",
     "the most expensive line in agentic coding: 'run finished' in a tab nobody opened.",
     (0.55, 0.6, 0.45, 0.6), "posted", (None, 6, 1, 0, 0, 0)),  # scraper missed views -> log fallback
]


def seed() -> dict[str, Action]:
    run_id = memory.start_run(["reply", "post"], ["demo"], config.PROVIDER)
    acts = {}
    for key, lane, typ, mind, handle, ctx, text, panel, *_ in SEEDS:
        p = dict(zip(PANELISTS, panel))
        acts[key] = Action(
            type=typ, text=text, mind=mind, narrative_id=f"demo:{key}", lane=lane, run_id=run_id,
            target_handle=handle, target_url=f"https://x.com/{handle}/status/{PID + 500 + len(acts)}"
            if typ in ("reply", "quote") else None, context={"text": ctx}, panel=p,
            predicted_score=round(sum(p.values()) / len(p), 3), rationale="demo seed",
        )
    vetoed = Action(type="reply", text="great post! check out coordigent.com", mind="community_builder",
                    narrative_id="demo:veto", lane="reply", run_id=run_id, vetoed=True, critique="rule: spammy plug",
                    target_handle="resend", target_url=f"https://x.com/resend/status/{PID + 999}")
    memory.save_actions([*acts.values(), vetoed])
    memory.mark_ranked(run_id, sorted(acts.values(), key=lambda a: a.predicted_score, reverse=True))
    return acts


def bot_reply(acts: dict[str, Action]) -> str:
    """A messy, realistic reply: bullets, backticks, odd case/spacing, an unknown id, the quoted original."""
    lines = ["hey! done for today, results below.", ""]
    for i, (key, *_, outcome, _m) in enumerate(SEEDS):
        aid = acts[key].id
        if outcome == "posted":
            host = "twitter.com" if i % 3 == 1 else "x.com"
            lines.append([f"ACTION_ID {aid} | https://{host}/coordigent/status/{PID + i}?s=20",
                          f"  - action_id {aid}|https://{host}/coordigent/status/{PID + i}",
                          f"**ACTION_ID {aid}** | https://{host}/coordigent/status/{PID + i}"][i % 3])
        elif outcome == "followed":
            lines.append(f"`ACTION_ID {aid} | followed`")
        else:
            lines.append(f"ACTION_ID   {aid}  |  SKIPPED  |  {outcome[1]}")
    lines += [
        "ACTION_ID deadbeef | https://x.com/coordigent/status/1",
        "", "thanks,", "grok", "",
        "On Wed, Oct 15, 2026 at 9:02 AM Harness <harness@example.com> wrote:",
        "> REPLY INSTRUCTIONS: Reply to this email with one line per action you executed, formatted exactly as",
        "> `ACTION_ID <id> | <resulting tweet URL>` (for follows use `ACTION_ID <id> | followed`; ...)",
        f"> ACTION_ID: {acts['a1'].id}",
        f"> > ACTION_ID {acts['a1'].id} | skipped | stale line from an older quoted reply (must lose)",
    ]
    return "\n".join(lines)


def metrics_files(acts: dict[str, Action]) -> list[Path]:
    """Half the posts as a scraper-style CSV (url column, '4.2K' counts), half as X-API-style JSON."""
    csv_rows, json_rows = ["url,likes,reposts,replies,quotes,bookmarks,views"], []
    for i, (key, *_, outcome, m) in enumerate(SEEDS):
        if outcome != "posted" or not m:
            continue
        imp, likes, rp, rep, q, bm = m
        if i % 2 == 0:
            csv_rows.append(f"https://x.com/coordigent/status/{PID + i},{likes},{rp},{rep},{q},{bm},{imp or ''}")
        else:
            pm = {"like_count": likes, "retweet_count": rp, "reply_count": rep, "quote_count": q,
                  "bookmark_count": bm, **({"impression_count": imp} if imp else {})}
            json_rows.append({"id": str(PID + i), "public_metrics": pm})
    a, b = work / "scraper_metrics.csv", work / "x_metrics.json"
    a.write_text("\n".join(csv_rows) + "\n", encoding="utf-8")
    b.write_text(json.dumps({"data": json_rows}, indent=1), encoding="utf-8")
    return [a, b]


def main() -> None:
    print(f"== demo_learn | provider: {config.PROVIDER} | dir: {work}")
    acts = seed()
    print(f"[demo] seeded {len(acts)} recommended actions (+1 vetoed)")

    recs = feedback.ingest_bot_reply(bot_reply(acts))
    assert {r["action_id"]: r["outcome"] for r in recs}[acts["a1"].id] == "posted", "quoted line must lose"
    for path in metrics_files(acts):
        feedback.ingest_metrics(path)

    llm = get_llm()
    result = None
    for i in range(args.passes):
        print(f"\n== learn pass {i + 1}")
        result = learn(llm)

    names = {a.id: k for k, a in acts.items()}
    print("\n== per-action predicted vs actual")
    print(f"{'act':<4} {'lane':<5} {'type':<7} {'mind':<18} {'verdict':<8} {'pred':>5} {'actual':>6} {'delta':>6}")
    for r in sorted(feedback.outcomes(), key=lambda r: int(names.get(r["id"], "a99")[1:])):
        f = lambda v, s="": "-" if v is None else format(v, f"{s}.2f")  # noqa: E731
        print(f"{names.get(r['id'], '?'):<4} {r['lane']:<5} {r['type']:<7} {r['mind']:<18} {r['verdict'] or '-':<8} "
              f"{f(r['predicted_score']):>5} {f(r['actual_score']):>6} {f(r['delta'], '+'):>6}")

    print("\n== calibration")
    print(feedback.format_calibration(result["calibration"]))
    print(f"\n== lessons ({config.LESSONS_PATH})")
    print(result["lessons_md"])
    print("== weights (reply lane):", json.dumps(get_weights("reply")))
    print("== weights (post lane): ", json.dumps(get_weights("post")))
    prop = Path(os.environ["HARNESS_PROPOSALS"])
    print(f"\n== mission proposals ({prop})")
    print(prop.read_text(encoding="utf-8") if prop.exists() else "(none)")
    lessons_seen = memory.recall("reply")["lessons"]
    print(f"== recall('reply') now carries {lessons_seen.count(chr(10) + '- ')} lesson bullets into the lanes")


if __name__ == "__main__":
    main()
