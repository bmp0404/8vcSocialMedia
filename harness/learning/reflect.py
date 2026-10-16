"""Reflection: ask why posts that share an outcome share it, test the answer, and keep what holds up.

The job follows three published designs. HypoGeniC (Zhou et al. 2024) and the tweet-popularity pairs it
uses (Tan et al. 2014) compare posts matched on author, so audience size can't explain the difference.
D5 (Zhong et al. 2023) proposes hypotheses on one split and evaluates them on another. ExpeL (Zhao et al.
2024) compares failed with successful attempts and votes insights up or down instead of rewriting them.

1. Filter: drop thread continuations, posts too young for a final label, and boosted successes.
2. Stratify by account and by launch vs non-launch, and split each stratum's posts in half: one half
   for proposing hypotheses, the other held out for testing them.
3. Show the model groups of failures and successes from the same strata and ask what the failures
   share that the successes lack, and the reverse. It may answer that nothing is shared. Each
   hypothesis cites episode ids and comes with a yes/no predicate, plus a rule code can check when
   the trait is one of the computed features.
4. Drop citations that don't point at an episode with the claimed outcome in that batch. A hypothesis
   left with fewer than two evidence ids or no contrast id is discarded.
5. Answer every predicate for every held-out post (by rule, or by a labeling model that never sees
   outcomes), run the stratified test in `stats.py`, and apply Benjamini-Hochberg across the run.
   A supported hypothesis becomes a lesson, or votes on the lesson it restates.
"""

import json
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from harness.learning.kinds import FAILURE, SUCCESS, Episode, Lesson, Reflection, load, save, short_hash
from harness.learning.settings import ReflectSettings
from harness.learning.stats import Table, heldout_test, verdicts

GroupKey = Callable[[Episode], str]


class MissingBoostFlags(ValueError):
    """Raised when no episode carries `boost_suspect`, so boosted successes can't be excluded."""


def by_account_and_launch(ep: Episode) -> str:
    return f"{ep.author_username}|{'launch' if ep.features.get('is_launch') else 'other'}"


def usable(episodes: list[Episode], *, require_boost_flags: bool = True,
           min_label_age_hours: float = 48) -> tuple[list[Episode], dict]:
    """Labeled episodes fit to learn from, and counts of what was dropped and why.

    Raises MissingBoostFlags when `require_boost_flags` is set and no episode has a `boost_suspect` value:
    without it, paid and bot pushes count as successes.
    """
    has_flags = any("boost_suspect" in ep.features for ep in episodes)
    if require_boost_flags and not has_flags:
        raise MissingBoostFlags(
            "no episode has a boost_suspect flag, so boosted successes can't be excluded; attach "
            "data/context/devtools_enriched.csv from the context worker, or pass --allow-unfiltered"
        )
    dropped = {"unlabeled": 0, "thread_continuation": 0, "label_too_young": 0, "not_label_eligible": 0,
               "boosted_success": 0}
    kept = []
    for ep in episodes:
        label = ep.label()
        age = ep.features.get("label_age_hours")
        if not label:
            dropped["unlabeled"] += 1
        elif ep.features.get("thread_continuation") is True:
            dropped["thread_continuation"] += 1
        elif isinstance(age, (int, float)) and age < min_label_age_hours:
            dropped["label_too_young"] += 1
        elif ep.features.get("label_eligible") is False:  # context worker: immature or a thread continuation
            dropped["not_label_eligible"] += 1
        elif label == SUCCESS and ep.features.get("boost_suspect") is True:
            dropped["boosted_success"] += 1
        else:
            kept.append(ep)
    dropped["boost_filter"] = "on" if has_flags else "off"
    return kept, dropped


def _strata(episodes: list[Episode], key: GroupKey) -> dict[str, dict[str, list[Episode]]]:
    strata: dict[str, dict[str, list[Episode]]] = defaultdict(lambda: {FAILURE: [], SUCCESS: []})
    for ep in sorted(episodes, key=lambda e: e.id):
        strata[key(ep)][ep.label()].append(ep)
    return strata


