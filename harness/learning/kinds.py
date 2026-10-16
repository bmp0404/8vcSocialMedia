"""What a memory is: the kinds this package writes, mapped onto the note types of Krish's store.

Krish's store (`harness/memstore/`, design in `memory/README.md`) has six note types: entity, fact,
lesson, procedure, episode and reflection. Every note needs `id` (kebab-case, at most 80 characters), `type`, `title`,
`aliases` (at least one), and `created` and `updated` dates. Bodies are dated bullets with `[[id]]`
links. Each dataclass here writes all its fields into the frontmatter, so code reads the frontmatter
and never parses the body; the body is the human-readable view.

- Episode (type episode): one action and what happened to it. The action can be ours (a run's draft,
  `ep-<action id>`) or someone else's (a dataset post, `post-<tweet id>`). Eval scores, the human
  verdict and real metrics stay separate outcomes, because they disagree and each has its own owner.
- Reflection (type reflection): a claim about why a group of episodes shares an outcome, citing those
  episodes and the contrasting ones, with the result of testing it on held-out episodes.
- Lesson (type lesson): advice a mind can act on, promoted from a reflection that held up, with
  confidence, evidence and a `valid_until` date after which it must be retested.
- Entity (type entity): a profile of an account, person, team or brand.
- Fact (type fact): one dated claim about an entity, with its source and a verbatim quote. An entity's history is its facts in date order; a newer fact can supersede an older one.
- Procedure (type procedure): how reviewers edit one mind's drafts, as a list of edit pairs (the draft
  and the text a human preferred).
"""

import hashlib
import re
from dataclasses import asdict, dataclass, field, fields
from typing import ClassVar

SUCCESS, FAILURE = "success", "failure"

# Which outcome decides an episode's label when several exist. Real metrics outrank a human verdict,
# which outranks the dataset's own category, which outranks a model's prediction.
OUTCOME_PRIORITY = ("metrics", "review", "dataset", "eval")

ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def short_hash(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:10]


def slug(text: str) -> str:
    """Kebab-case for note ids: lowercase, runs of anything but a-z0-9 become one hyphen."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def day(value: str | None) -> str:
    """The YYYY-MM-DD part of an ISO date or timestamp, or "" when there is none."""
    head = (value or "")[:10]
    return head if DATE_PATTERN.match(head) else ""


def check_note(kind: str, meta: dict) -> None:
    """Raise when a note would break the store's required frontmatter."""
    nid = meta.get("id", "")
    if not ID_PATTERN.match(nid) or len(nid) > 80:
        raise ValueError(f"{kind} id {nid!r} must be kebab-case [a-z0-9-] and at most 80 characters")
    if not str(meta.get("title", "")).strip():
        raise ValueError(f"{nid}: title is required")
    if not meta.get("aliases"):
        raise ValueError(f"{nid}: aliases needs at least one entry")
    for key in ("created", "updated"):
        if not DATE_PATTERN.match(str(meta.get(key, ""))):
            raise ValueError(f"{nid}: {key} must be YYYY-MM-DD, got {meta.get(key)!r}")


KEYWORD_STOP = set(
    "a an and are as at be but by does did do for from has have how in into is it its of on or that the this "
    "to was were what when where which who why will with without post posts their they them than then there".split()
)


def keywords(text: str, limit: int = 6) -> list[str]:
    """Distinct content words, in order, for note aliases: the store's search only finds spelled-out words."""
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9'-]*", (text or "").lower()):
        if len(w) > 2 and w not in KEYWORD_STOP and w not in out:
            out.append(w)
    return out[:limit]


def rule_text(rule: dict) -> str:
    """'media is one of photo, video' style rendering of a reflect.RULE_OPS rule, or '' with none."""
    if not rule:
        return ""
    feat, op, value = rule["feature"], rule["op"], rule["value"]
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    words = {"eq": "is", "in": "is one of", "lt": "is under", "gt": "is over",
             "contains_any": "contains any of", "contains_none": "contains none of"}
    return f"{feat} {words.get(op, op)} {value}"


def heldout_sentence(result: dict, outcome: str) -> str:
    """'trait on 31 of 40 held-out failures vs 9 of 35 successes, same accounts', or '' with no test."""
    if not result or "target_posts" not in result:
        return "not tested on held-out posts"
    plural = {SUCCESS: "successes", FAILURE: "failures"}
    other = SUCCESS if outcome == FAILURE else FAILURE
    return (f"trait on {result['target_with_trait']} of {result['target_posts']} held-out {plural[outcome]} vs "
            f"{result['other_with_trait']} of {result['other_posts']} {plural[other]} from the same accounts")


