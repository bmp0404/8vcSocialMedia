"""The morning report, out/improve/report-<date>.md: what the loop did since the last one, for Sam.

`render` is pure: the scheduler gathers a `ReportData` and this turns it into plain sentences that
fit on a page. No individual's post or reply text appears in it, only handles of watched brands.
"""

from dataclasses import dataclass, field
from datetime import datetime

from harness.improve.watchday import Day

NAMES = {"reply_ranker": "Reply ranker", "tfidf_post_ranker": "TF-IDF post ranker", "label_model": "Claude label model",
         "lineup_detector": "Lineup AI detector", "action_veto": "Action veto", "slop_gate": "Slop gate",
         "panel": "Panel"}


@dataclass
class ReportData:
    now: datetime
    since: datetime | None  # the previous report, or None for the first
    cycles: int
    llm_calls: int
    facts: dict[str, str] = field(default_factory=dict)  # profile -> one-line result
    fold: dict | None = None  # the rolling benchmark's manifest
    instruments: dict = field(default_factory=dict)  # instruments.json
    events: list[dict] = field(default_factory=list)  # instrument events since `since`
    decided: list[dict] = field(default_factory=list)  # experiment records decided since `since`
    champion: str = ""
    champion_changed: list[str] = field(default_factory=list)  # promotions and reverts since `since`
    queue: list[dict] = field(default_factory=list)  # {spec, fitness, profile, state, note}
    queue_errors: dict[str, str] = field(default_factory=dict)
    day: Day | None = None
    hot_offers: dict[str, dict] = field(default_factory=dict)  # hot status ID -> arm and offer
    failures: list[str] = field(default_factory=list)  # steps that failed, with why
    disk_free_gb: float | None = None