def split_heldout(episodes: list[Episode], key: GroupKey, rng: random.Random,
                  min_per_side: int = 2) -> tuple[list[Episode], list[Episode]]:
    """(proposal, held-out). Strata with at least `min_per_side` of each outcome give half of each side
    to the held-out set; every other episode goes to the proposal set."""
    proposal, held = [], []
    for _, sides in sorted(_strata(episodes, key).items()):
        eligible = len(sides[FAILURE]) >= min_per_side and len(sides[SUCCESS]) >= min_per_side
        for label in (FAILURE, SUCCESS):
            eps = list(sides[label])
            rng.shuffle(eps)
            cut = len(eps) // 2 if eligible else 0
            held += eps[:cut]
            proposal += eps[cut:]
    return proposal, held


@dataclass
class Batch:
    group: str
    failures: dict[str, list[Episode]] = field(default_factory=dict)  # stratum -> episodes
    successes: dict[str, list[Episode]] = field(default_factory=dict)

    def ids(self, label: str) -> set[str]:
        side = self.failures if label == FAILURE else self.successes
        return {ep.id for eps in side.values() for ep in eps}


def contrast_batches(episodes: list[Episode], key: GroupKey, rng: random.Random, n_batches: int = 2,
                     groups_per_batch: int = 4, per_side: int = 3, min_per_side: int = 1) -> list[Batch]:
    """Batches of strata that each have at least `min_per_side` failures and successes, sampled per side."""
    strata = _strata(episodes, key)
    eligible = sorted(g for g, s in strata.items()
                      if len(s[FAILURE]) >= min_per_side and len(s[SUCCESS]) >= min_per_side)
    rng.shuffle(eligible)
    batches = []
    for i in range(n_batches):
        chosen = sorted(eligible[i * groups_per_batch:(i + 1) * groups_per_batch])
        if not chosen:
            break
        batch = Batch(group="contrast:" + ",".join(chosen))
        for g in chosen:
            for label, side in ((FAILURE, batch.failures), (SUCCESS, batch.successes)):
                pool = strata[g][label]
                side[g] = rng.sample(pool, min(per_side, len(pool)))
        batches.append(batch)
    return batches


def link_evidence(hypothesis: dict, batch: Batch) -> tuple[list[str], list[str], list[str]] | None:
    """Keep only citations that exist in the batch with the right outcome.

    Evidence must share the hypothesis's outcome; contrast must have the other one. Returns
    (evidence, contrast, dropped), or None when fewer than two evidence ids or no contrast id survive.
    """
    outcome = hypothesis.get("outcome")
    if outcome not in (FAILURE, SUCCESS):
        return None
    other = SUCCESS if outcome == FAILURE else FAILURE
    same_ids, other_ids = batch.ids(outcome), batch.ids(other)
    evidence, contrast, dropped = [], [], []
    for cited, allowed, keep in ((hypothesis.get("evidence") or [], same_ids, evidence),
                                 (hypothesis.get("contrast") or [], other_ids, contrast)):
        for eid in cited:
            eid = str(eid)
            if eid in allowed and eid not in keep:
                keep.append(eid)
            elif eid not in keep:
                dropped.append(eid)
    if len(evidence) < 2 or not contrast:
        return None
    return evidence, contrast, dropped


# Rules code can check, over features `dataset.row_features` computes. Anything else goes to the labeler.
RULE_OPS = {
    "media": {"eq", "in"},
    "has_link": {"eq"},
    "starts_with_mention": {"eq"},
    "is_launch": {"eq"},
    "n_chars": {"lt", "gt"},
    "n_hashtags": {"lt", "gt", "eq"},
    "text": {"contains_any", "contains_none"},
}
BOOL_FEATURES = {"has_link", "starts_with_mention", "is_launch"}


