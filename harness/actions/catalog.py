"""Every action type the harness can recommend.

The X primitives (post, reply, quote, mention, follow) are the base action space. The rest come from the
"Intelligence Components + Action Types" doc, adapted for a dev-tools company, plus three the doc lacks:
release announcements, contributor thanks, and an explicit "hold". Tiers: 1 = run now, 2 = relationship
work a person approves, 3 = later or occasional.
"""

from harness.actions.core import (
    ENUM,
    INT,
    MENTION,
    OBJECTS,
    STRINGS,
    Action,
    ActionSpec,
    Context,
    S,
    grounded_list,
    grounded_url,
    known_person,
    post_id,
    require,
    seconds,
    text_fits,
    thread_parts,
    x_handle,
)

# Real X engagement, weighted toward what spreads.
X_METRICS = {"likes": 1, "replies": 3, "bookmarks": 3, "reposts": 5, "quotes": 6,
             "profile_clicks": 2, "link_clicks": 4, "follows": 10}
X_FEEDBACK = ("off_brand", "weak_hook", "unsupported_claim", "stale", "wrong_target")
PUBLIC = ("x", "linkedin", "reddit", "hackernews", "blog")
OUTREACH_TYPES = ("send_dm", "follow_up", "invite_expert_collab", "propose_reciprocal_share",
                  "request_warm_intro", "pitch_journalist", "pitch_podcast")

TEXT = S("the draft. On X: under 280 characters per post (links count 23); a thread separates posts "
         "with a line containing only ---")
MESSAGE = S("the message to send, written to this one person")
TARGET_URL = S("URL of the post or thread, copied exactly from the narratives")
TARGET_HANDLE = S("X handle without the @, from the narratives or the CRM")
PERSON = S("who this is for: an X handle, GitHub username, email, or name from the CRM or the narratives")


def _x_post(a: Action) -> dict:
    return {"op": "create_post", "posts": thread_parts(a.text)}


def _x_reply(a: Action) -> dict:
    return {"op": "reply", "in_reply_to_post_id": post_id(a.target_url), "posts": thread_parts(a.text)}


def _normalize_mention(a: Action) -> None:
    a.target_handle = (a.target_handle or "").lstrip("@")
    if a.target_handle and a.target_handle.lower() not in {h.lower() for h in MENTION.findall(a.text)}:
        a.text = f"@{a.target_handle} {a.text}".strip()


def _normalize_follow(a: Action) -> None:
    a.target_handle = (a.target_handle or "").lstrip("@")
    a.text = ""


def _clip_check(a: Action, ctx: Context, errors: list[str]) -> None:
    src = a.details.get("source_url")
    if src not in {m.get("url") for m in ctx.media}:
        errors.append(f"source_url is not in mission/media.json: {src!r}")
    start, end = seconds(a.details.get("start")), seconds(a.details.get("end"))
    if start is None or end is None:
        errors.append("start and end must be timestamps like 12:30 or 1:02:05")
    elif not 5 <= end - start <= 180:
        errors.append(f"clip is {end - start}s; keep it between 5s and 3 minutes")


def _shortlist_check(a: Action, ctx: Context, errors: list[str]) -> None:
    cands = a.details.get("candidates") or []
    if not cands:
        errors.append("candidates is empty")
    for c in cands:  # the CRM, not the model, decides who's already known
        c["already_known"] = ctx.contact(c.get("person")) is not None


def _roundtable_check(a: Action, ctx: Context, errors: list[str]) -> None:
    people = [i.get("person") for i in a.details.get("invitees") or []]
    cap = a.details.get("capacity") or 0
    if len(people) < 2:
        errors.append("a roundtable needs at least 2 invitees")
    if cap and len(people) > cap:
        errors.append(f"{len(people)} invitees for {cap} seats")
    for p in people:
        c = ctx.contact(p)
        if not ctx.knows(p):
            errors.append(f"invitee {p!r} is not in the CRM or the narratives")
        elif c and c.get("do_not_contact"):
            errors.append(f"invitee {p!r} is marked do_not_contact")
        elif ctx.recent({"invite_to_roundtable"}, "target_person", p, 30):
            errors.append(f"{p} was invited to a roundtable in the last 30 days")
    a.target_person = ", ".join(p for p in people if p)


