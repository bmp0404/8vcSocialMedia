"""Ranking memories for a round, and the one function the round loop calls at round start.

Krish's store owns storage and search. This module supplies what to rank by, from fields only this
package writes: a lesson's held-out effect and retest date, a reflection's verdict, a fact's status, an
episode's outcome, author and boost flag. `rank` is written so Krish's `context_for` can call it on
its own candidates; `round_memory` is the entry point for Dhilan's `engine/`.

`rank` follows Generative Agents (Park et al. 2023): relevance, importance and recency, each scaled to
[0, 1] and summed with equal weights. Here "importance" is trust: a lesson's effect, or a fixed value per
kind. Relevance is the store's search score. Lessons are few and abstract ("lead with a concrete number")
while narratives are concrete ("Fever offseason"), so keyword overlap would drop most lessons; every
active lesson is a candidate and relevance only reorders them. ExpeL (Zhao et al. 2024) likewise puts its
whole insight list in the prompt and retrieves only examples.

Every weight, cap and factor lives in `settings.RecallSettings`. Most are unvalidated starting points;
`exemplar_check` measures one of them.
"""

import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from harness.learning.kinds import FAILURE, OUTCOME_PRIORITY, SUCCESS, Episode, heldout_sentence, save
from harness.learning.settings import RecallSettings
from harness.learning.store import FakeStore, Memory

HOME = "brand"  # domain tag for memories learned from this account's own runs


@dataclass
class Pick:
    id: str
    kind: str
    text: str
    score: float
    why: dict = field(default_factory=dict)


def _date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def memory_date(m: Memory) -> date | None:
    for key in ("updated", "created", "created_at", "as_of"):
        d = _date(m.meta.get(key))
        if d:
            return d
    return None


def is_stale(m: Memory, today: date) -> bool:
    due = _date(m.meta.get("valid_until"))
    return m.meta.get("status") == "stale" or (due is not None and due <= today)


def revalidation_queue(store, today: date) -> list[dict]:
    """Lessons and facts past `valid_until`. Observe should recheck these; action doesn't see them."""
    return [
        {"id": m.id, "kind": m.kind, "text": m.body.splitlines()[0] if m.body else "",
         "valid_until": m.meta.get("valid_until", "")}
        for kind in ("lesson", "fact") for m in store.list_memories(kind)
        if m.meta.get("status") in ("active", "current", "stale") and is_stale(m, today)
    ]


def domain_of(m: Memory) -> str:
    return m.meta.get("domain") or (m.meta.get("features") or {}).get("domain", "")


def episode_label(m: Memory) -> str:
    """The same priority rule as Episode.label, read from stored frontmatter."""
    outcomes = [o for o in m.meta.get("outcomes", []) if o.get("label") in (SUCCESS, FAILURE)]
    for source in OUTCOME_PRIORITY:
        for o in outcomes:
            if o.get("source") == source:
                return o["label"]
    return ""


def trust(m: Memory, today: date, s: RecallSettings) -> float | None:
    """How much to rely on a memory, in [0, 1]; None leaves it out of the round."""
    meta = m.meta
    if m.kind == "lesson":
        if meta.get("status") != "active" or is_stale(m, today):
            return None
        base = max(0.0, min(1.0, float(meta.get("confidence", 0.0))))
    elif m.kind == "reflection":
        if meta.get("status") != "supported":
            return None
        base = s.kind_trust["reflection"]
    elif m.kind == "fact":
        if meta.get("status", "current") != "current" or is_stale(m, today):
            return None
        base = s.kind_trust["fact"]
    elif m.kind == "episode":
        if not episode_label(m):
            return None
        base = 0.5 if (meta.get("features") or {}).get("boost_suspect") is True else 1.0
        if meta.get("author_username") not in s.home_authors:
            base *= s.other_author_factor
    elif m.kind in s.kind_trust:
        base = s.kind_trust[m.kind]
    else:
        return None
    domain = domain_of(m)
    if domain and domain not in s.home_domains and m.kind in ("lesson", "reflection"):
        if meta.get("scope", "domain") != "general":
            return None
        base *= s.other_domain_factor
    return base


def recency(m: Memory, today: date, s: RecallSettings) -> float:
    d = memory_date(m)
    if d is None:
        return s.undated_recency
    return 0.5 ** (max((today - d).days, 0) / s.half_life_days)


