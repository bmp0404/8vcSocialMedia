"""Format a measured round for 8VC's Grok bot: one JSON file and one short list a person can read.

The bot's input format isn't defined yet, so this writes everything a decider needs per action:
the target URL, the action type, the text, why, and the evidence behind it, including what the
integration checks found. Nothing here posts; the files are recommendations.

A recommendation is "ready" only when every check below passes. Anything else is listed as
"held" with the reasons, so the bot or a person can still see it.
- qualify's critic kept it,
- a reply or quote target resolves on X, by the author in its URL,
- no claim the Opus audit labelled unsupported or contradicted,
- no targeted insult and no engagement bait (X labels both and drops the post from non-followers),
- the AI-register lineup did not pick it in both orders.

Ready actions are approved in the review table with a note saying the integration checks, not a
person, approved them, and `out/approved-run<id>.json` is exported through the dashboard's own
`export_approved`, so the hand-off file has the shape the dashboard writes.

    uv run python -m harness.integration.handoff --run 14 [--copy-to ../8vc-context/tonight-recommendations.md]
"""

import argparse
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import config, memory
from harness.integration.live import RECORD_DIR, record_path

APPROVAL_NOTE = "approved by the integration checks (harness/integration/handoff.py), not yet by a person"
# The reply lane's own staleness cutoff (harness/minds/lanes.py STALE_MINUTES). Past it a new reply
# mostly lands below the visible page, so the hand-off says when each reply stops being worth sending.
REPLY_WINDOW_MIN = 120


def hold_reasons(action: dict, draft: dict | None, target: dict | None) -> list[str]:
    """Why an action that qualify kept still isn't ready. Empty means ready."""
    reasons = []
    if action.get("vetoed"):
        reasons.append(f"critic veto: {action.get('critique', '')}")
    if action["type"] in ("reply", "quote"):
        if not target:
            reasons.append("target not checked")
        elif not target.get("resolves"):
            reasons.append(f"target doesn't resolve: {target.get('why', '')}")
        elif not target.get("author_matches_url"):
            reasons.append(f"target author @{target.get('author')} doesn't match the URL")
    if action["type"] in ("reply", "quote", "post", "mention"):
        if not draft:
            reasons.append("draft not audited")
        else:
            a = draft["audit"]
            if a["unsupported_claims"]:
                reasons.append("unsupported claims: " + "; ".join(a["unsupported_claims"]))
            if a["targeted_insult"]:
                reasons.append("targeted insult (X FosnrAbuseInsults risk)")
            if a["engagement_bait"]:
                reasons.append("engagement bait (X SpamHighRecall risk)")
            lu = draft["lineup"]
            if lu.get("ran") and lu["caught_in"] == 2:
                reasons.append("AI-register lineup picked it in both orders")
    return reasons


