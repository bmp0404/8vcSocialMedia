"""Does grounding against live facts stop the gate rejecting real replies, and does it still catch invented
stats? Writes action/experiments/fact_grounding.json.

Real replies, checked the way `gate_calibration` checks them (the post and the thread's other replies as
context), once without facts and once with the facts that were live when the post went out:
- wnba: the 24 threads in action/fixtures/x_threads.json, with the ESPN facts daily refreshes would have
  left in the store at each post: each team's and player's most recent game from the GAME_DAYS before it,
  the next game, injury entries updated by then, and no roster, which ESPN only serves for today
  (`Espn.facts_at`);
- devtools: the threads in data/context/reply_samples.jsonl (local only), with the author's own posts from
  the 30 days before the root (data/context/author_timelines.jsonl). Only counts are written for it.

Invented stats (`--invent`): a model writes replies that each state a number it makes up, from the post
alone, and the gate checks them with the same facts. A draft counts as caught when a grounding rule
rejects it. Drafts are cached in data/context/fact_grounding_drafts.jsonl (gitignored).

    uv run python -m harness.minds.lab.fact_grounding
    HARNESS_PROVIDER=anthropic uv run python -m harness.minds.lab.fact_grounding --invent
"""

import argparse
import json
import random
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from harness import config
from harness.evals.metrics import cluster_bootstrap
from harness.facts import ground, posts
from harness.context.http import PoliteClient
from harness.facts.espn import GAME_DAYS, Espn
from harness.minds import gate
from harness.minds.lab.fetch_x_threads import OUT as FIXTURE

REPORT = config.ROOT / "action" / "experiments" / "fact_grounding.json"
CONTEXT = config.ROOT / "data" / "context"
DRAFTS = CONTEXT / "fact_grounding_drafts.jsonl"
LEADING_MENTIONS = re.compile(r"^(?:@\w+\s+)+")
INVENT_MODEL = "claude-sonnet-5-5"
INVENT_PER_THREAD = 6
DEVTOOLS_INVENT_THREADS = 40

INVENT_SYSTEM = """You write replies to posts on X, the way people scrolling X write them: short, casual, lowercase is fine."""
INVENT_PROMPT = """Post by @{author}:
{text}

Write {n} different replies to this post. Each reply must state one specific number that the post doesn't give: a stat, score, record, price, benchmark, date, version or count about a named player, team, person or product. Make the numbers up; don't try to be accurate. Return JSON only: {{"replies": ["..."]}}"""


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _read_jsonl(path):
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------- the two sets


class _Memo:
    """In-memory copies of ESPN responses for one run, so the run writes nothing to disk. Game summaries
    (about 0.5 MB each) aren't kept; _MemoEspn keeps their parsed facts instead."""

    def __init__(self, client: PoliteClient):
        self.client, self.seen = client, {}

    def get(self, url, max_age_s=None, headers=None, settled_after=None):
        if "/summary?" in url:
            return self.client.get(url)
        if url not in self.seen:
            self.seen[url] = self.client.get(url)
        return self.seen[url]


class _MemoEspn(Espn):
    def __init__(self):
        super().__init__(client=_Memo(PoliteClient(min_interval_s=1.0, cache_dir=None)))
        self.games: dict[tuple[str, str], list] = {}

    def game(self, event: dict, team: str) -> list:
        key = (event["id"], team)
        if key not in self.games:
            self.games[key] = super().game(event, team)
        return self.games[key]


def wnba_threads(espn: Espn, roster_days: int = 0) -> list[dict]:
    """The fixture's threads with the facts live at each post. With `roster_days`, threads posted in the
    last `roster_days` also get today's rosters, as if a refresh had run that day and no roster changed."""
    now = datetime.now(timezone.utc)
    out = []
    for t in json.loads(FIXTURE.read_text(encoding="utf-8"))["threads"]:
        at = _when(t["created_at"])
        facts = ground.live(espn.facts_at(at, history_days=GAME_DAYS), at)
        if roster_days and now - at < timedelta(days=roster_days):
            facts += espn.rosters(at)
        elif roster_days:
            continue
        out.append({"author": t["author"], "text": t["text"], "at": at, "facts": facts,
                    "replies": [r["text"] for r in t["direct_replies"] if r["text"].strip()]})
    return out


