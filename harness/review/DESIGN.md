# Review page design

This file is for whoever changes `harness/review/` next. It says what each view is for, where its data comes from, what a verdict writes, and what is still open.

`python -m harness review` serves a React page built from `web/` into `dist/`. The build is committed, so the command needs no Node. The server is the standard library's `ThreadingHTTPServer`, bound to 127.0.0.1, with routes in `queue.py`, `tweets.py`, `history.py`, `audit.py` and `status.py`.

## What a person does here

The page has three views because the person has three jobs:

1. Decide on drafts, often on a phone during a game. That is the queue.
2. See what happened to past verdicts. That is History.
3. Label the 200-item audit set. That is Audit.

Run status takes one sentence in the header, such as "Last round 14 min ago. Watcher on." Experiments, memory and eval reports stay in files.

## The queue

### What is in it

Hot targets come first, newest post first. They are the watcher's `out/hot/<status_id>.json` files that offer a draft or a prompt for a one-liner; an abstention offers nothing and is not a card. Then come the pending drafts of rounds started in the last 24 hours, newest round first and best predicted first within a round. A card leaves the queue when its action has a verdict.

The queue leaves out three things that stay in memory:

- Follows, which have no text to judge.
- Drafts the critic vetoed.
- Drafts from mock rounds, unless the harness itself runs on the mock provider. On sam/platform's `data/harness.db` on 2026-10-16, 109 of 110 pending drafts came from mock rounds and buried the one live draft. The README's offline walkthrough still works, because there `HARNESS_PROVIDER` is `mock`.

The 24 hours is a guess with no measurement behind it. It only keeps a week of unreviewed rounds from burying today's.

### How a card reads

A card is laid out as the thread it would join: the post being answered, the person's reply box, then our draft rendered as X would render the reply. An X-style line runs down the avatar column to tie them together. The box comes before the draft because a person's own words post verbatim and read as human, and because the human arm of the watcher's protocol must not show a draft at all.

The card has one primary action. Its label says what it will send:

| State of the card | Primary action | Body sent to `POST /api/actions/<id>/review` |
| --- | --- | --- |
| Box empty, draft present | Approve draft | `{status: "approved", text: null}` |
| Box holds the person's own words | Approve your reply | `{status: "approved", text: <words>, note: TAKE_NOTE}` |
| Box holds the draft after Edit, changed | Approve edit | `{status: "approved", text: <words>, note: ""}` |
| Draft has a `[VERIFY: ...]` marker | Edit to check the fact | nothing until the person edits |
| Kill, or Skip on a one-liner | Kill draft or Skip post | `{status: "killed", reasons: [...], note}` |
| Undo on a receipt | Undo | `{status: "pending"}` |

`web/src/lib/verdict.ts` builds these bodies, and `verdict.test.ts` pins them. A take is stored like an edit, with `TAKE_NOTE` in the note, so `learn` and the hand-off read it with no new field. A kill needs at least one reason from `memory.REVIEW_REASONS`. A one-liner with no draft can only be skipped for reasons about the post: off-message, stale story, wrong target or other.

When the box has words, the draft below dims instead of disappearing, so nothing moves under the person's cursor. New cards that arrive while someone works wait behind a "Show 1 new hot target" button for the same reason. An empty queue takes them at once.

### What a verdict on a hot target writes

A hot target has no memory row until its first verdict. `hot.ensure_action` then writes one run (`sources = 'watch'`, the watcher's provider and start time, the file's profile and the account the watcher drafted for) and one action, `hot-<status_id>`. The action id comes from `hot_action_id` in `harness/outcomes/handoff.py`, the id the outcomes ledger gives the same target. The hand-off file and the hot file then describe one action, and `outcomes sync` keeps one ledger row with its arm. The status line skips these runs when it names the last round.

## History

History lists every verdict, newest first. Beside an approved one it shows what the outcomes ledger knows (`status_rows` in `harness/outcomes/sync.py`):