def _swag_check(a: Action, ctx: Context, errors: list[str]) -> None:
    items = {i["item"].lower(): i for i in ctx.swag.get("items", [])}
    item = items.get((a.details.get("item") or "").lower())
    if not item:
        errors.append(f"item {a.details.get('item')!r} is not in mission/swag.json")
        return
    budget = ctx.swag.get("monthly_budget_usd")
    if budget is not None:
        spent = sum(items.get((h.get("details") or {}).get("item", "").lower(), {}).get("cost_usd", 0)
                    for h in ctx.recent({"send_swag"}, "type", "send_swag", 30))
        if spent + item.get("cost_usd", 0) > budget:
            errors.append(f"over the swag budget: ${spent} spent of ${budget} in the last 30 days")


SPECS = [
    # --- X primitives: the base action space the posting bot executes -------------------------
    ActionSpec(
        type="post", tool="create_post", title="X post", tier=1, play=False,
        description="Publish an original post or thread to the account's timeline. Use for takes, jokes, "
        "demos, and stories that stand on their own.",
        fields={"text": TEXT}, checks=(text_fits(),), to_x=_x_post,
        feedback=X_FEEDBACK, metrics=X_METRICS,
    ),
    ActionSpec(
        type="reply", tool="reply_to_post", title="X reply", tier=1, play=False,
        description="Reply under an existing X post. Best on high-traffic posts where an early, sharp reply "
        "gets seen. The reply has to add something: a joke, a fact, or a sharper take.",
        fields={"target_url": TARGET_URL, "text": TEXT}, checks=(grounded_url(x_only=True), text_fits()),
        to_x=_x_reply, feedback=X_FEEDBACK, metrics=X_METRICS,
    ),
    ActionSpec(
        type="quote", tool="quote_post", title="X quote post", tier=1, play=False,
        description="Quote an existing X post with commentary, so it reaches the account's own followers. "
        "Use when the take works without the reader opening the original.",
        fields={"target_url": TARGET_URL, "text": TEXT}, checks=(grounded_url(x_only=True), text_fits()),
        to_x=lambda a: {"op": "quote", "quote_post_id": post_id(a.target_url), "posts": thread_parts(a.text)},
        feedback=X_FEEDBACK, metrics=X_METRICS,
    ),
    ActionSpec(
        type="mention", tool="mention_account", title="X mention", tier=1, play=False,
        description="Post that tags another account to pull them into the conversation. The handle is added "
        "to the text if it isn't already there.",
        fields={"target_handle": TARGET_HANDLE, "text": TEXT}, checks=(x_handle(), text_fits()),
        to_x=lambda a: {"op": "create_post", "posts": thread_parts(a.text), "mentions": [a.target_handle]},
        normalize=_normalize_mention, feedback=X_FEEDBACK, metrics=X_METRICS,
    ),
    ActionSpec(
        type="follow", tool="follow_account", title="X follow", tier=1, play=False, audience="person",
        description="Follow an account likely to notice and engage back: developers in the conversation, "
        "tool builders, and community voices.",
        fields={"target_handle": TARGET_HANDLE}, checks=(x_handle(),),
        to_x=lambda a: {"op": "follow", "handle": a.target_handle}, normalize=_normalize_follow,
        feedback=("irrelevant", "too_big", "spammy"), metrics={"follow_back": 5, "replies": 3},
        cooldown_days=365, cooldown_key="target_handle",
    ),

    # --- Tier 1: run now -----------------------------------------------------------------------
    ActionSpec(
        type="respond_to_mention", tool="respond_to_mention", title="Respond to a mention", tier=1,
        description="Answer someone who mentioned the product or its problem space: a bug report, a "
        "question, a feature request, a comparison, or a complaint. Classify the intent, then choose a "
        "public reply, a DM, or escalation to the team.",
        channels=("x", "github", "hackernews", "reddit", "discord"),
        fields={
            "target_url": TARGET_URL,
            "mention_intent": ENUM(("bug", "question", "feature_request", "praise", "complaint",
                                    "comparison", "churn_risk"), "what the person wants"),
            "response_mode": ENUM(("public_reply", "dm", "escalate_to_team"), "how to respond"),
            "text": TEXT,
        },
        checks=(grounded_url(), text_fits()),
        to_x=lambda a: _x_reply(a) if a.details.get("response_mode") == "public_reply" else None,
        feedback=("misread_intent", "wrong_priority", "wrong_channel", "inaccurate_answer", "no_response_needed"),
        metrics={"resolved": 10, "author_replied": 5, "follow_up_questions": 2, "likes": 1, "escalated": 1},
        measure_days=3,
    ),
    ActionSpec(
        type="answer_recurring_question", tool="answer_recurring_question", title="Answer a recurring question",
        tier=1,
        description="A question developers keep asking (in at least two places in the narratives) that the "
        "docs don't answer well. Draft the answer as a docs page, FAQ entry, post, or thread, and list "
        "anything the team has to supply.",
        channels=("docs", "blog", "x", "github", "discord"),
        fields={
            "question": S("the question, in the asker's words"),
            "asked_in": {"type": "array", "items": {"type": "string"},
                         "description": "URLs from the narratives where it was asked (at least 2)"},
            "coverage": ENUM(("not_covered", "partly_covered", "covered_but_hard_to_find"), "current docs coverage"),
            "answer_format": ENUM(("docs_page", "faq_entry", "blog_post", "x_thread", "github_discussion"), "format"),
            "text": TEXT,
        },
        checks=(require("question"), grounded_list("asked_in", 2), text_fits()),
        to_x=_x_post, feedback=("already_answered", "misunderstood", "low_priority", "inaccurate_answer"),
        metrics={"helpful_votes": 5, "saves": 3, "source_clicks": 3, "docs_views": 1, "repeat_questions": -2},
        measure_days=14,
    ),
    ActionSpec(
        type="reply_to_prominent_voice", tool="reply_to_prominent_voice", title="Reply to a prominent voice",
        tier=1,
        description="Join a fresh conversation started by a well-known developer or tool builder, adding a "
        "specific insight, example, or data point. Never a product plug as the opener.",
        channels=("x", "hackernews", "reddit", "linkedin"),
        fields={"target_url": TARGET_URL, "target_person": PERSON,
                "why_them": S("why this person's conversation is worth joining"), "text": TEXT},
        checks=(grounded_url(), known_person(), text_fits()), to_x=_x_reply,
        feedback=("wrong_tone", "stale", "unsuitable_person", "nothing_to_add"),
        metrics={"author_replied": 10, "replies": 3, "likes": 1, "profile_clicks": 2, "link_clicks": 4,
                 "negative_replies": -5},
        measure_days=2, cooldown_days=7,
    ),
    ActionSpec(
        type="publish_on_trend", tool="publish_on_trend", title="Publish on an audience trend", tier=1,
        description="A topic growing among the developers we serve. Publish a take, demo, or meme that "
        "joins it with something the brand can credibly say.",
        channels=PUBLIC,
        fields={"trend": S("the trend, in one line"), "segment": S("which audience segment it's for"),
                "format": ENUM(("post", "thread", "demo", "meme", "article"), "format"), "text": TEXT},
        checks=(require("trend"), grounded_list("evidence", 1), text_fits()), to_x=_x_post,
        feedback=("irrelevant", "stale", "off_brand", "weak_material", "unsupported_claim"),
        metrics=X_METRICS, measure_days=3,
    ),
    ActionSpec(
        type="publish_on_competitor_trend", tool="publish_on_competitor_trend",
        title="Publish on a competitor-audience trend", tier=1,
        description="A conversation among a competitor's or adjacent tool's users (launches, complaints, "
        "limits). Add a distinct point of view; never dunk on the competitor or its users.",
        channels=PUBLIC,
        fields={"competitor": S("the tool or company whose audience this is"), "trend": S("the conversation"),
                "distinct_angle": S("what we add that their audience isn't already hearing"), "text": TEXT},
        checks=(require("competitor", "distinct_angle"), grounded_list("evidence", 1), text_fits()),
        to_x=_x_post, feedback=("wrong_audience", "weak_relevance", "nothing_distinctive", "punches_down"),
        metrics={**X_METRICS, "installs": 15}, measure_days=3,
    ),
    ActionSpec(
        type="follow_up", tool="follow_up", title="Follow up on a previous interaction", tier=1,
        audience="person", outreach=True,
        description="Close the loop with someone we've talked to: their feature request shipped, their bug "
        "is fixed, their question has an answer, or a promised next step is due.",
        channels=("x", "email", "github", "discord", "linkedin"),
        fields={"target_person": PERSON,
                "prior_interaction": S("URL or CRM note of the earlier interaction"),
                "commitment": S("what was asked or promised"),
                "status": ENUM(("shipped", "fixed", "answered", "checking_in"), "what changed"),
                "text": MESSAGE},
        checks=(known_person(), require("prior_interaction", "commitment"), text_fits("message")),
        feedback=("already_handled", "no_longer_relevant", "contact_declined"),
        metrics={"replied": 5, "commitment_closed": 8, "meeting": 10, "reactivated": 10},
        measure_days=14, cooldown_days=7,
    ),
    ActionSpec(
        type="announce_release", tool="announce_release", title="Announce a release", tier=1,
        description="Turn a shipped release into a channel-native announcement: what changed, why a "
        "developer should care, and how to get it. Only shipped, fact-sheet-backed features.",
        channels=("x", "hackernews", "reddit", "linkedin", "discord", "blog", "newsletter"),
        fields={"version": S("release version or name"), "changelog_url": S("link to the changelog"),
                "headline_change": S("the one change to lead with"), "text": TEXT},
        checks=(require("version", "changelog_url", "headline_change"), text_fits()), to_x=_x_post,
        feedback=("not_newsworthy", "wrong_channel", "overclaims", "bad_timing"),
        metrics={**X_METRICS, "installs": 15}, measure_days=7,
    ),
    ActionSpec(
        type="thank_contributor", tool="thank_contributor", title="Thank a contributor", tier=1,
        audience="person",
        description="Publicly thank someone for a merged pull request, a sharp bug report, an integration, or "
        "a great write-up about the product.",
        channels=("github", "x", "discord"),
        fields={"target_person": PERSON, "contribution_url": S("link to what they did"), "text": TEXT},
        checks=(known_person(), require("contribution_url"), text_fits()), to_x=_x_post,
        feedback=("wrong_person", "too_late", "tone_off"),
        metrics={"author_replied": 5, "second_contribution": 15, "reposts": 5},
        measure_days=30, cooldown_days=30,
    ),
    ActionSpec(
        type="hold", tool="hold_off", title="Hold / wait", tier=1, play=False, audience="internal",
        channels=("internal",),
        description="Recommend doing nothing on a narrative for now (too early, too heated, nothing useful to "
        "add) and say what would change that.",
        fields={"reason": S("why not now"), "watch_for": S("the signal that would make it worth acting"),
                "revisit_after_hours": INT("when to look again")},
        checks=(require("reason"),), feedback=("should_act_now", "wrong_reason"), metrics={}, measure_days=0,
    ),

    # --- Tier 2: relationship work, a person approves every send -------------------------------
    ActionSpec(
        type="send_dm", tool="send_dm", title="DM an individual", tier=2, audience="person", outreach=True,
        description="Message one developer about a topic they care about, with a timely reason: they asked "
        "about this, they hit the pain we solve, they starred or forked the repo.",
        channels=("x", "linkedin", "email", "discord"),
        fields={"target_person": PERSON, "topic": S("what it's about"),
                "opening": S("the timely reason to reach out now"), "text": MESSAGE},
        checks=(known_person(), require("topic", "opening"), text_fits("message")),
        feedback=("wrong_person", "wrong_topic", "bad_timing", "already_in_touch"),
        metrics={"replied": 5, "substantive_reply": 8, "meeting": 15, "reciprocal_outreach": 6},
        measure_days=14, cooldown_days=14, cooldown_types=OUTREACH_TYPES,
    ),
    ActionSpec(
        type="invite_expert_collab", tool="invite_expert_collab", title="Invite an expert to co-create", tier=2,
        audience="person", outreach=True,
        description="Invite a maintainer or practitioner with the right expertise to make something together "
        "for a real audience need: an integration guide, example repo, livestream, or joint post.",
        channels=("email", "x", "linkedin", "github"),
        fields={"target_person": PERSON, "expertise": S("what they know that we need"),
                "audience_need": S("the audience question or gap this fills"),
                "concept": S("the piece, in one line"),
                "format": ENUM(("integration_guide", "example_repo", "livestream", "joint_thread", "blog_post",
                                "workshop"), "format"),
                "effort_ask": S("how much of their time it takes"), "text": MESSAGE},
        checks=(known_person(), require("concept", "audience_need", "effort_ask"), text_fits("message")),
        feedback=("wrong_expert", "weak_concept", "excessive_effort", "poor_timing"),
        metrics={"accepted": 10, "completed": 20, "engagement": 3, "new_followers": 5},
        measure_days=30, cooldown_days=30, cooldown_types=OUTREACH_TYPES,
    ),
    ActionSpec(
        type="propose_reciprocal_share", tool="propose_reciprocal_share", title="Propose a reciprocal share",
        tier=2, audience="person", outreach=True,
        description="Propose that a partner with an overlapping audience (an adjacent tool or creator) and we "
        "each share the other's content, built around an asset that's useful to both audiences.",
        channels=("email", "x", "linkedin", "discord"),
        fields={"target_person": PERSON, "audience_overlap": S("who both audiences have in common"),
                "our_offer": S("what we'd share of theirs"), "their_ask": S("what we'd ask them to share"),
                "text": MESSAGE},
        checks=(known_person(), require("audience_overlap", "our_offer", "their_ask"), text_fits("message")),
        feedback=("audience_mismatch", "weak_content", "uneven_value"),
        metrics={"accepted": 10, "completed": 15, "referral_visits": 3, "installs": 15},
        measure_days=21, cooldown_days=30, cooldown_types=OUTREACH_TYPES,
    ),
    ActionSpec(
        type="request_warm_intro", tool="request_warm_intro", title="Request a warm introduction", tier=2,
        audience="person", outreach=True,
        description="Ask a mutual connection (investor, advisor, friendly user) to introduce us to someone, "
        "with a short forwardable note. Only when the connection is evidenced, not guessed.",
        channels=("email", "linkedin", "x"),
        fields={"target_person": PERSON, "connector": S("who would make the intro, from the CRM"),
                "connection_evidence": S("how we know the connector knows them"),
                "purpose": S("why we want to meet them"), "text": S("forwardable intro note")},
        checks=(known_person(), known_person("connector"), require("connection_evidence", "purpose"),
                text_fits("message")),
        feedback=("weak_connection", "inappropriate_ask", "wrong_target", "overused_connector"),
        metrics={"intro_made": 10, "target_replied": 10, "meeting": 20},
        measure_days=21, cooldown_days=30, cooldown_key="connector",
    ),
    ActionSpec(
        type="shortlist_from_network", tool="shortlist_from_network", title="Shortlist from an amplifier's network",
        tier=2, audience="internal", channels=("crm",),
        description="From the people who engage with an amplifier (a big dev account, maintainer, or "
        "creator), shortlist the ones worth knowing. Goes to the CRM, not out to anyone.",
        fields={"amplifier": S("whose network this is"),
                "candidates": OBJECTS({"person": PERSON, "reason": S("why they're worth knowing"),
                                       "segment": S("audience segment"),
                                       "already_known": {"type": "boolean", "description": "in our CRM already"}},
                                      "people to add")},
        checks=(known_person("amplifier"), _shortlist_check),
        feedback=("irrelevant", "already_known", "weak_reason"),
        metrics={"qualified": 5, "conversations": 10}, measure_days=30,
    ),

    # --- Tier 3: later or occasional ----------------------------------------------------------
    ActionSpec(
        type="pitch_journalist", tool="pitch_journalist", title="Pitch a journalist or newsletter", tier=3,
        audience="person", outreach=True,
        description="Pitch a story to a journalist or dev newsletter editor whose coverage fits. The angle "
        "must be backed by evidence the team can share.",
        channels=("email", "x", "linkedin"),
        fields={"target_person": PERSON, "outlet": S("publication or newsletter"), "angle": S("the story"),
                "subject": S("email subject line"), "text": MESSAGE},
        checks=(known_person(), require("outlet", "angle", "subject", "evidence"), text_fits("message")),
        feedback=("wrong_journalist", "weak_angle", "missing_evidence", "already_pitched"),
        metrics={"interested_reply": 8, "interview_request": 15, "coverage": 25},
        measure_days=30, cooldown_days=30, cooldown_types=OUTREACH_TYPES,
    ),
    ActionSpec(
        type="pitch_podcast", tool="pitch_podcast", title="Pitch a podcast appearance", tier=3,
        audience="person", outreach=True,
        description="Pitch a spokesperson as a guest on a show whose audience and topics fit.",
        channels=("email", "x", "linkedin"),
        fields={"target_person": PERSON, "show": S("podcast name"), "spokesperson": S("who would appear"),
                "angle": S("the episode idea"), "talking_points": STRINGS("3-5 talking points"),
                "subject": S("email subject line"), "text": MESSAGE},
        checks=(known_person(), require("show", "spokesperson", "angle", "talking_points"), text_fits("message")),
        feedback=("wrong_show", "weak_angle", "guest_unavailable", "already_pitched"),
        metrics={"booked": 20, "published": 25, "inbound_interest": 5},
        measure_days=60, cooldown_days=60, cooldown_types=OUTREACH_TYPES,
    ),
    ActionSpec(
        type="make_podcast_clip", tool="make_podcast_clip", title="Turn a podcast segment into a clip", tier=3,
        description="Cut a short, source-linked clip from one of the client's recordings (mission/media.json), "
        "with captions and enough context that it can't mislead.",
        channels=("x", "linkedin", "youtube"),
        fields={"source_url": S("episode URL from mission/media.json"), "start": S("start timestamp, mm:ss"),
                "end": S("end timestamp, mm:ss"), "caption": S("on-screen caption"),
                "context_note": S("what a viewer needs to know so the clip doesn't mislead"), "text": TEXT},
        checks=(_clip_check, require("caption", "context_note"), text_fits()),
        feedback=("missing_context", "weak_opening", "unclear_takeaway", "bad_boundaries"),
        metrics={"completion": 5, "retention": 5, "source_clicks": 4, "reposts": 5, "corrections": -10},
        measure_days=7,
    ),
    ActionSpec(
        type="invite_to_roundtable", tool="invite_to_roundtable", title="Invite contacts to a roundtable", tier=3,
        audience="person", outreach=True,
        description="Bring together a small group with complementary perspectives (a community call, "
        "design-partner session, or roundtable) around a shared problem.",
        channels=("email", "discord", "linkedin", "x"),
        fields={"invitees": OBJECTS({"person": PERSON, "why": S("what they bring")}, "who to invite"),
                "topic": S("the shared problem"), "purpose": S("what the session should produce"),
                "format": ENUM(("community_call", "design_partner_session", "roundtable", "office_hours"), "format"),
                "capacity": INT("seats"), "agenda": STRINGS("agenda items"), "text": S("the invitation")},
        checks=(_roundtable_check, require("topic", "purpose", "agenda"), text_fits("message")),
        feedback=("wrong_mix", "poor_timing", "unclear_purpose", "too_big"),
        metrics={"accepted": 3, "attended": 5, "follow_ups": 5, "collaborations": 15}, measure_days=30,
    ),
    ActionSpec(
        type="send_swag", tool="send_swag", title="Send brand swag", tier=3, audience="person", outreach=True,
        channels=("mail",),
        description="Send something from mission/swag.json to someone who earned it (first merged PR, a great "
        "write-up, a long-time power user), with a short note.",
        fields={"target_person": PERSON, "item": S("item name from mission/swag.json"),
                "trigger": S("what earned it"), "text": S("the accompanying note")},
        checks=(known_person(), require("trigger"), _swag_check, text_fits("message")),
        feedback=("prohibited", "unsuitable_item", "too_early", "already_gifted", "over_budget"),
        metrics={"delivered": 1, "acknowledged": 5, "meaningful_interaction": 10},
        measure_days=30, cooldown_days=180,
    ),
]
