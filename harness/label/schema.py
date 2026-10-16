"""The label schema: what each LLM annotator answers about a post or reply.

`SCHEMA` is the source of truth; `write_schema()` renders `data/labels/schema.json` in the contract
shape the labeling UI reads. Each field also has a `guide`, the anchors the annotator prompt shows;
the UI doesn't need it, so it stays out of schema.json.

Version 1 was the brief's starting set plus two veto fields, tested in the pilot. Version 2 drops
the two that showed no validity on the dev benchmarks (DESIGN.md has the numbers). A rewritten field
gets a new name so labels under one name always answer one question.
"""

import json
from pathlib import Path

from harness import config

LABELS_DIR = config.ROOT / "data" / "labels"
SCHEMA_PATH = LABELS_DIR / "schema.json"

VERSION = 2

FIELDS_V1 = [
    {
        "name": "format",
        "kind": "enum",
        "values": ["announcement", "tip_or_howto", "opinion_or_take", "question", "joke_or_meme",
                   "praise_or_thanks", "complaint_or_criticism", "promo_or_event", "link_or_share", "other"],
        "question": "What is this item mainly doing?",
        "applies_to": ["post", "reply"],
        "guide": "announcement: news about the author's own product, company or release. tip_or_howto: explains how "
                 "something works or how to do it. opinion_or_take: argues a claim or gives a view. question: mainly "
                 "asks something. joke_or_meme: the point is to be funny. praise_or_thanks: congratulates, compliments, "
                 "thanks or hypes. complaint_or_criticism: reports a problem or criticizes. promo_or_event: an event, "
                 "webinar, hiring post, discount or call to sign up. link_or_share: mostly passes along a link, video or "
                 "someone else's post with little comment of its own. other: none of these. Pick the one that best "
                 "describes the main purpose.",
    },
    {
        "name": "specificity",
        "kind": "ordinal",
        "values": [1, 2, 3, 4, 5],
        "question": "How concrete is it?",
        "applies_to": ["post", "reply"],
        "guide": "1: generic, could be said about almost anything (\"Great work!\", \"Huge\"). 2: on topic but vague. "
                 "3: names one specific thing. 4: several specifics such as names, numbers, versions or steps. 5: dense "
                 "with concrete detail: mechanisms, measurements, exact steps or examples.",
    },
    {
        "name": "novelty",
        "kind": "ordinal",
        "values": [1, 2, 3, 4, 5],
        "question": "Would a typical follower of this account learn something new or see a new angle?",
        "applies_to": ["post", "reply"],
        "guide": "1: boilerplate, repeats the obvious or the parent. 2: mostly familiar. 3: a modest new detail or "
                 "angle. 4: clearly new information or a fresh angle. 5: surprising information or an original "
                 "insight most followers would not have seen.",
    },
    {
        "name": "humor",
        "kind": "ordinal",
        "values": ["none", "some", "main_point"],
        "question": "How much does it rely on humor?",
        "applies_to": ["post", "reply"],
        "guide": "none: no attempt at humor. some: a light joke, wordplay or playful tone alongside other content. "
                 "main_point: being funny is the point. Judge the attempt, not whether you find it funny.",
    },
    {
        "name": "reads_as_ai",
        "kind": "ordinal",
        "values": [1, 2, 3, 4, 5],
        "question": "How likely is it that a language model wrote this text?",
        "applies_to": ["post", "reply"],
        "guide": "1: clearly human: idiosyncratic voice, slang, typos, fragments, inside references. 3: could be "
                 "either. 5: reads like LLM output: generic enthusiasm, polished symmetrical sentences, em-dash "
                 "pivots, \"not X, it's Y\", tidy triads, summary-style restatement of the parent. Very short texts "
                 "with nothing to judge are 2.",
    },
    {
        "name": "adds_to_parent",
        "kind": "ordinal",
        "values": [1, 2, 3, 4, 5],
        "question": "How much does this reply add to the post it answers?",
        "applies_to": ["reply"],
        "guide": "1: nothing: a bare reaction, an emoji, \"this\", or off topic. 2: a short relevant reaction. 3: a "
                 "relevant opinion, question or small detail. 4: a useful point, answer or example the parent lacked. "
                 "5: a substantive contribution that readers of the thread would value.",
    },
    {
        "name": "engagement_guess",
        "kind": "ordinal",
        "values": ["below", "typical", "above", "breakout"],
        "question": "Compared with this author's usual posts or replies, how much engagement will it get?",
        "applies_to": ["post", "reply"],
        "guide": "below: less than the author's usual. typical: about the author's usual. above: clearly more. "
                 "breakout: far more, the kind that travels well beyond the author's followers. Judge from the text "
                 "and metadata only; you are not shown its counts.",
    },
    {
        "name": "engagement_bait",
        "kind": "enum",
        "values": ["no", "yes"],
        "question": "Does it explicitly ask for engagement or use bait phrasing?",
        "applies_to": ["post", "reply"],
        "guide": "yes: asks readers to like, repost, reply, follow or tag (\"drop a 🔥\", \"who agrees?\", \"comment "
                 "X below\", \"RT if\"), or withholds information to force a click or reply. A genuine question to "
                 "the audience is no. X's ranker removes bait from non-followers' feeds.",
    },
    {
        "name": "hostility",
        "kind": "ordinal",
        "values": ["none", "snark", "insult"],
        "question": "Is it hostile toward a person or group?",
        "applies_to": ["post", "reply"],
        "guide": "none: no hostility. snark: teasing, sarcasm or pointed criticism without attacking anyone. insult: "
                 "insults, mocks or demeans a person or group. Criticism of a product alone is none or snark.",
    },
]

# Dropped after the pilot (data/labels/reports/pilot-v1.json, Claude Sonnet 5.5 against GPT-6.1 Sol):
# adds_to_parent agreed (alpha 0.75) but did not predict which reply got more likes (0.53, 95% CI 0.47
# to 0.59, 438 dev pairs); engagement_bait was "yes" on 5 to 12 of 1,000 items, too rare to measure.
DROPPED = ("adds_to_parent", "engagement_bait")
FIELDS = [f for f in FIELDS_V1 if f["name"] not in DROPPED]
SCHEMA_V1 = {"version": 1, "fields": FIELDS_V1}
SCHEMA = {"version": VERSION, "fields": FIELDS}


def contract_schema(schema: dict = SCHEMA) -> dict:
    """The schema.json shape: version plus name, kind, values, question and applies_to per field."""
    keys = ("name", "kind", "values", "question", "applies_to")
    return {"version": schema["version"], "fields": [{k: f[k] for k in keys} for f in schema["fields"]]}


def write_schema(path: Path = SCHEMA_PATH, schema: dict = SCHEMA) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract_schema(schema), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fields_for(kind: str, schema: dict = SCHEMA) -> list[dict]:
    return [f for f in schema["fields"] if kind in f["applies_to"]]


def validate_labels(labels: dict, kind: str, schema: dict = SCHEMA) -> list[str]:
    """Problems with one annotator answer; empty means every applicable field has an allowed value."""
    problems = []
    applicable = {f["name"]: f for f in fields_for(kind, schema)}
    for name, field in applicable.items():
        if name not in labels:
            problems.append(f"missing {name}")
        elif labels[name] not in field["values"]:
            problems.append(f"{name}={labels[name]!r} is not one of {field['values']}")
    for name in labels:
        if name not in applicable:
            problems.append(f"unexpected field {name}")
    return problems