def rule_error(rule, extra: dict[str, str] | None = None) -> str | None:
    """Why a rule can't be checked, or None when it can. `extra` maps context features to "bool" or "number"."""
    if not isinstance(rule, dict):
        return "rule is not an object"
    feat, op, value = rule.get("feature"), rule.get("op"), rule.get("value")
    extra = extra or {}
    if feat in extra:
        if extra[feat] == "bool":
            return None if op == "eq" and isinstance(value, bool) else f"{feat} needs op eq with true or false"
        ok = op in ("lt", "gt") and isinstance(value, (int, float)) and not isinstance(value, bool)
        return None if ok else f"{feat} needs op lt or gt with a number"
    if feat not in RULE_OPS:
        return f"unknown feature {feat!r}"
    if op not in RULE_OPS[feat]:
        return f"op {op!r} not allowed for {feat}"
    if op in ("lt", "gt") and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return "lt/gt need a number"
    if op in ("in", "contains_any", "contains_none") and not (
            isinstance(value, list) and value and all(isinstance(v, str) and v.strip() for v in value)):
        return f"{op} needs a non-empty list of strings"
    if op == "eq" and feat in BOOL_FEATURES and not isinstance(value, bool):
        return f"{feat} eq needs true or false"
    if op == "eq" and feat == "media" and not isinstance(value, str):
        return "media eq needs a string"
    return None


def extra_features(episodes: list[Episode]) -> dict[str, str]:
    """Context features (`pre_*`) present on every episode, typed "bool" or "number", usable in rules."""
    if not episodes:
        return {}
    out = {}
    for name in sorted(k for k in episodes[0].features if k.startswith("pre_")):
        values = [ep.features.get(name) for ep in episodes]
        if all(isinstance(v, bool) for v in values):
            out[name] = "bool"
        elif all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            out[name] = "number"
    return out


def apply_rule(rule: dict, ep: Episode) -> bool:
    feat, op, value = rule["feature"], rule["op"], rule["value"]
    if feat == "text":
        text = ep.text.lower()
        hit = any(re.search(r"(?<![a-z0-9])" + re.escape(w.strip().lower()) + r"(?![a-z0-9])", text) for w in value)
        return hit if op == "contains_any" else not hit
    actual = ep.features.get(feat)
    if op == "eq":
        return actual == value
    if op == "in":
        return actual in value
    if op == "lt":
        return actual is not None and actual < value
    return actual is not None and actual > value


PROPOSE_SYSTEM = """You study why social media posts succeed or fail. Posts are grouped by who posted them, and
each group has posts that FAILED and posts that SUCCEEDED, so audience size is the same within a group.

Find what the failures have in common that the successes from the same groups lack, and the reverse.
Look for traits you can see in the post itself: the hook, the format, the topic, the call to action,
the media, the length, who it addresses. Don't explain an outcome with luck, timing or account size.
If the failures share nothing the successes lack, say so in `no_shared_trait` and return fewer or no
hypotheses; a forced hypothesis is worse than none.

Every hypothesis cites the post ids it explains (`evidence`, all with the stated outcome) and post ids
with the other outcome that lack the trait (`contrast`). Write `predicate` as a yes/no question someone
could answer from one post's text and media field alone, true for the posts the claim is about. When
the trait can be written as a rule over these fields, also give `rule`, which is checked by code:
- {"feature": "media", "op": "eq" or "in", "value": "none"|"photo"|"video"|"gif" or a list of them}
- {"feature": "has_link" | "starts_with_mention", "op": "eq", "value": true or false}
- {"feature": "n_chars" | "n_hashtags", "op": "lt" or "gt", "value": a number}
- {"feature": "text", "op": "contains_any" or "contains_none", "value": ["word or phrase", ...]}
Use null for `rule` otherwise. Write `lesson` as one imperative sentence a writer could follow. Set
`transfer` to "general" if you expect the claim to hold for other kinds of accounts, else "domain". If
a hypothesis repeats an existing lesson, put that lesson's id in `restates`."""

PROPOSE_PROMPT = """OUTCOMES: {definition}
{extra}

EXISTING LESSONS:
{lessons}

GROUPS:
{groups}

Propose up to {n} hypotheses, mixing failure and success claims.
Return JSON: {{"no_shared_trait": "", "hypotheses": [{{"outcome": "failure", "claim": "...", "predicate": "...",
"rule": null, "lesson": "...", "evidence": ["..."], "contrast": ["..."], "transfer": "domain", "restates": ""}}]}}"""

