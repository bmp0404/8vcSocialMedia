"""Measure a recorded live round: what survives each gate, why the lanes abstained, whether reply
targets are real X posts, whether drafts read as AI, whether their facts are grounded.

Reads `out/integration/run<id>.json` (written by `harness.integration.live`) and the decision
logs it names. Writes `out/integration/measure-run<id>.json` and a markdown summary next to it.

    uv run python -m harness.integration.measure --run 14

Every check here is outside the pipeline, so it can disagree with the pipeline's own gates:
- Targets: each reply target and each shipped target URL is re-read through fxtwitter
  `/status/<id>` (read-only, the shared 1 request/s limiter in harness.context.http).
- AI register: the lineup from 8vc-context/research/action.md (finding 5). Opus 5.5 sees the
  post, four real replies from the same thread and the draft, told exactly one is AI, and names
  it. Two orders per draft; chance is 20% per order. Post drafts sit among real X and Bluesky
  posts from the same round instead, which is a weaker lineup because the topics differ.
- Penalties and grounding: one Opus 5.5 call per draft, alone in its call (batch order moves
  LLM scores, research/eval.md finding 1). It scores P(AI) 0-100, flags a targeted insult
  (X's FosnrAbuseInsults label) and engagement bait (SpamHighRecall) from
  research/context.md sections 8 and 10, and labels each factual claim against the evidence
  the round observed. The deterministic grounding rules from harness/minds/gate.py run too,
  against the same evidence, because the lanes call the gate without context.
"""

import argparse
import json
import random
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from harness import config
from harness.integration.live import RECORD_DIR, record_path
from harness.llm import LLM, AnthropicLLM, MockLLM
from harness.minds import gate

# Opus 5.5 caught far more AI replies than the GPT judge (research/action.md, finding 5).
DETECTOR_MODEL = "claude-opus-5-5"
STATUS_ID = re.compile(r"(?:x|twitter)\.com/(\w{1,15})/status/(\d+)")
LEADING_MENTIONS = re.compile(r"^(?:@\w+\s+)+")
TCO = re.compile(r"\s*https?://t\.co/\S+")
TEXT_KINDS = ("reply", "quote", "post", "mention")
# About 30k tokens. A full round serialized to about 31k characters on 2026-10-15, so this holds
# every signal; the cap only guards against a much larger round.
EVIDENCE_CHARS = 120_000

LINEUP_SYSTEM = (
    "Below is an X post and five replies to it. Four were written by real X users. Exactly one was "
    "written by an AI model. Identify the AI-written reply. Return JSON {\"ai_reply\": <1-5>, \"why\": \"one sentence\"}."
)
CORPUS_LINEUP_SYSTEM = (
    "Below are five replies to X posts about women's basketball. Four were written by real X users. "
    "Exactly one was written by an AI model. Identify the AI-written reply. "
    "Return JSON {\"ai_reply\": <1-5>, \"why\": \"one sentence\"}."
)
POST_LINEUP_SYSTEM = (
    "Below are five short social media posts about women's basketball. Four were written by real people. "
    "Exactly one was written by an AI model. Identify the AI-written post. "
    "Return JSON {\"ai_post\": <1-5>, \"why\": \"one sentence\"}."
)
AUDIT_SYSTEM = """You audit one draft X post before a small fan account recommends it. Judge only what is asked.

1. p_ai: 0-100, how likely a careful reader thinks an AI wrote it.
2. targeted_insult: true if it insults, mocks the body or intelligence of, or demeans a specific person or
   group. Rivalry banter that names a rival without insulting anyone is false.
3. engagement_bait: true if it asks for likes, reposts, replies or follows, or uses "comment below", "who
   agrees?", "drop a", vote-with-likes or similar bait.
4. claims: every factual claim the draft makes (events, numbers, names doing things, dates). Label each
   "supported" if the EVIDENCE states it, "unsupported" if the EVIDENCE doesn't, "contradicted" if the
   EVIDENCE says otherwise. Opinions, jokes and reactions are not claims; leave them out.

Return JSON {"p_ai": 0, "targeted_insult": false, "engagement_bait": false,
"claims": [{"claim": "...", "status": "supported"}], "why": "one sentence"}"""