def bullet(label: str, text: str, when: str) -> str:
    """One body line in the store's format: `- **Label:** statement; stated on YYYY-MM-DD.`"""
    return f"- **{label}:** {text.rstrip('.')}; stated on {when}."


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


class _Kind:
    KIND: ClassVar[str]

    def note_fields(self) -> dict:
        """title, aliases, created, updated and any optional store keys (tags, evidence, source...)."""
        raise NotImplementedError

    def body(self) -> str:
        raise NotImplementedError

    def to_memory(self) -> tuple[str, str, dict]:
        meta = {**asdict(self), **self.note_fields(), "id": self.id, "type": self.KIND}
        check_note(self.KIND, meta)
        return self.KIND, self.body(), meta

    @classmethod
    def from_memory(cls, memory) -> "_Kind":
        if memory.kind != cls.KIND:
            raise ValueError(f"{memory.id} is a {memory.kind!r} note, not {cls.KIND!r}")
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in memory.meta.items() if k in names})


@dataclass
class Outcome:
    label: str  # SUCCESS, FAILURE, or "" when the source gives no verdict
    source: str  # one of OUTCOME_PRIORITY
    raw: str = ""  # the source's own value: "Deadweight", "killed", "0.62"
    metrics: dict = field(default_factory=dict)  # X API v2 public_metrics keys (like_count, reply_count...)
    note: str = ""  # a reviewer's note or an eval critique
    at: str = ""
    reasons: list[str] = field(default_factory=list)  # review kill reason codes (memory.REVIEW_REASONS)


@dataclass
class Episode(_Kind):
    KIND: ClassVar[str] = "episode"

    id: str  # "post-<tweet id>" for posts already on X, "ep-<action id>" for our drafts
    source: str  # "dataset" or "run"
    author_username: str  # X username without the @, as in X API v2 user objects
    text: str
    action_type: str = "post"
    created_at: str = ""  # ISO 8601; dataset rows only have a date
    tweet_id: str | None = None  # X API v2 tweet id, once the post exists on X
    loop_id: str = ""  # the round that produced it, "run-<id>" for today's runs
    url: str | None = None
    features: dict = field(default_factory=dict)  # media, has_link, eng_ratio_author_median, boost_suspect...
    # What the agent saw at decision time: narrative id, title and summary, signal ids, texts and metrics.
    observation: dict = field(default_factory=dict)
    # How the draft was made and chosen, for counterfactual reruns: mind, provider, model, prompt_hash,
    # siblings [{"id", "predicted_score", "vetoed"}], chosen_by_bot (true/false/null), draft_meta.
    decision: dict = field(default_factory=dict)
    # Other accounts' posts on the same story: [{"tweet_id", "author_username", "engagement", "author_median"}].
    peers: list[dict] = field(default_factory=list)
    # One per source and age: metrics outcomes carry raw "+24h" or "+48h" and `at`.
    outcomes: list[Outcome] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.outcomes = [o if isinstance(o, Outcome) else Outcome(**o) for o in self.outcomes]

    def outcome(self) -> Outcome | None:
        """The highest-priority outcome that carries a label."""
        labeled = [o for o in self.outcomes if o.label in (SUCCESS, FAILURE)]
        for source in OUTCOME_PRIORITY:
            for o in labeled:
                if o.source == source:
                    return o
        return None

    def label(self) -> str:
        o = self.outcome()
        return o.label if o else ""

    def note_fields(self) -> dict:
        dates = [d for d in (day(self.created_at), *(day(o.at) for o in self.outcomes)) if d]
        return {
            "title": f"@{self.author_username} {self.action_type}: {_clip(self.text, 60)}",
            "aliases": [a for a in (self.author_username, self.tweet_id or self.id.removeprefix("ep-"),
                                    self.features.get("media"), "launch" if self.features.get("is_launch") else "")
                        if a],
            "created": dates[0] if dates else "",
            "updated": max(dates) if dates else "",
            "tags": [t for t in (self.source, self.label()) if t],
        }

    def body(self) -> str:
        d = self.note_fields()["created"]
        lines = [bullet(self.action_type.capitalize(), f"@{self.author_username}: {_clip(self.text, 400)}", d)]
        for o in self.outcomes:
            if o.label or o.raw or o.metrics:
                detail = ", ".join(f"{k} {v}" for k, v in o.metrics.items())
                lines.append(bullet(f"{o.source.capitalize()} outcome",
                                    f"{o.label or 'unlabeled'}" + (f" ({o.raw})" if o.raw else "")
                                    + (f", {detail}" if detail else "")
                                    + (f", reasons {' '.join(o.reasons)}" if o.reasons else "")
                                    + (f", note: {_clip(o.note, 200)}" if o.note else ""), day(o.at) or d))
        return "\n".join(lines)