LABEL_SYSTEM = """You label social media posts. For each post, answer each question with T (true) or F (false),
using only the post's text and its media field. Answer every question for every post."""

LABEL_PROMPT = """QUESTIONS (answer in this order):
{questions}

POSTS:
{posts}

Return JSON mapping each post id to a string of {n} letters, one T or F per question in order.
Example for 3 questions: {{"labels": {{"post-123": "TFF"}}}}"""


def _post_line(ep: Episode, limit: int = 300) -> str:
    text = " ".join(ep.text.split())[:limit]
    return f"- [{ep.id}] (media: {ep.features.get('media', 'unknown')}) {text}"


def render_batch(batch: Batch) -> str:
    lines = []
    for g in sorted(batch.failures):
        account, _, kind = g.partition("|")
        lines.append(f"## {account}" + (f" ({kind} posts)" if kind else ""))
        lines.append("FAILED:")
        lines += [_post_line(ep) for ep in batch.failures[g]]
        lines.append("SUCCEEDED:")
        lines += [_post_line(ep) for ep in batch.successes.get(g, [])]
    return "\n".join(lines)


def _mock_hypotheses(batch: Batch) -> dict:
    fails, wins = sorted(batch.ids(FAILURE)), sorted(batch.ids(SUCCESS))
    return {
        "no_shared_trait": "",
        "hypotheses": [
            {
                "outcome": FAILURE,
                "claim": "Mock: the failed posts have no media attached.",
                "predicate": "Does the post have no media attached?",
                "rule": {"feature": "media", "op": "eq", "value": "none"},
                "lesson": "Attach an image or video.",
                "evidence": fails[:2],
                "contrast": wins[:1],
                "transfer": "general",
                "restates": "",
            }
        ],
    }


def propose(llm, batch: Batch, definition: str, lessons: list[Lesson], n: int = 5,
            extra: dict[str, str] | None = None) -> dict:
    """The model's hypotheses for one batch. Malformed JSON is retried once; a second failure yields none
    for this batch, with `error` set so the run reports it."""
    lesson_lines = "\n".join(f"- [{l.id}] {l.text}" for l in lessons) or "(none)"
    extra_text = ("\nMORE RULE FEATURES (computed per post; bool takes op eq, number takes lt or gt):\n"
                  + ", ".join(f"{k} ({t})" for k, t in sorted((extra or {}).items())) + "\n") if extra else ""
    prompt = PROPOSE_PROMPT.format(definition=definition, extra=extra_text, lessons=lesson_lines,
                                   groups=render_batch(batch), n=n)
    for attempt in (1, 2):
        try:
            data = llm.complete_json(PROPOSE_SYSTEM, prompt, mock=lambda: _mock_hypotheses(batch))
            return {"no_shared_trait": str(data.get("no_shared_trait") or ""),
                    "hypotheses": list(data.get("hypotheses") or []), "error": ""}
        except ValueError as e:  # llm.parse_json raises ValueError; JSONDecodeError is a subclass
            print(f"[reflect] proposal for {batch.group} attempt {attempt} failed: {str(e)[:160]}")
            error = str(e)[:300]
            prompt += "\n\nYour previous reply was not valid JSON. Escape any double quotes inside strings."
    return {"no_shared_trait": "", "hypotheses": [], "error": error}


def parse_labels(data, qids: list[str]) -> dict[str, dict[str, bool]]:
    """{post id: {qid: bool}} from {"labels": {post id: "TF..."}}; a string of the wrong length is dropped."""
    out = {}
    for pid, answers in ((data or {}).get("labels") or {}).items():
        if isinstance(answers, str):
            letters = answers.strip().upper().replace(" ", "")
            if len(letters) == len(qids) and set(letters) <= {"T", "F"}:
                out[str(pid)] = {q: c == "T" for q, c in zip(qids, letters)}
    return out