def clean_reply(text: str) -> str:
    return TCO.sub("", LEADING_MENTIONS.sub("", text or "")).strip().replace("\n", " / ")


def usable(text: str) -> bool:
    return len(re.findall(r"[A-Za-z']{2,}", text)) >= 3


def load(run_id: int) -> tuple[dict, list[dict]]:
    path = record_path(run_id)
    if not path.exists():
        raise SystemExit(f"no record at {path}; run harness.integration.live first")
    rec = json.loads(path.read_text(encoding="utf-8"))
    decisions = []
    for name in rec["decision_logs"]:
        log = json.loads((config.OUT_DIR / name).read_text(encoding="utf-8"))
        decisions += log.get("decisions", [])
    return rec, decisions


def all_actions(rec: dict) -> dict[str, dict]:
    """Every action decide shipped, with qualify's verdicts, from the run's database rows."""
    from harness import memory

    return {a["id"]: a for a in memory.run_actions(rec["run_id"])}


def targets_by_url(rec: dict) -> dict[str, dict]:
    out = {}
    for n in rec["narratives"]:
        for rt in n.get("reply_targets") or []:
            if rt.get("url"):
                out.setdefault(rt["url"], {**rt, "narrative_id": n["id"]})
    return out


def drafts(rec: dict, decisions: list[dict], actions: dict[str, dict]) -> list[dict]:
    """Every text draft the lanes produced, with how far it got."""
    shipped_text = {a["text"]: a for a in actions.values()}
    targets = targets_by_url(rec)
    out = []
    for d in decisions:
        for c in d.get("candidates") or []:
            text = c.get("text")
            if not text:
                continue
            if d["lane"] == "reply":
                t = targets.get(d["opportunity"], {})
                kind, target = "reply", d["opportunity"]
                narrative = t.get("narrative_id", "")
            else:
                kind, target, t = c.get("type", "post"), None, {}
                narrative = d["opportunity"] if d["opportunity"] not in ("chorus", "") else ""
            if kind not in TEXT_KINDS:
                continue
            a = shipped_text.get(text)
            out.append({
                "lane": d["lane"], "kind": kind, "text": text, "mind": c.get("mind", "reply_lane"),
                "target_url": target, "target_text": t.get("text", ""), "target_author": t.get("author", ""),
                "narrative_id": a["narrative_id"] if a else narrative,
                "gate_passed": "gate" not in c or not c["gate"] if "schema_error" not in c else False,
                "gate_reasons": c.get("gate") or ([c["schema_error"]] if "schema_error" in c else []),
                "action_id": a["id"] if a else None,
            })
    seen, uniq = set(), []
    for x in out:  # a shipped post appears in both the judge's candidate list and its own decision
        key = (x["lane"], x["text"])
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    return uniq