def rank(candidates: list[Memory], today: date, s: RecallSettings) -> list[Pick]:
    """Score = weighted relevance + trust + recency, each in [0, 1]; then per-kind caps.

    Relevance is the search score min-max scaled across candidates; a candidate with no search score
    (an active lesson the query didn't match) gets 0. Episodes are capped at one success and one failure,
    so a mind sees a contrast rather than two of the same.
    """
    scores = [m.score for m in candidates if m.score > 0]
    lo, hi = (min(scores), max(scores)) if scores else (0.0, 0.0)
    picks, seen = [], set()
    for m in candidates:
        if m.id in seen:
            continue
        seen.add(m.id)
        t = trust(m, today, s)
        if t is None:
            continue
        rel = 0.0 if m.score <= 0 else (1.0 if hi == lo else (m.score - lo) / (hi - lo))
        rec = recency(m, today, s)
        score = s.weight_relevance * rel + s.weight_trust * t + s.weight_recency * rec
        picks.append(Pick(id=m.id, kind=m.kind, text=m.body, score=round(score, 4),
                          why={"relevance": round(rel, 3), "trust": round(t, 3), "recency": round(rec, 3),
                               "label": episode_label(m) if m.kind == "episode" else ""}))
    picks.sort(key=lambda p: (-p.score, p.id))
    taken: Counter = Counter()
    out = []
    for p in picks:
        if taken[p.kind] >= s.kind_caps.get(p.kind, 0):
            continue
        if p.kind == "episode":
            if taken[("episode", p.why["label"])] >= 1:
                continue
            taken[("episode", p.why["label"])] += 1
        taken[p.kind] += 1
        out.append(p)
    return out


SECTION_TITLES = {
    "lesson": "LESSONS (each tested on held-out posts)",
    "procedure": "HOW REVIEWERS EDIT DRAFTS",
    "fact": "FACTS (verbatim from the source)",
    "episode": "SIMILAR PAST POSTS",
    "reflection": "OBSERVATIONS",
    "entity": "WHO'S INVOLVED",
}


def _line(p: Pick, m: Memory | None) -> str:
    if p.kind == "lesson" and m is not None:
        outcome = FAILURE if m.meta.get("polarity") == "avoid" else SUCCESS
        first = p.text.splitlines()[0].split(": ", 1)[-1] if p.text else ""
        return f"- {first} ({heldout_sentence(m.meta.get('last_test') or {}, outcome)}) [{p.id}]"
    if p.kind == "episode":
        first = p.text.splitlines()[0].split(": ", 1)[-1] if p.text else ""
        return f"- {p.why['label']}: {' '.join(first.split())[:240]} [{p.id}]"
    return "\n".join(p.text.splitlines()[: 3 if p.kind == "procedure" else 1]) + f" [{p.id}]"


def render_picks(picks: list[Pick], store, budget_chars: int) -> str:
    """The memory block for one narrative, in the order a mind should read it, cut at `budget_chars`."""
    if not picks:
        return "(no relevant memories yet)"
    by_id = {m.id: m for m in store.list_memories()}
    by_kind = defaultdict(list)
    for p in picks:
        by_kind[p.kind].append(p)
    lines: list[str] = []
    for kind, title in SECTION_TITLES.items():
        if kind in by_kind:
            lines.append(title + ":")
            lines += [_line(p, by_id.get(p.id)) for p in by_kind[kind]]
    text = "\n".join(lines)
    return text if len(text) <= budget_chars else text[: budget_chars - 1] + "…"


def narrative_query(n) -> str:
    return " ".join(str(getattr(n, attr, "") or "") for attr in ("title", "summary", "angle"))


def round_memory(store, narratives, s: RecallSettings, today: date | None = None, seed: int = 0) -> dict:
    """What memory hands the round at its start.

    {"mode", "action": {narrative id: {"picks": [Pick as dict], "text": str}}, "observe": {"recheck": [...]}}.
    Mode "off" returns no memories; "shuffled" returns the same number of picks per kind, drawn at random
    from memories that pass the trust filters, so an ablation compares memory to noise at the same budget.
    """
    today = today or datetime.now(timezone.utc).date()
    lessons = store.list_memories("lesson")
    rng = random.Random(seed)
    pool = [m for m in store.list_memories() if trust(m, today, s) is not None] if s.mode == "shuffled" else []
    action = {}
    for n in narratives:
        if s.mode == "off":
            picks: list[Pick] = []
        else:
            hits = store.search(narrative_query(n), k=s.search_k)
            hit_ids = {h.id for h in hits}
            picks = rank(hits + [m for m in lessons if m.id not in hit_ids], today, s)
            if s.mode == "shuffled":
                want = Counter(p.kind for p in picks)
                picks = []
                for kind, count in sorted(want.items()):
                    options = [m for m in pool if m.kind == kind]
                    for m in rng.sample(options, min(count, len(options))):
                        picks.append(Pick(id=m.id, kind=m.kind, text=m.body, score=0.0,
                                          why={"shuffled": True, "label": episode_label(m) if m.kind == "episode" else ""}))
        action[n.id] = {"picks": [p.__dict__ for p in picks], "text": render_picks(picks, store, s.budget_chars)}
    return {"mode": s.mode, "action": action, "observe": {"recheck": revalidation_queue(store, today)}}