def label_predicates(labeler, predicates: dict[str, str], posts: list[Episode], rng: random.Random,
                     chunk: int = 25) -> tuple[dict[str, dict[str, bool]], int]:
    """({post id: {predicate id: bool}}, chunks that failed twice). Posts are shuffled and shown without outcomes."""
    qids = list(predicates)
    if not qids:
        return {}, 0
    order = list(posts)
    rng.shuffle(order)
    questions = "\n".join(f"{i + 1}. {predicates[q]}" for i, q in enumerate(qids))
    labels: dict[str, dict[str, bool]] = {}
    failed = 0
    for start in range(0, len(order), chunk):
        part = order[start:start + chunk]
        prompt = LABEL_PROMPT.format(questions=questions, posts="\n".join(_post_line(ep, 400) for ep in part),
                                     n=len(qids))

        def mock(part=part):
            return {"labels": {ep.id: "".join("T" if ep.features.get("media", "none") == "none" else "F"
                                              for _ in qids) for ep in part}}

        for attempt in (1, 2):
            try:
                labels.update(parse_labels(labeler.complete_json(LABEL_SYSTEM, prompt, mock=mock), qids))
                break
            except ValueError as e:  # llm.parse_json raises ValueError; JSONDecodeError is a subclass
                print(f"[reflect] labeling chunk {start // chunk + 1} attempt {attempt} failed: {str(e)[:160]}")
                failed += attempt == 2
    return labels, failed


def tables_for(heldout: list[Episode], key: GroupKey, answers: dict[str, bool], outcome: str) -> list[Table]:
    """One 2x2 table per stratum from held-out posts that have an answer."""
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for ep in heldout:
        if ep.id not in answers:
            continue
        c = counts[key(ep)]
        c[(0 if answers[ep.id] else 2) + (0 if ep.label() == outcome else 1)] += 1
    return [Table(s, *c) for s, c in sorted(counts.items())]


def answers_for(predicate_id: str, rule: dict | None, heldout: list[Episode],
                labels: dict[str, dict[str, bool]]) -> dict[str, bool]:
    if rule:
        return {ep.id: apply_rule(rule, ep) for ep in heldout}
    return {pid: a[predicate_id] for pid, a in labels.items() if predicate_id in a}


LAUNCH_WORDS = re.compile(r"\b(announc\w*|launch\w*|releas\w*|introduc\w*|ship\w*|new (?:product|feature|tool)\w*)",
                          re.IGNORECASE)


def caveats_for(hypothesis: dict, filters: dict) -> list[str]:
    """Reasons to doubt a claim that the held-out test can't rule out."""
    out = []
    if filters.get("boost_filter") == "off":
        out.append("boost flags were unavailable, so boosted successes were not excluded")
    text = " ".join(str(hypothesis.get(k) or "") for k in ("claim", "predicate", "lesson"))
    if hypothesis.get("outcome") == SUCCESS and LAUNCH_WORDS.search(text):
        out.append("likely launch-confounded: it describes announcements, and launches get paid and bot boosts "
                   "that the launch keyword rule doesn't fully catch")
    return out


def _until(today: str, days: int) -> str:
    return (datetime.fromisoformat(today) + timedelta(days=days)).date().isoformat()


def vote(lesson: Lesson, verdict: str, result: dict, test_id: str, today: str, domain: str,
         labeled_count: int, valid_days: int = 30) -> Lesson:
    """ExpeL-style vote on a retest.

    Supported renews the lesson and records its effect; contradicted counts against it and retires it
    once contradictions outnumber support; inconclusive changes nothing but the count. A lesson that
    passes on a second domain's data is promoted from "domain" to "general" scope.
    """
    if verdict == "supported":
        lesson.support += 1
        lesson.status = "active"
        if result.get("boost_filter") == "on":  # passed with boosted successes removed
            lesson.caveats = [c for c in lesson.caveats if not c.startswith("boost flags were unavailable")]
        lesson.confidence = result["effect"]
        lesson.last_test = result
        lesson.valid_until = _until(today, valid_days)
        if domain not in lesson.domains:
            lesson.domains.append(domain)
        if len(lesson.domains) >= 2:
            lesson.scope = "general"
    elif verdict == "contradicted":
        lesson.contradict += 1
        if lesson.contradict > lesson.support:
            lesson.status = "retired"
    else:
        lesson.inconclusive += 1
    if test_id not in lesson.reflections:
        lesson.reflections.append(test_id)
    lesson.labeled_at_test = labeled_count
    lesson.updated = today
    return lesson