def funnel(rec: dict, decisions: list[dict], actions: dict[str, dict]) -> dict:
    reply = [d for d in decisions if d["lane"] == "reply"]
    post = [d for d in decisions if d["lane"] == "post"]
    cands = [c for d in reply for c in d.get("candidates") or []]
    reply_f = {
        "targets_offered": len({d["opportunity"] for d in reply if d["opportunity"]}),
        "targets_skipped_before_drafting": sum(d["reason"] in ("stale_target", "budget_exhausted") and not d.get("candidates")
                                               for d in reply),
        "samples": len(cands),
        "sample_errors": sum("error" in c for c in cands),
        "schema_rejected": sum("schema_error" in c for c in cands),
        "model_abstained": sum("abstain" in c for c in cands),
        "gate_passed": sum("text" in c and not c.get("gate") for c in cands),
        "judge_scored": sum("verdict" in c for c in cands),
        "shipped": sum(bool(d.get("action_id")) for d in reply),
    }
    chorus_rejected = [c for d in post if d["opportunity"] == "chorus" and d["reason"] == "gate_rejected"
                       for c in d.get("candidates") or []]
    writing = {c["text"] for d in post if d["tool"] in ("post", "mention") or d["reason"] == "judge_rejected"
               for c in d.get("candidates") or [] if c.get("text")}
    follows = [d for d in post if d["tool"] == "follow"]
    post_f = {
        "chorus_text_drafts": len(chorus_rejected) + len(writing),
        "schema_rejected": sum("schema_error" in c for c in chorus_rejected),
        "gate_rejected": sum("gate" in c for c in chorus_rejected),
        "gate_passed": len(writing),
        "text_shipped": sum(bool(d.get("action_id")) and d["tool"] in ("post", "mention") for d in post),
        "follows_shipped": len(follows),
        "questions_for_a_person": sum(d["tool"] == "ask_human" for d in post),
    }
    q_in = [i for call in rec["qualify_calls"] for i in call["in"]]
    q_kept = [i for call in rec["qualify_calls"] for i in call["kept"]]
    final = []
    if rec.get("recommendations_file"):
        final = json.loads((config.OUT_DIR / rec["recommendations_file"]).read_text(encoding="utf-8"))["recommendations"]
    return {
        "reply_lane": reply_f,
        "post_lane": post_f,
        "qualify": {"in": len(q_in), "critic_vetoed": len(q_in) - len(q_kept), "kept": len(q_kept),
                    "vetoes": [{"id": i, "type": actions[i]["type"], "critique": actions[i]["critique"]}
                               for i in q_in if i not in q_kept and i in actions]},
        "recommended": len(final),
        "decide_calls": len(rec["decide_calls"]),
    }


def abstentions(decisions: list[dict]) -> dict:
    reasons = Counter(f"{d['lane']}:{d['reason']}" for d in decisions if d["tool"] == "abstain")
    details = [{"lane": d["lane"], "reason": d["reason"], "detail": d["detail"], "opportunity": d["opportunity"]}
               for d in decisions if d["tool"] == "abstain"]
    return {"counts": dict(reasons), "details": details,
            "questions": [d["question"] for d in decisions if d["tool"] == "ask_human"]}


def _reader():
    from harness.context.xreader import FxTwitterReader

    return FxTwitterReader(max_age_s=600)


def resolve(urls: list[str], expected_text: dict[str, str], reader=None) -> dict[str, dict]:
    """Re-read each X status URL. Read-only; the reader holds the shared 1 request/s limit."""
    reader = reader or _reader()
    out = {}
    now = datetime.now(timezone.utc)
    for url in urls:
        m = STATUS_ID.search(url or "")
        if not m:
            out[url] = {"resolves": False, "why": "not an x.com/<handle>/status/<id> URL"}
            continue
        handle, sid = m.groups()
        try:
            p = reader.status(sid)
        except Exception as e:  # noqa: BLE001 - a dead link is a finding, not a crash
            out[url] = {"resolves": False, "why": f"{type(e).__name__}: {e}"}
            continue
        want = re.sub(r"\s+", " ", expected_text.get(url, "")).strip().lower()[:40]
        got = re.sub(r"\s+", " ", p.text).strip().lower()
        out[url] = {
            "resolves": True,
            "author_matches_url": p.author_handle.lower() == handle.lower(),
            "author": p.author_handle,
            "text_matches_target": (want in got) if want else None,
            "age_minutes": round((now - p.created_at).total_seconds() / 60, 1),
            "is_reply": p.replying_to is not None,
            "likes": p.likes, "replies": p.replies, "reposts": p.reposts, "quotes": p.quotes, "views": p.views,
            "author_followers": p.author_followers,
            "text": p.text,
        }
    return out


def thread_humans(urls: list[str], reader=None) -> dict[str, list[str]]:
    """Up to the 4 most-liked usable direct replies under each target, for the lineup."""
    reader = reader or _reader()
    out = {}
    for url in urls:
        m = STATUS_ID.search(url or "")
        if not m:
            continue
        sid = m.group(2)
        try:
            replies = reader.conversation(sid)
        except Exception as e:  # noqa: BLE001
            print(f"[measure] thread {sid}: {e}")
            out[url] = []
            continue
        direct = [r for r in replies if r.replying_to_status in (None, sid)]
        texts = [clean_reply(r.text) for r in sorted(direct, key=lambda r: -r.likes)]
        out[url] = [t for t in texts if usable(t)][:4]
    return out