def _iso(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def build(run_id: int) -> dict:
    rec = json.loads(record_path(run_id).read_text(encoding="utf-8"))
    m_path = RECORD_DIR / f"measure-run{run_id}.json"
    if not m_path.exists():
        raise SystemExit(f"no measurement at {m_path}; run harness.integration.measure --run {run_id} first")
    m = json.loads(m_path.read_text(encoding="utf-8"))
    final = json.loads((config.OUT_DIR / rec["recommendations_file"]).read_text(encoding="utf-8"))["recommendations"]
    drafts = {d["action_id"]: d for d in m["drafts"] if d.get("action_id")}
    narratives = {n["id"]: n for n in rec["narratives"]}
    measured_at = _iso(m["measured_at"])
    ready, held = [], []
    for rank, a in enumerate(final, 1):
        d = drafts.get(a["id"])
        t = m["targets"].get(a.get("target_url") or "")
        n = narratives.get(a.get("narrative_id"), {})
        item = {
            "action_id": a["id"],
            "type": a["type"],
            "target_url": a.get("target_url"),
            "target_handle": a.get("target_handle"),
            "target_text": a.get("target_text") or (t or {}).get("text"),
            "text": a.get("text", ""),
            "rationale": a.get("rationale", ""),
            "evidence": {
                "narrative": {"id": n.get("id"), "title": n.get("title"), "summary": n.get("summary"),
                              "velocity": n.get("velocity"), "velocity_basis": n.get("velocity_basis")},
                "sources": (n.get("signal_urls") or [])[:6],
                "target_post": {k: t.get(k) for k in ("author", "age_minutes", "likes", "replies", "reposts", "views",
                                                      "author_followers")} if t and t.get("resolves") else None,
                "decide": a.get("draft_meta") or {},
                "qualify": {"panel_mean": a.get("predicted_score"), "critic": a.get("critique")},
                "grounding": {"claims": d["audit"]["claims"],
                              "deterministic_flags": d["audit"]["deterministic_ungrounded"]} if d else None,
                "ai_register": {"p_ai": d["audit"]["p_ai"], "lineup": d["lineup"]} if d else None,
                "x_penalties": {"targeted_insult": d["audit"]["targeted_insult"],
                                "engagement_bait": d["audit"]["engagement_bait"]} if d else None,
            },
            "pipeline_rank": rank,
        }
        if a["type"] in ("reply", "quote") and t and t.get("resolves") and measured_at:
            created = measured_at - timedelta(minutes=t["age_minutes"])
            item["send_before"] = (created + timedelta(minutes=REPLY_WINDOW_MIN)).isoformat(timespec="minutes")
        why = hold_reasons(a, d, t)
        (held if why else ready).append({**item, "held_because": why} if why else item)
    return {
        "format": "media-harness grok hand-off, draft 1",
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Recommendations only. The harness has no posting access; 8VC's Grok bot decides what goes out.",
        "ready": ready,
        "held": held,
        "abstentions": m["abstentions"]["details"],
        "questions_for_a_person": m["abstentions"]["questions"],
    }


def _one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def render(h: dict) -> str:
    out = [
        f"# Recommendations for 8VC's Grok bot, run {h['run_id']}",
        "",
        f"Generated {h['generated_at']}. {h['note']} Full evidence: `out/grok-handoff-run{h['run_id']}.json`.",
        "",
        f"## Ready ({len(h['ready'])})",
        "",
    ]
    if not h["ready"]:
        out += ["None passed every check this round.", ""]
    for i, r in enumerate(h["ready"], 1):
        out += _item(i, r)
    out += [f"## Held ({len(h['held'])})", ""]
    for i, r in enumerate(h["held"], 1):
        out += _item(i, r) + [f"   - Held because: {'; '.join(r['held_because'])}", ""]
    if h["questions_for_a_person"]:
        out += ["## Questions for a person", ""] + [f"- {q}" for q in h["questions_for_a_person"]] + [""]
    if h["abstentions"]:
        out += ["## Where the harness abstained", ""]
        out += [f"- {a['lane']} lane, `{a['reason']}`: {_one_line(a['detail'])}" + (f" ({a['opportunity']})" if a["opportunity"] else "")
                for a in h["abstentions"]] + [""]
    return "\n".join(out)


def _item(i: int, r: dict) -> list[str]:
    e = r["evidence"]
    target = r["target_url"] or (f"@{r['target_handle']}" if r["target_handle"] else "new post")
    lines = [f"{i}. **{r['type']}** → {target}"]
    if r.get("text"):
        lines.append(f"   - Text: \"{_one_line(r['text'])}\"")
    if r.get("target_text"):
        lines.append(f"   - Answers: \"{_one_line(r['target_text'])[:200]}\"")
    if r.get("send_before"):
        lines.append(f"   - Send before {r['send_before']} UTC (the reply window closes 120 minutes after the post)")
    if r.get("rationale"):
        lines.append(f"   - Why: {_one_line(r['rationale'])}")
    if e["narrative"].get("title"):
        lines.append(f"   - Story: {e['narrative']['title']}; sources: {', '.join(e['sources'][:3]) or 'none'}")
    if e.get("target_post"):
        tp = e["target_post"]
        lines.append(f"   - Target post: @{tp['author']}, {tp['age_minutes']:.0f} min old at check, {tp['likes']} likes, "
                     f"{tp['replies']} replies, {tp['views']} views")
    if e.get("ai_register"):
        lu = e["ai_register"]["lineup"]
        caught = f"picked in {lu['caught_in']}/2 lineup orders" if lu.get("ran") else f"no lineup ({lu.get('why')})"
        lines.append(f"   - Reads as AI: P(AI) {e['ai_register']['p_ai']}, {caught}")
    if e.get("grounding"):
        claims = e["grounding"]["claims"]
        ok = sum(c.get("status") == "supported" for c in claims)
        lines.append(f"   - Facts: {ok}/{len(claims)} claims supported by tonight's sources")
    lines.append("")
    return lines


def approve_ready(h: dict) -> Path:
    from harness.review import export_approved

    for r in h["ready"]:
        memory.set_review(r["action_id"], "approved", None, APPROVAL_NOTE)
    return export_approved(h["run_id"])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="harness.integration.handoff")
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--copy-to", help="also write the markdown list here")
    ap.add_argument("--no-approve", action="store_true", help="don't mark ready actions approved or export approved-run<id>.json")
    args = ap.parse_args(argv)
    h = build(args.run)
    config.OUT_DIR.mkdir(exist_ok=True)
    j = config.OUT_DIR / f"grok-handoff-run{args.run}.json"
    j.write_text(json.dumps(h, indent=1, ensure_ascii=False), encoding="utf-8")
    md = config.OUT_DIR / f"grok-handoff-run{args.run}.md"
    md.write_text(render(h), encoding="utf-8")
    print(f"[handoff] {len(h['ready'])} ready, {len(h['held'])} held -> {j.name}, {md.name}")
    if not args.no_approve:
        print(f"[handoff] approved export -> {approve_ready(h)}")
    if args.copy_to:
        shutil.copyfile(md, args.copy_to)
        print(f"[handoff] copied to {args.copy_to}")


if __name__ == "__main__":
    main()