def due_for_retest(lesson: Lesson, labeled_now: int, today: str, min_new: int = 50) -> bool:
    if lesson.status == "retired":
        return False
    return labeled_now - lesson.labeled_at_test >= min_new or bool(lesson.valid_until and lesson.valid_until <= today)


@dataclass
class Candidate:
    hypothesis: dict
    batch: Batch
    evidence: list[str]
    contrast: list[str]
    dropped: list[str]
    rule: dict | None
    rule_problem: str | None


@dataclass
class ReflectionReport:
    filters: dict = field(default_factory=dict)
    batches: int = 0
    proposed: int = 0
    linked: int = 0
    heldout_posts: int = 0
    heldout_strata: int = 0
    no_shared_trait: list[str] = field(default_factory=list)
    proposal_errors: list[str] = field(default_factory=list)
    label_chunks_failed: int = 0
    reflections: list[Reflection] = field(default_factory=list)
    lessons_new: list[str] = field(default_factory=list)
    lessons_voted: list[str] = field(default_factory=list)


def reflect(
    episodes: list[Episode],
    llm,
    store,
    *,
    definition: str,
    domain: str,
    settings: ReflectSettings | None = None,
    labeler=None,
    key: GroupKey = by_account_and_launch,
    seed: int = 0,
    now: datetime | None = None,
) -> ReflectionReport:
    s = settings or ReflectSettings()
    rng = random.Random(seed)
    today = (now or datetime.now(timezone.utc)).date().isoformat()
    pool, filters = usable(episodes, require_boost_flags=s.require_boost_flags,
                           min_label_age_hours=s.min_label_age_hours)
    existing = {m.id: load(m) for m in store.list_memories("lesson")}
    proposal, held = split_heldout(pool, key, rng, min_per_side=s.heldout_min_per_side)
    # A restated lesson is voted on with this run's test, so no existing lesson's own evidence may be in it.
    cited = {eid for lesson in existing.values() for eid in lesson.evidence}
    held = [ep for ep in held if ep.id not in cited]
    batches = contrast_batches(proposal, key, rng, n_batches=s.n_batches, groups_per_batch=s.groups_per_batch,
                               per_side=s.per_side)
    report = ReflectionReport(filters=filters, batches=len(batches), heldout_posts=len(held),
                              heldout_strata=len({key(ep) for ep in held}))
    if not batches or not held:
        print(f"[reflect] skipped: {len(held)} held-out posts and {len(batches)} batches; reflection needs "
              f"strata with at least 2 failures and 2 successes each (filters: {filters})")
        return report
    active = [l for l in existing.values() if l.status == "active"]
    extra = extra_features(pool)

    candidates: list[Candidate] = []
    for batch in batches:
        out = propose(llm, batch, definition, active, n=s.n_hypotheses, extra=extra)
        if out["error"]:
            report.proposal_errors.append(f"{batch.group}: {out['error']}")
        if out["no_shared_trait"]:
            report.no_shared_trait.append(out["no_shared_trait"])
        report.proposed += len(out["hypotheses"])
        for h in out["hypotheses"]:
            linked = link_evidence(h, batch)
            if not linked or not str(h.get("predicate", "")).strip():
                continue
            rule = h.get("rule") or None
            problem = rule_error(rule, extra) if rule else None
            candidates.append(Candidate(h, batch, *linked, rule=None if problem else rule, rule_problem=problem))
    report.linked = len(candidates)
    if not candidates:
        print(f"[reflect] {report.proposed} hypotheses proposed, none survived evidence linking")
        return report

    # Which existing lesson each candidate would vote on: the one it restates, if the outcomes match, else the
    # lesson with the same predicate. A vote always tests the lesson's own predicate, not the new wording.
    targets: list[Lesson | None] = []
    for c in candidates:
        h = c.hypothesis
        named = existing.get(str(h.get("restates") or ""))
        if named is not None and named.polarity != ("avoid" if h["outcome"] == FAILURE else "do"):
            named = None
        same = existing.get("lesson-" + short_hash(str(h["predicate"]).strip(), h["outcome"]))
        target = named or same
        targets.append(target if target is not None and target.status != "retired" else None)
    voted = list(dict.fromkeys(t.id for t in targets if t is not None))

    to_label = {f"q{i}": str(c.hypothesis["predicate"]).strip() for i, c in enumerate(candidates) if not c.rule}
    to_label.update({f"L{lid}": existing[lid].predicate for lid in voted if not existing[lid].rule})
    labels, report.label_chunks_failed = label_predicates(labeler or llm, to_label, held, rng, chunk=s.label_chunk)

    def test(qid: str, rule: dict | None, outcome: str) -> dict:
        return heldout_test(tables_for(held, key, answers_for(qid, rule, held, labels), outcome),
                            min_posts_with_trait=s.min_posts_with_trait)

    results = [test(f"q{i}", c.rule, c.hypothesis["outcome"]) for i, c in enumerate(candidates)]
    lesson_results = [test(f"L{lid}", existing[lid].rule or None,
                           FAILURE if existing[lid].polarity == "avoid" else SUCCESS) for lid in voted]
    # One Benjamini-Hochberg family for every test this run makes.
    all_verdicts = verdicts(results + lesson_results, s.fdr_q)
    run_verdicts, lesson_verdicts = all_verdicts[:len(results)], all_verdicts[len(results):]
    n_tests = len(all_verdicts)
    labeled_count = len(pool)

    by_id = {ep.id: ep for ep in episodes}
    for c, result, verdict, target in zip(candidates, results, run_verdicts, targets):
        # Cited dataset posts become episode notes so [[links]] resolve and the store's lesson-evidence check
        # passes. Run episodes (ep-<action id>) are left alone: Krish's round_end writes those.
        for eid in c.evidence + c.contrast:
            if by_id[eid].source == "dataset":
                by_id[eid].features.setdefault("domain", domain)
                save(store, by_id[eid])
        h = c.hypothesis
        predicate = str(h["predicate"]).strip()
        result = {**result, "verdict": verdict, "checked_by": "rule" if c.rule else "labeler", "q": s.fdr_q,
                  "tests_in_run": n_tests, "boost_filter": filters["boost_filter"]}
        refl = Reflection(
            id="refl-" + short_hash(str(h.get("claim", "")), ",".join(sorted(c.evidence))),
            claim=str(h.get("claim", "")).strip(),
            predicate=predicate,
            outcome=h["outcome"],
            evidence=c.evidence,
            contrast=c.contrast,
            group=c.batch.group,
            scope="domain",
            domain=domain,
            heldout=result,
            status={"supported": "supported", "contradicted": "rejected"}.get(verdict, "inconclusive"),
            restates=target.id if target is not None else "",
            dropped_citations=c.dropped,
            rule=c.rule or {},
            rule_problem=c.rule_problem or "",
            transfer_hint=str(h.get("transfer") or ""),
            filters=filters,
            caveats=caveats_for(h, filters),
            created=today,
        )
        save(store, refl)
        report.reflections.append(refl)
        if target is not None:
            if refl.id not in target.reflections:
                target.reflections.append(refl.id)
            save(store, target)
        elif verdict == "supported":
            lesson = Lesson(
                id="lesson-" + short_hash(predicate, refl.outcome),
                text=str(h.get("lesson") or refl.claim).strip(),
                predicate=predicate,
                rule=c.rule or {},
                confidence=result["effect"],
                evidence=c.evidence + c.contrast,
                reflections=[refl.id],
                valid_until=_until(today, s.valid_days),
                scope="domain",
                domain=domain,
                domains=[domain],
                transfer_hint=refl.transfer_hint,
                polarity="avoid" if refl.outcome == FAILURE else "do",
                support=1,
                last_test=result,
                labeled_at_test=labeled_count,
                caveats=refl.caveats,
                created=today,
                updated=today,
            )
            if lesson.id in existing:  # an earlier candidate in this run created it
                existing[lesson.id].reflections.append(refl.id)
                save(store, existing[lesson.id])
            else:
                save(store, lesson)
                existing[lesson.id] = lesson
                report.lessons_new.append(lesson.id)

    for lid, result, verdict in zip(voted, lesson_results, lesson_verdicts):
        result = {**result, "verdict": verdict, "checked_by": "rule" if existing[lid].rule else "labeler",
                  "q": s.fdr_q, "tests_in_run": n_tests, "boost_filter": filters["boost_filter"]}
        save(store, vote(existing[lid], verdict, result, f"reflect-{today}-seed{seed}", today, domain,
                         labeled_count, s.valid_days))
        report.lessons_voted.append(lid)

    counts = {v: run_verdicts.count(v) for v in ("supported", "contradicted", "inconclusive")}
    print(f"[reflect] {report.batches} batches, {report.proposed} proposed, {report.linked} linked, "
          f"{report.heldout_posts} held-out posts in {report.heldout_strata} strata, {counts}, "
          f"{len(report.lessons_new)} new lessons, {len(report.lessons_voted)} votes")
    return report