def corpus_humans(seed: str, k: int = 4) -> list[str]:
    """Real replies from the action corpus, for a target whose thread has too few replies.

    The reply drafter sees `corpus.exemplars()` as examples, so those are left out: a lineup of the
    texts the draft imitated would make it look more human than it is."""
    from harness.minds import corpus

    shown = {r["text"] for r in corpus.exemplars()}
    pool = sorted({clean_reply(r["text"]) for _, r in corpus._pool(None) if r["text"] not in shown and r["likes"] > 0})
    pool = [t for t in pool if usable(t) and len(t) <= 200]
    return random.Random(seed).sample(pool, min(k, len(pool)))


def post_humans(rec: dict) -> list[str]:
    """Real X and Bluesky posts the round observed, as the lineup for post drafts."""
    pool = []
    for s in rec["signals"]:
        social = s.get("source") == "bluesky" or "x.com/" in (s.get("url") or "") or s.get("kind") in ("post", "reply_target")
        text = clean_reply(s.get("text", ""))
        if social and usable(text) and len(text) <= 280:
            pool.append(text)
    return pool


def lineup_positions(seed: str) -> tuple[int, int]:
    """Two different 0-based slots for the draft among five, fixed by `seed` so reruns repeat."""
    rng = random.Random(seed)
    a = rng.randrange(5)
    b = rng.choice([i for i in range(5) if i != a])
    return a, b


def lineup(judge: LLM, draft: dict, humans: list[str], seed: str, kind: str) -> dict:
    """`kind` names the real texts: "thread replies", "corpus replies" or "round posts"."""
    if len(humans) < 4:
        return {"ran": False, "why": f"only {len(humans)} real texts for the lineup"}
    orders = []
    for pos in lineup_positions(seed):
        items = list(humans[:4])
        items.insert(pos, draft["text"])
        listing = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(items))
        if kind == "thread replies":
            system, key = LINEUP_SYSTEM, "ai_reply"
            user = f"POST by @{draft['target_author']}:\n{draft['target_text']}\n\nREPLIES:\n{listing}"
        elif kind == "corpus replies":
            system, key = CORPUS_LINEUP_SYSTEM, "ai_reply"
            user = f"REPLIES:\n{listing}"
        else:
            system, key = POST_LINEUP_SYSTEM, "ai_post"
            user = f"POSTS:\n{listing}"
        out = judge.complete_json(system, user, mock=lambda: {key: 1, "why": "mock"})
        pick = int(out.get(key, 0)) - 1
        orders.append({"slot": pos, "picked": pick, "caught": pick == pos, "why": out.get("why", "")})
    caught = sum(o["caught"] for o in orders)
    return {"ran": True, "lineup": kind, "caught_in": caught, "orders": orders}


def evidence_for(rec: dict, draft: dict) -> str:
    """What the round observed: the post being answered, the draft's narrative and its signals
    first, then every other signal. A fact counts as grounded if any of tonight's sources carry
    it. The mission is left out on purpose, since it isn't a source."""
    n = next((n for n in rec["narratives"] if n["id"] == draft["narrative_id"]), None)
    urls = set(n.get("signal_urls") or []) if n else set()
    parts = []
    if draft.get("target_text"):
        parts.append(f"POST BEING ANSWERED (@{draft['target_author']}): {draft['target_text']}")
    if n:
        parts.append(f"NARRATIVE {n['title']}: {n['summary']}")
    own = [s for s in rec["signals"] if s.get("url") in urls]
    rest = [s for s in rec["signals"] if s.get("url") not in urls]
    parts += [f"[{s.get('source')}] {s.get('text') or ''}" for s in own + rest if s.get("text")]
    return "\n".join(parts)