def _t(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M UTC")


def _ci(s: dict) -> str:
    if not s or s.get("estimate") is None:
        return ""
    ci = s.get("ci95")
    return f"{s['estimate']:.3f}" + (f" [{ci[0]:.3f}, {ci[1]:.3f}]" if ci else "")


def _source(s: dict, unit: str) -> str:
    if not s or not s.get("n"):
        return "no outcomes yet"
    cov = s.get("coverage")
    cover = f", coverage {cov:.0%}" if cov is not None and cov < 1 else ""
    return f"{_ci(s)} on {s['n']} {unit}{cover}"


def _instrument_line(name: str, e: dict) -> str:
    sc = e.get("scores", {})
    unit = "accounts" if name in ("tfidf_post_ranker", "label_model", "panel") else "threads"
    status = "gates" if e["status"] == "gates" else "report-only"
    threads = sc.get("threads") or {}
    origins = (threads.get("detail") or {}).get("origins")
    where = f" ({', '.join(f'{n} from the {o}' for o, n in origins.items())})" if origins else ""
    parts = [f"rooted since {e['cutoff'][:10]}: {_source(threads, unit)}{where}"]
    sgu = sc.get("sgu") or {}
    if sgu.get("n"):
        parts.insert(0, f"_sgu_ replies (Spearman): {_source(sgu, 'replies')}")
    extra = (sc.get("threads") or {}).get("detail", {}).get("pass_rate_on_real_replies")
    if extra is not None:
        parts.append(f"passes {extra:.0%} of real replies")
    return f"- **{NAMES.get(name, name)}**: {status}. " + "; ".join(parts) + "."


def _metric(rec: dict) -> str:
    m = next((x for x in rec["eval"].get("metrics", []) if x.get("primary")), None)
    if not m:
        return ""
    d = m["delta"]
    ci = d.get("ci95")
    line = (f"{m['name']} {m['champion']['value']:.3f} to {m['challenger']['value']:.3f}, difference "
            f"{d['value']:+.3f}" + (f" [{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci else ""))
    abstain = next((x for x in rec["eval"]["metrics"] if x["name"] == "abstain_rate"), None)
    if abstain:
        line += f"; coverage {1 - abstain['challenger']['value']:.0%} against {1 - abstain['champion']['value']:.0%}"
    return f"{line}, on {rec['eval']['n']} {rec['eval']['unit']}s"


def _clip(text: str, limit: int = 240) -> str:
    """At most `limit` characters, cut at the last clause boundary so no word is broken."""
    text = " ".join(text.split()).rstrip(".")
    if len(text) <= limit:
        return text
    cut = max(text.rfind("; ", 0, limit), text.rfind(". ", 0, limit))
    return text[:cut] if cut > 0 else text[:limit].rsplit(" ", 1)[0]


def _decision(rec: dict) -> str:
    who = rec["proposer"].get("spec") or rec["id"]
    phase = rec["proposer"].get("phase") or rec["proposer"].get("kind")
    numbers = _metric(rec)
    return (f"- **{who}** ({rec['eval'].get('fitness') or 'no instrument'}, {phase}): {rec['decision']['status']}."
            + (f" {numbers}." if numbers else "") + f" {_clip(rec['decision']['reason'])}.")


def render(d: ReportData) -> str:
    span = f"From {_t(d.since)} to {_t(d.now)}" if d.since else f"The first report, up to {_t(d.now)}"
    lines = [f"# Morning report, {d.now.date().isoformat()}", "",
             f"{span}: {d.cycles} cycle{'s' if d.cycles != 1 else ''} and "
             f"{d.llm_calls} LLM call{'s' if d.llm_calls != 1 else ''}.", "", "## What ran and what changed", ""]
    for profile, result in sorted(d.facts.items()):
        lines.append(f"- Facts for {profile}: {result}.")
    if d.fold is not None:
        weeks = d.fold.get("weeks") or {}
        if d.fold.get("pairs"):
            lines.append(f"- The rolling reply benchmark holds {d.fold['pairs']} pairs from {d.fold['threads']} watcher "
                         f"threads read at +48h, in week(s) {', '.join(weeks)}.")
        else:
            nxt = f"; the next +48h read opens {d.day.next_mature_read[:16].replace('T', ' ')} UTC" \
                if d.day and d.day.next_mature_read else ""
            lines.append(f"- No watcher thread has a +48h read yet, so the rolling benchmark is empty{nxt}.")
        if d.fold.get("refused"):
            lines.append(f"- Threads kept out of the benchmark: " + ", ".join(f"{n} {why}" for why, n in d.fold["refused"].items()) + ".")
    if d.champion_changed:
        lines.append(f"- The champion changed ({', '.join(d.champion_changed)}); it is now `{d.champion}`.")
    else:
        lines.append(f"- The champion did not change (`{d.champion}`).")

    lines += ["", "## Instruments", ""]
    for name in NAMES:
        if name in d.instruments.get("instruments", {}):
            lines.append(_instrument_line(name, d.instruments["instruments"][name]))
    moved = [e for e in d.events if e.get("event") == "status" and e.get("from")]
    for e in moved:
        verb = "dropped to report-only" if e["to"] == "report_only" else "gates again"
        lines.append(f"- {NAMES.get(e['instrument'], e['instrument'])} {verb}: {e['why']}.")
    if not moved:
        lines.append("- No instrument changed status.")

    lines += ["", "## Experiments", ""]
    lines += [_decision(r) for r in d.decided] or ["- No experiment was decided."]
    for q in d.queue:
        if q["state"] not in ("ran", "done"):
            lines.append(f"- {q['spec']} ({q['fitness']}, {q['profile']}): {_clip(q['note'])}.")

    lines += ["", "## The watcher's day", ""]
    if d.day is None:
        lines.append("- No watcher state found.")
    else:
        w = d.day
        fail = f", {w.poll_errors / w.polls:.0%} failed" if w.polls else ""
        lines.append(f"- {'Running' if w.running else 'Not running'}. {w.polls:,} polls over {w.handles_polled} "
                     f"handles{fail}.")
        lines.append(f"- {w.posts_seen} new watched posts, {w.fresh_posts} of them under 10 minutes old when read.")
        if w.hot:
            parts = []
            for h in w.hot:
                o = d.hot_offers.get(h["status_id"], {})
                what = o.get("offer") or h.get("state")
                if what == "abstain":
                    what = f"abstained ({o.get('abstain_reason')})"
                elif what == "human_prompt":
                    what = "asked for a one-liner"
                elif what == "draft":
                    what = "offered a draft"
                parts.append(f"@{h['handle']} ({h['profile']}, arm {o.get('arm', '?')}): {what}")
            lines.append(f"- {len(w.hot)} hot target{'s' if len(w.hot) != 1 else ''}: " + "; ".join(parts) + ".")
        else:
            lines.append("- No hot targets.")
        kept = ", ".join(f"{n} at {cp}" for cp, n in sorted(w.kept.items(), key=lambda kv: int(kv[0][1:-1])))
        other = sum(n for s, n in w.read.items() if s != "kept")
        lines.append(f"- Threads kept: {kept or 'none'}" + (f"; {other} other reads were low coverage, empty or missed"
                                                           if other else "") + ".")

    needs = [_clip(f, 300) for f in d.failures]
    waiting = [h for h in (d.day.hot if d.day else []) if d.hot_offers.get(h["status_id"], {}).get("offer")
               in ("draft", "human_prompt")]
    if waiting:
        needs.append(f"{len(waiting)} hot target{'s' if len(waiting) != 1 else ''} offered a draft or asked for a "
                     "one-liner. Reply from _sgu_ whatever the arm, or note why not, so the arm comparison stays fair")
    needs += [f"Queue file {name} was refused: {why}" for name, why in sorted(d.queue_errors.items())]
    needs += [f"{NAMES.get(e['instrument'], e['instrument'])} is report-only now; decide whether to fix or retire it"
              for e in moved if e["to"] == "report_only"]
    if d.day is not None and not d.day.running:
        needs.append("The watcher isn't running, so no fresh threads are arriving")
    if d.disk_free_gb is not None and d.disk_free_gb < 8:
        needs.append(f"Free disk is {d.disk_free_gb:.1f} GB, under the 8 GB floor")
    needs += [f"Queue spec {q['spec']} is stuck: {q['note']}" for q in d.queue if q["state"] == "stale"]
    needs += [f"Queue spec {q['spec']} can't run until the gateway answers: {q['note']}" for q in d.queue
              if q["state"] == "deferred" and q["note"].startswith("waiting for")]
    lines += ["", "## Needs a person", ""] + ([f"- {n}." for n in needs] or ["- Nothing."])
    return "\n".join(lines) + "\n"