def revalidate(episodes: list[Episode], llm, store, *, domain: str, settings: ReflectSettings | None = None,
               labeler=None, key: GroupKey = by_account_and_launch, seed: int = 1,
               now: datetime | None = None, force: bool = False) -> list[tuple[str, str, dict]]:
    """Retest due lessons of one domain and vote on them.

    Test posts exclude every episode a due lesson cites, so a lesson is never retested on its own evidence.
    """
    s = settings or ReflectSettings()
    today = (now or datetime.now(timezone.utc)).date().isoformat()
    pool, filters = usable(episodes, require_boost_flags=s.require_boost_flags,
                           min_label_age_hours=s.min_label_age_hours)
    due = [l for l in (load(m) for m in store.list_memories("lesson"))
           if l.domain == domain and l.status != "retired"
           and (force or due_for_retest(l, len(pool), today, s.min_new_labeled))]
    if not due:
        return []
    cited = {eid for l in due for eid in l.evidence}
    _, held = split_heldout([ep for ep in pool if ep.id not in cited], key, random.Random(seed),
                            min_per_side=s.heldout_min_per_side)
    to_label = {l.id: l.predicate for l in due if not l.rule}
    labels, _ = label_predicates(labeler or llm, to_label, held, random.Random(seed), chunk=s.label_chunk)
    results = [heldout_test(tables_for(held, key, answers_for(l.id, l.rule or None, held, labels),
                                       FAILURE if l.polarity == "avoid" else SUCCESS),
                            min_posts_with_trait=s.min_posts_with_trait) for l in due]
    out = []
    for lesson, result, verdict in zip(due, results, verdicts(results, s.fdr_q)):
        result = {**result, "verdict": verdict, "boost_filter": filters["boost_filter"]}
        save(store, vote(lesson, verdict, result, f"revalidation-{today}", today, domain, len(pool), s.valid_days))
        out.append((lesson.id, verdict, result))
    return out


def report_json(report: ReflectionReport) -> str:
    return json.dumps(
        {
            "filters": report.filters,
            "batches": report.batches,
            "proposed": report.proposed,
            "linked": report.linked,
            "heldout_posts": report.heldout_posts,
            "heldout_strata": report.heldout_strata,
            "no_shared_trait": report.no_shared_trait,
            "proposal_errors": report.proposal_errors,
            "label_chunks_failed": report.label_chunks_failed,
            "lessons_new": report.lessons_new,
            "lessons_voted": report.lessons_voted,
            "reflections": [
                {"id": r.id, "status": r.status, "outcome": r.outcome, "claim": r.claim, "predicate": r.predicate,
                 "rule": r.rule, "rule_problem": r.rule_problem, "evidence": r.evidence, "contrast": r.contrast,
                 "group": r.group, "heldout": r.heldout} for r in report.reflections
            ],
        },
        indent=1,
    )