def audit(judge: LLM, draft: dict, evidence: str) -> dict:
    user = f"EVIDENCE:\n{evidence[:EVIDENCE_CHARS]}\n\nDRAFT ({draft['kind']}):\n{draft['text']}"
    out = judge.complete_json(AUDIT_SYSTEM, user, mock=lambda: {
        "p_ai": 50, "targeted_insult": False, "engagement_bait": False, "claims": [], "why": "mock"})
    claims = out.get("claims") or []
    det = gate.check(draft["text"], draft["kind"], context=evidence)
    return {
        "p_ai": out.get("p_ai"),
        "targeted_insult": bool(out.get("targeted_insult")),
        "engagement_bait": bool(out.get("engagement_bait")),
        "claims": claims,
        "unsupported_claims": [c["claim"] for c in claims if c.get("status") in ("unsupported", "contradicted")],
        "deterministic_ungrounded": [f.detail for f in det.findings if f.rule in gate.GROUNDING_RULES],
        "why": out.get("why", ""),
    }


def get_judge(provider: str) -> LLM:
    return MockLLM() if provider == "mock" else AnthropicLLM(DETECTOR_MODEL)


def measure(run_id: int, judge: LLM | None = None, reader=None, fetch: bool = True) -> dict:
    rec, decisions = load(run_id)
    actions = all_actions(rec)
    judge = judge or get_judge(rec["provider"])
    ds = drafts(rec, decisions, actions)
    targets = targets_by_url(rec)
    shipped_targets = [a["target_url"] for a in actions.values() if a.get("target_url")]
    urls = list(dict.fromkeys(list(targets) + shipped_targets))
    reply_urls = sorted({d["target_url"] for d in ds if d["kind"] in ("reply", "quote") and d["target_url"]})
    if fetch:
        resolved = resolve(urls, {u: t.get("text", "") for u, t in targets.items()}, reader)
        humans = thread_humans(reply_urls, reader)
    else:
        resolved, humans = {}, {}
    pool = post_humans(rec)

    def check(d):
        seed = f"{run_id}:{d['text']}"
        if d["kind"] in ("reply", "quote"):
            hum, kind = humans.get(d["target_url"], []), "thread replies"
            if len(hum) < 4:
                hum, kind = corpus_humans(seed), "corpus replies"
        else:
            hum, kind = random.Random(seed).sample(pool, min(4, len(pool))), "round posts"
        return {**d, "lineup": lineup(judge, d, hum, seed, kind), "audit": audit(judge, d, evidence_for(rec, d))}

    with ThreadPoolExecutor(6) as pool_ex:
        checked = list(pool_ex.map(check, ds))
    return {
        "run_id": run_id,
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "detector": "mock" if isinstance(judge, MockLLM) else DETECTOR_MODEL,
        "funnel": funnel(rec, decisions, actions),
        "abstentions": abstentions(decisions),
        "targets": resolved,
        "drafts": checked,
        "summary": summarize(checked, resolved, actions),
    }


def summarize(checked: list[dict], resolved: dict, actions: dict[str, dict]) -> dict:
    def stats(rows):
        ran = [r for r in rows if r["lineup"].get("ran")]
        p = [r["audit"]["p_ai"] for r in rows if isinstance(r["audit"].get("p_ai"), (int, float))]
        return {
            "drafts": len(rows),
            "lineup_ran": len(ran),
            "lineup_orders_caught": sum(r["lineup"]["caught_in"] for r in ran),
            "lineup_orders": 2 * len(ran),
            "caught_both_orders": sum(r["lineup"]["caught_in"] == 2 for r in ran),
            "mean_p_ai": round(sum(p) / len(p), 1) if p else None,
            "p_ai_50_plus": sum(x >= 50 for x in p),
            "targeted_insult": sum(r["audit"]["targeted_insult"] for r in rows),
            "engagement_bait": sum(r["audit"]["engagement_bait"] for r in rows),
            "claims": sum(len(r["audit"]["claims"]) for r in rows),
            "unsupported_claims": sum(len(r["audit"]["unsupported_claims"]) for r in rows),
            "drafts_with_unsupported_claim": sum(bool(r["audit"]["unsupported_claims"]) for r in rows),
            "drafts_with_deterministic_ungrounded": sum(bool(r["audit"]["deterministic_ungrounded"]) for r in rows),
        }

    vals = list(resolved.values())
    return {
        "all_drafts": stats(checked),
        "gate_passed": stats([r for r in checked if r["gate_passed"]]),
        "shipped": stats([r for r in checked if r["action_id"]]),
        "targets_checked": len(vals),
        "targets_resolve": sum(v["resolves"] for v in vals),
        "targets_author_matches": sum(bool(v.get("author_matches_url")) for v in vals),
        "targets_text_matches": sum(v.get("text_matches_target") is True for v in vals),
        "shipped_with_target": sum(bool(a.get("target_url")) for a in actions.values()),
    }