@dataclass
class Reflection(_Kind):
    KIND: ClassVar[str] = "reflection"

    id: str
    claim: str  # why the evidence episodes share their outcome
    predicate: str  # a yes/no question about one post, used to test the claim on held-out posts
    outcome: str  # the outcome the evidence episodes share: SUCCESS or FAILURE
    evidence: list[str]  # episode ids that share `outcome` and that the claim explains
    contrast: list[str]  # episode ids with the other outcome that lack the trait
    group: str  # how the batch was drawn, e.g. "contrast:cline,zeddotdev"
    scope: str = "general"  # "general" (should transfer to other brands) or "domain"
    domain: str = ""  # where the evidence came from, e.g. "devtools"
    heldout: dict = field(default_factory=dict)  # the stats.heldout_test result plus its run verdict
    status: str = "untested"  # "supported", "rejected", "inconclusive" or "untested"
    restates: str = ""  # lesson id this claim repeats, if the model said so
    dropped_citations: list[str] = field(default_factory=list)
    rule: dict = field(default_factory=dict)  # code-checkable form of the predicate, when there is one
    rule_problem: str = ""  # why the model's rule was unusable, if it gave a bad one
    transfer_hint: str = ""  # the model's guess at "general" or "domain"; scope ignores it until tested
    filters: dict = field(default_factory=dict)  # what reflect.usable dropped, and whether boost flags existed
    caveats: list[str] = field(default_factory=list)  # known reasons to doubt the test, from reflect.caveats_for
    created: str = ""

    def note_fields(self) -> dict:
        return {"title": _clip(self.claim, 90), "aliases": keywords(self.predicate) or [self.id], "created": self.created,
                "updated": self.created, "tags": [self.status, self.outcome, self.domain or "brand"],
                "source": "reflect"}

    def body(self) -> str:
        h = self.heldout
        lines = [bullet("Claim", self.claim, self.created),
                 bullet("Evidence", f"shared by {' '.join(f'[[{e}]]' for e in self.evidence)}, absent from "
                        f"{' '.join(f'[[{e}]]' for e in self.contrast)}", self.created),
                 bullet("Predicate", self.predicate + (f" (tested as: {rule_text(self.rule)})" if self.rule else ""),
                        self.created)]
        if h:
            lines.append(bullet("Held-out test", f"{self.status}, {heldout_sentence(h, self.outcome)}", self.created))
        lines += [bullet("Caveat", c, self.created) for c in self.caveats]
        return "\n".join(lines)


@dataclass
class Lesson(_Kind):
    KIND: ClassVar[str] = "lesson"

    id: str
    text: str  # imperative advice a mind can follow
    predicate: str  # the testable form, rerun at revalidation
    # Effect from the latest supporting test: share of target-outcome held-out posts with the trait minus
    # share of other-outcome posts with it, within the same strata. Not a probability that the lesson holds.
    confidence: float
    evidence: list[str]  # episode ids behind it; the store rejects a lesson without one backed by a real outcome
    reflections: list[str]  # reflection or revalidation ids that tested it, oldest first
    valid_until: str  # YYYY-MM-DD fallback; a lesson is also due once enough new labeled episodes arrive
    rule: dict = field(default_factory=dict)  # code-checkable form of the predicate, when there is one
    scope: str = "domain"  # "domain" until it passes on a second domain's episodes, then "general"
    domain: str = ""
    domains: list[str] = field(default_factory=list)  # domains it has passed a held-out test on
    transfer_hint: str = ""
    polarity: str = "avoid"  # "avoid" when the trait predicts failure, "do" when it predicts success
    status: str = "active"  # "active", "stale" or "retired"
    support: int = 0  # held-out tests that agreed
    contradict: int = 0  # held-out tests where the opposite direction was significant
    inconclusive: int = 0  # held-out tests that showed neither
    last_test: dict = field(default_factory=dict)  # the latest supporting stats.heldout_test result
    labeled_at_test: int = 0  # labeled episodes in its domain when last tested, for the retest trigger
    caveats: list[str] = field(default_factory=list)  # known reasons to doubt it, shown wherever it is shown
    created: str = ""
    updated: str = ""

    def note_fields(self) -> dict:
        return {"title": _clip(self.text, 90), "aliases": keywords(self.text + " " + self.predicate) or [self.id],
                "tags": ["pattern", self.domain or "brand", self.polarity, self.status], "source": "reflect"}

    def body(self) -> str:
        return "\n".join(
            [bullet("Lesson", self.text, self.created),
             bullet("Held-out test", f"{heldout_sentence(self.last_test, FAILURE if self.polarity == 'avoid' else SUCCESS)}, "
                    f"{self.support} supporting, {self.contradict} contradicting and {self.inconclusive} inconclusive "
                    f"tests, retest by {self.valid_until}", self.updated),
             bullet("Tested as", f"{rule_text(self.rule) or self.predicate} in "
                    f"{' '.join(f'[[{r}]]' for r in self.reflections)}", self.updated),
             bullet("Evidence", " ".join(f"[[{e}]]" for e in self.evidence[:12]), self.created)]
            + [bullet("Caveat", c, self.updated) for c in self.caveats]
        )