- A reply shows its rank among the replies to the same post ("3rd of 41 replies"), its likes, and when that was read. The rank comes from `rank_in_thread` (`harness/outcomes/rank.py`).
- A post shows its likes and when they were read.
- An approval that isn't on X yet says so: not posted, not on X yet, or not found yet.

With no outcomes at all, one line says they appear after `harness outcomes sync` runs.

## Audit

Audit reads the label store on sam/corpus-clean only through its contract (`load_schema`, `queue("audit")`, `items`, `consensus`, `append_human`). Its controls come from `data/labels/schema.json`: an enum is a set of choices, an ordinal is one connected scale, and text is a box. A schema change needs no page change.

The models' votes stay on the server until the person answers. `GET /api/audit` never sends them. `POST /api/audit/<item_id>` saves the answer with `append_human` and returns `consensus`, which the page shows beside the person's answer. The item shows no counts, because `engagement_guess` asks the person to predict them. It does show the author's follower count and the date, as the annotators saw them. `HARNESS_LABELER` names the labeler, and the OS user name is the fallback.

On a branch without the store, Audit says so and the other views work.

## Posts

`GET /api/tweet/<status_id>` returns X's syndication answer, the `Tweet` that react-tweet's components take. The request carries react-tweet's token (`tweets.token`, a port of `getToken` checked against Node). Answers are cached under `~/.cache/media-harness/tweets/` and wait in the shared per-host throttle from `harness/context/http.py`. A found post is refetched after 10 minutes, so a reply card's reply count stays current. A missing one is retried after an hour. Neither number was measured.

When X has no copy, the route returns what the harness stored about the post: the hot file's target, the label item, or a round's draft and that round's trace. `web/src/lib/tweet.ts` builds a `Tweet` from those fields, and from the brand account's profile for our drafts.

`tweet.ts` escapes `&`, `<` and `>` in every `Tweet` it builds. react-tweet's `TweetBody` writes the plain stretches of a post as HTML, because X sends them escaped. Unescaped draft or stored text would run as markup in this page, which can approve drafts.

The page draws posts with react-tweet's own parts in X's timeline layout. It leaves out Follow, like, reply and the X logo, and any count the person doesn't need at that moment. A target post shows its age, colored while it is under 10 minutes old and again until 30, and a reply target shows its reply count.

## Build and check

```sh
pnpm --dir harness/review/web install
pnpm --dir harness/review/web test     # builders, schema controls, verdict bodies, X's character count
pnpm --dir harness/review/web build    # type-checks, then writes harness/review/dist/
uv run python -m pytest tests/test_review_server.py tests/test_review_xtext_vectors.py
```

`pnpm --dir harness/review/web dev` serves the page with hot reload and sends `/api/` to a running `harness review` on port 8765. Commit `dist/` with any change under `web/src/`.

## What was cut from the last version

The last version (`origin/sam/ui-loop`, about 13,000 lines) had Queue, Loop, Experiments, Memory and Eval views. This one keeps three jobs and drops the rest:

- Loop, Experiments, Memory and Eval reports. They answer questions a person asks rarely and at a desk, and their files already say it.
- The metadata strip (type, lane, mind, story, panel score, status), the panel score and the critic's text on each card. None of them changes the decision on a hot target.
- Overruling abstentions and the "Your take" bay for `ask_human` questions. The watcher's hot path replaced the stale reply targets that made abstentions worth overruling. A post-lane question with no draft no longer gets a card; see the open questions.
- The fixture switcher and its badges, the always-visible key bar, and the flight-strip metaphor.

## Open questions

- A phone can't reach the page. The server binds 127.0.0.1 and refuses other Host headers to stop DNS rebinding. Opening it to a network needs authentication first, so this version doesn't.
- A post-lane `ask_human` question with no draft has no card. Giving it one needs a record for "skipped the question", which memory has no place for.
- Two rounds that draft for one target show two cards.
- The 24-hour window, the 30-second poll and both cache lifetimes are unmeasured.