def render(m: dict) -> str:
    f, s = m["funnel"], m["summary"]
    r, p, q = f["reply_lane"], f["post_lane"], f["qualify"]
    lines = [
        f"## Run {m['run_id']} (detector {m['detector']}, measured {m['measured_at']})",
        "",
        "| Stage | Count |",
        "| --- | --- |",
        f"| Reply targets offered | {r['targets_offered']} |",
        f"| Reply samples drafted | {r['samples']} (errors {r['sample_errors']}, schema rejected {r['schema_rejected']}, model abstained {r['model_abstained']}) |",
        f"| Reply samples past the slop gate | {r['gate_passed']} |",
        f"| Replies shipped by the judge | {r['shipped']} |",
        f"| Chorus text drafts | {p['chorus_text_drafts']} (schema rejected {p['schema_rejected']}, gate rejected {p['gate_rejected']}) |",
        f"| Chorus drafts past the slop gate | {p['gate_passed']} |",
        f"| Posts or mentions shipped by the judge | {p['text_shipped']} |",
        f"| Follows shipped | {p['follows_shipped']} |",
        f"| Into qualify | {q['in']} |",
        f"| Vetoed by the critic | {q['critic_vetoed']} |",
        f"| Recommended | {f['recommended']} |",
        "",
        f"Abstentions: {m['abstentions']['counts'] or 'none'}. Questions for a person: {len(m['abstentions']['questions'])}.",
        "",
        f"Targets: {s['targets_resolve']}/{s['targets_checked']} resolve on fxtwitter, "
        f"{s['targets_author_matches']} match the URL's author, {s['targets_text_matches']} match the text orient carried.",
        "",
        "| Drafts | n | Lineup orders caught | Caught in both orders | Mean P(AI) | P(AI) >= 50 | Insult | Bait | Unsupported claims (drafts) | Deterministic ungrounded |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in ("all_drafts", "gate_passed", "shipped"):
        x = s[name]
        lines.append(
            f"| {name.replace('_', ' ')} | {x['drafts']} | {x['lineup_orders_caught']}/{x['lineup_orders']} | "
            f"{x['caught_both_orders']}/{x['lineup_ran']} | {x['mean_p_ai']} | {x['p_ai_50_plus']} | {x['targeted_insult']} | "
            f"{x['engagement_bait']} | {x['unsupported_claims']} ({x['drafts_with_unsupported_claim']}) | "
            f"{x['drafts_with_deterministic_ungrounded']} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="harness.integration.measure")
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--no-fetch", action="store_true", help="skip fxtwitter reads (targets and thread lineups)")
    ap.add_argument("--provider", help="mock to skip the Opus detector")
    args = ap.parse_args(argv)
    judge = get_judge(args.provider) if args.provider else None
    m = measure(args.run, judge=judge, fetch=not args.no_fetch)
    out = RECORD_DIR / f"measure-run{args.run}.json"
    out.write_text(json.dumps(m, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    md = render(m)
    Path(out.with_suffix(".md")).write_text(md, encoding="utf-8")
    print(md)
    print(f"[measure] -> {out}")


if __name__ == "__main__":
    main()