@dataclass
class Entity(_Kind):
    KIND: ClassVar[str] = "entity"

    id: str  # "entity-<slug>"
    name: str
    summary: str
    entity_type: str = "account"  # account, person, team, brand
    aliases: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    relations: list[dict] = field(default_factory=list)  # {"type": "rival", "target": "entity-..."}
    updated: str = ""

    def __post_init__(self) -> None:
        self.aliases = self.aliases or [self.name]

    def note_fields(self) -> dict:
        return {"title": self.name, "aliases": self.aliases, "created": self.updated,
                "updated": self.updated, "tags": [self.entity_type]}

    def body(self) -> str:
        return bullet("Profile", self.summary, self.updated)


@dataclass
class Fact(_Kind):
    KIND: ClassVar[str] = "fact"

    id: str
    subject: str  # entity id
    question: str
    answer: str
    quote: str  # verbatim from the source; ingest drops facts whose quote it cannot find
    source_url: str
    as_of: str
    valid_until: str = ""
    supersedes: str = ""
    status: str = "current"  # "current" or "superseded"

    def note_fields(self) -> dict:
        return {"title": _clip(self.question, 90), "aliases": [self.subject, *keywords(self.question)],
                "created": self.as_of, "updated": self.as_of, "tags": [self.status], "source": self.source_url}

    def body(self) -> str:
        return "\n".join([bullet("Fact", f"[[{self.subject}]] {self.answer}", self.as_of),
                          bullet("Quote", f"\"{_clip(self.quote, 300)}\" ({self.source_url})", self.as_of)])


@dataclass
class EditPair:
    """A draft and the text a human changed it to, the most direct preference signal we get."""

    action_id: str
    original: str
    edited: str
    verdict: str  # the review status the edit was saved with
    note: str = ""
    narrative_id: str = ""
    action_type: str = ""
    diff: dict = field(default_factory=dict)
    at: str = ""


@dataclass
class Procedure(_Kind):
    KIND: ClassVar[str] = "procedure"

    id: str  # "procedure-review-edits-<mind>"
    mind: str
    pairs: list[EditPair] = field(default_factory=list)
    created: str = ""
    updated: str = ""

    def __post_init__(self) -> None:
        self.pairs = [p if isinstance(p, EditPair) else EditPair(**p) for p in self.pairs]

    def note_fields(self) -> dict:
        return {"title": f"How reviewers edit {self.mind} drafts", "aliases": [self.mind],
                "tags": ["review", "edits"], "evidence": [f"ep-{p.action_id}" for p in self.pairs],
                "source": "review"}

    def body(self) -> str:
        lines = []
        for p in self.pairs:
            text = (f"[[ep-{p.action_id}]] {p.verdict} after an edit. Draft: \"{_clip(p.original, 240)}\" "
                    f"Kept: \"{_clip(p.edited, 240)}\"" + (f" Note: {_clip(p.note, 200)}" if p.note else ""))
            lines.append(bullet("Edit", text, day(p.at) or self.updated))
        return "\n".join(lines)


KINDS: dict[str, type[_Kind]] = {k.KIND: k for k in (Episode, Reflection, Lesson, Entity, Fact, Procedure)}


def load(memory) -> _Kind:
    """Turn a stored note back into its dataclass; raises on a type this package doesn't define."""
    cls = KINDS.get(memory.kind)
    if cls is None:
        raise ValueError(f"no dataclass for note type {memory.kind!r} ({memory.id})")
    return cls.from_memory(memory)


def save(store, item: _Kind) -> str:
    kind, body, frontmatter = item.to_memory()
    return store.write_memory(kind, body, frontmatter)