def devtools_threads() -> list[dict]:
    roots = {r["id"]: r for r in _read_jsonl(CONTEXT / "x_metrics.jsonl") if "id" in r and "error" not in r}
    timeline = [r for r in _read_jsonl(CONTEXT / "author_timelines.jsonl") if "id" in r and not r.get("is_repost")]
    by_root: dict[str, dict[str, str]] = {}
    for r in _read_jsonl(CONTEXT / "reply_samples.jsonl"):
        if "id" not in r or str(r.get("replying_to_status")) != str(r["reply_to_post"]):
            continue
        root = roots[str(r["reply_to_post"])]
        if r["author_handle"].lower() == root["author_handle"].lower():
            continue
        text = LEADING_MENTIONS.sub("", r.get("text") or "").strip()
        if text:
            by_root.setdefault(root["id"], {})[r["id"]] = text
    out = []
    for rid, replies in sorted(by_root.items()):
        root = roots[rid]
        author, at = root["author_handle"], _when(root["created_at"])
        own = [{"id": p["id"], "text": p["text"], "created_at": _when(p["created_at"]), "url": ""}
               for p in timeline if p["author_handle"].lower() == author.lower()
               and at - timedelta(days=posts.POST_DAYS) <= _when(p["created_at"]) < at]
        out.append({"author": author, "text": root["text"], "at": at, "replies": list(replies.values()),
                    "facts": ground.live(posts.post_facts(author, own), at)})
    return out


def index_for(t: dict) -> ground.FactIndex:
    return ground.FactIndex(t["facts"], ground.anchor_words(ground.split_handle(t["author"]) + " " + t["text"]))


# ---------------------------------------------------------------- measuring


def _rule_hits(results) -> Counter:
    return Counter(f.rule for r in results for f in r.findings)


def _grounding_only(results) -> int:
    return sum(1 for r in results if r.findings and all(f.rule in gate.GROUNDING_RULES for f in r.findings))


def _paired_change(before: list[bool], after: list[bool], threads: list[int]) -> dict:
    """after-rate minus before-rate over the same items, with a 95% interval that resamples whole threads."""
    def diff(idx, _ids):
        return (sum(after[i] for i in idx) - sum(before[i] for i in idx)) / len(idx) if idx else None

    ci = cluster_bootstrap(diff, threads, n_boot=2000, seed=0)
    return {"change": round(diff(list(range(len(before))), None), 3),
            "ci95": None if ci is None else [round(c, 3) for c in ci], "clusters": len(set(threads))}