def render_lessons(store, today: date, s: RecallSettings, limit: int = 15) -> str:
    """memory/lessons.md: active lessons the minds read today, home-domain lessons first."""
    rows = []
    for m in store.list_memories("lesson"):
        t = trust(m, today, s)
        if t is not None:
            rows.append((domain_of(m) not in ("", *s.home_domains), -t, m.id, m))
    rows.sort(key=lambda r: r[:3])
    lines = ["# Lessons", "",
             "Generated by `python -m harness learn` from lessons that passed a held-out test. Don't edit by hand.", ""]
    if not rows:
        lines.append("(No tested lessons yet.)")
    for _, _, _, m in rows[:limit]:
        meta = m.meta
        outcome = FAILURE if meta.get("polarity") == "avoid" else SUCCESS
        lines.append(f"- {meta.get('text', m.body)} ({heldout_sentence(meta.get('last_test') or {}, outcome)}; "
                     f"learned from {meta.get('domain') or HOME}; retest by {meta.get('valid_until', '?')}; {m.id})")
    return "\n".join(lines) + "\n"


def exemplar_check(episodes: list[Episode], k: int = 10, n_test: int = 300, seed: int = 0) -> dict:
    """Do keyword-similar past posts predict a held-out post's outcome?

    For each test post, predict the majority label among its top-k search hits under three neighbor
    pools: any author, other authors only, and the same author only. Baselines: the overall majority
    label, and the author's own majority label in the training posts. Only posts whose author has both
    outcomes in training are scored, since the others are decided by account size alone.
    """
    rng = random.Random(seed)
    labeled = [e for e in episodes if e.label()]
    rng.shuffle(labeled)
    test, train = labeled[:n_test], labeled[n_test:]
    store = FakeStore()
    for ep in train:
        save(store, ep)
    by_id = {ep.id: ep for ep in train}
    author_counts: dict[str, Counter] = defaultdict(Counter)
    for ep in train:
        author_counts[ep.author_username][ep.label()] += 1
    overall = Counter(ep.label() for ep in train).most_common(1)[0][0]

    def majority(eps: list[Episode]) -> str | None:
        c = Counter(e.label() for e in eps)
        if not c:
            return None
        top = c.most_common()
        return None if len(top) > 1 and top[0][1] == top[1][1] else top[0][0]

    names = ("overall_majority", "author_majority", "knn_any", "knn_other_authors", "knn_same_author")
    tallies = {name: [0, 0] for name in names}
    # Author-majority accuracy on exactly the posts each method predicted, since kNN abstains on ties.
    baseline_on_same = {name: 0 for name in names}
    scored = 0
    for ep in test:
        counts = author_counts.get(ep.author_username, Counter())
        if not (counts[SUCCESS] and counts[FAILURE]):
            continue
        scored += 1
        hits = [by_id[h.id] for h in store.search(ep.text, k=200) if h.id in by_id]
        preds = {
            "overall_majority": overall,
            "author_majority": counts.most_common(1)[0][0] if counts[SUCCESS] != counts[FAILURE] else None,
            "knn_any": majority(hits[:k]),
            "knn_other_authors": majority([h for h in hits if h.author_username != ep.author_username][:k]),
            "knn_same_author": majority([h for h in hits if h.author_username == ep.author_username][:k]),
        }
        for name, pred in preds.items():
            if pred is not None:
                tallies[name][1] += 1
                tallies[name][0] += pred == ep.label()
                baseline_on_same[name] += preds["author_majority"] == ep.label()
    return {
        "scored_posts": scored,
        "k": k,
        "accuracy": {name: {"correct": c, "predicted": n, "accuracy": round(c / n, 3) if n else None,
                            "author_majority_on_same_posts": round(baseline_on_same[name] / n, 3) if n else None}
                     for name, (c, n) in tallies.items()},
    }


def today_utc() -> date:
    return datetime.now(timezone.utc).date()