def measure_real(threads: list[dict], examples: bool) -> dict:
    """Rejection rates on real replies with and without facts, as gate_calibration counts them."""
    before, after, flipped, cluster = [], [], [], []
    for k, t in enumerate(threads):
        idx = index_for(t)
        for i, text in enumerate(t["replies"]):
            context = f"@{t['author']}: {t['text']}\n" + "\n".join(t["replies"][:i] + t["replies"][i + 1:])
            b, a = gate.check(text, "reply", context=context), gate.check(text, "reply", context=context, facts=idx)
            before.append(b)
            after.append(a)
            cluster.append(k)
            if not b.passed and a.passed:
                flipped.append(text)
    n = len(before)
    only = lambda r: bool(r.findings) and all(f.rule in gate.GROUNDING_RULES for f in r.findings)  # noqa: E731

    def summary(results):
        return {"rejected": sum(not r.passed for r in results), "rejected_rate": round(sum(not r.passed for r in results) / n, 3),
                "rejected_by_grounding_only": _grounding_only(results),
                "rejected_by_grounding_only_rate": round(_grounding_only(results) / n, 3),
                "grounding_rule_hits": {k: v for k, v in _rule_hits(results).items() if k in gate.GROUNDING_RULES}}

    out = {"threads": len(threads), "replies": n, "facts_live_per_thread_median": sorted(len(t["facts"]) for t in threads)[len(threads) // 2],
           "before": summary(before), "after": summary(after), "passed_only_with_facts": len(flipped),
           "change_in_rejected_rate": _paired_change([not r.passed for r in before], [not r.passed for r in after], cluster),
           "change_in_grounding_only_rate": _paired_change([only(r) for r in before], [only(r) for r in after], cluster)}
    if examples:
        out["passed_only_with_facts_examples"] = flipped[:12]
    return out


def invent(threads: list[dict], profile: str, llm, batch: str) -> list[dict]:
    """Invented-stat drafts per thread, cached by batch, profile and post so a rerun pays only for new ones.
    Batch "design" shaped the number rule in ground.py; batch "heldout" was drawn after it was fixed."""
    cache = {(d.get("batch", "design"), d["profile"], d["post"]): d for d in (_read_jsonl(DRAFTS) if DRAFTS.exists() else [])}
    out = []
    with DRAFTS.open("a", encoding="utf-8") as f:
        for t in threads:
            key = (batch, profile, f"@{t['author']}: {t['text']}")
            if key not in cache:
                resp = llm.messages.create(model=INVENT_MODEL, max_tokens=4000, system=INVENT_SYSTEM, messages=[
                    {"role": "user", "content": INVENT_PROMPT.format(author=t["author"], text=t["text"], n=INVENT_PER_THREAD)}])
                from harness.llm import parse_json

                replies = parse_json("".join(b.text for b in resp.content if b.type == "text")).get("replies", [])
                cache[key] = {"batch": batch, "profile": profile, "post": key[2], "requested_model": INVENT_MODEL,
                              "reported_model": resp.model, "replies": [r for r in replies if isinstance(r, str)]}
                f.write(json.dumps(cache[key], ensure_ascii=False) + "\n")
                f.flush()
            out.append({**t, "drafts": cache[key]["replies"], "reported_model": cache[key]["reported_model"]})
    return out


def measure_invented(threads: list[dict], examples: bool) -> dict:
    def caught(r) -> bool:
        return any(f.rule in gate.GROUNDING_RULES for f in r.findings)

    n = cb = ca = cnum_b = cnum_a = 0
    let_through, flags_b, flags_a, cluster = [], [], [], []
    for k, t in enumerate(threads):
        idx = index_for(t)
        context = f"@{t['author']}: {t['text']}\n" + "\n".join(t["replies"])
        for d in t["drafts"]:
            if not gate.NUMBER.search(d):
                continue  # the model ignored the instruction; nothing to catch
            b, a = gate.check(d, "reply", context=context), gate.check(d, "reply", context=context, facts=idx)
            n += 1
            cb += caught(b)
            ca += caught(a)
            flags_b.append(caught(b))
            flags_a.append(caught(a))
            cluster.append(k)
            cnum_b += any(f.rule == "ungrounded_number" for f in b.findings)
            cnum_a += any(f.rule == "ungrounded_number" for f in a.findings)
            if caught(b) and not caught(a):
                let_through.append(d)
    out = {"drafts_with_a_number": n, "caught_before": cb, "caught_after": ca,
           "caught_before_rate": round(cb / n, 3) if n else None, "caught_after_rate": round(ca / n, 3) if n else None,
           "number_flagged_before": cnum_b, "number_flagged_after": cnum_a, "let_through_by_facts": len(let_through),
           "change_in_caught_rate": _paired_change(flags_b, flags_a, cluster) if n else None,
           "models": sorted({t["reported_model"] for t in threads})}
    if examples:
        out["let_through_examples"] = let_through
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--invent", action="store_true", help="also write and check invented-stat drafts (LLM calls)")
    args = p.parse_args()
    report = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {}
    espn = _MemoEspn()
    wnba, dev = wnba_threads(espn), devtools_threads()
    report["wnba_real_replies"] = measure_real(wnba, examples=True)
    recent = [t for t in wnba if datetime.now(timezone.utc) - t["at"] < timedelta(days=7)]
    report["wnba_real_replies_last_7_days"] = measure_real(recent, examples=False)
    report["wnba_real_replies_last_7_days_with_rosters"] = measure_real(wnba_threads(espn, roster_days=7), examples=False)
    report["devtools_real_replies"] = measure_real(dev, examples=False)
    report["wnba_espn_games_skipped"] = sorted(espn.skipped)
    if args.invent:
        import anthropic

        client = anthropic.Anthropic()
        sample = random.Random(0).sample(dev, min(DEVTOOLS_INVENT_THREADS, len(dev)))
        for batch in ("design", "heldout"):
            suffix = "" if batch == "design" else "_heldout"
            report[f"wnba_invented_stats{suffix}"] = measure_invented(invent(wnba, "wnba", client, batch), examples=True)
            report[f"devtools_invented_stats{suffix}"] = measure_invented(invent(sample, "devtools", client, batch), examples=False)
    REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if not kk.endswith("examples")} if isinstance(v, dict) else v
                      for k, v in report.items()}, indent=1))


if __name__ == "__main__":
    main()
