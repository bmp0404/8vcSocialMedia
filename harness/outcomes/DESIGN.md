# Outcomes design

This package answers two questions about every action we approve. Did 8VC's bot post it? If it did, how did the post do compared with the account's usual post? The answers go into memory's `outcomes` table, which `learn` already reads. Until this branch nothing wrote to that table automatically, so every learning step ran on proxies.

## What exists now

- `harness/memory.py:15` defines `outcomes (action_id PRIMARY KEY, recorded_at, metrics)`, so it holds one row per action. `record_outcomes` (`:49`) writes with `INSERT OR REPLACE`. `scored_history` (`:127`) joins it to `actions`, and `learn` (`harness/learn.py:22`) reads that join. The table had 0 rows after the 2026-10-15 demo.
- The only writer is `harness record <file>` (`harness/cli.py:51`), which stores a JSON list someone builds by hand.
- `export_approved` (`harness/review/__init__.py:17`) writes `out/approved-run<id>.json`. Each action carries its `id`, `type`, `text`, `target_url` and `approved_at`, plus `original_text` when a reviewer edited it (`memory.approved_actions`, `harness/memory.py:113`).
- `[brand] x_handle` in the profile's `observe.toml` (`mission/profiles/<name>/`) names the account `sync` reads: `_sgu_`, Sam's alt for testing. `HARNESS_ACCOUNT_HANDLE` overrides it (`harness/sources/watch.py` `account_handle`). The profile's `reply_framing.json` holds a different handle, the one reply drafts are written under, and `sync` never reads it.

## What other branches expect

- Memory (`sam/memory`, PR #21). `run_episodes` (`harness/learning/reviews.py:119`) gives real metrics a label only when `metrics["outcome"]` is `success` or `failure` (`:143`), and real metrics outrank a reviewer's verdict. Its within-account definition (`AUTHOR_PRIOR_DEFINITION`, `harness/learning/dataset.py:178`) counts a post above the account's median of earlier posts as a success, and a post at or below it as a failure. Its open question 6 asks who decides success for our own posts.
- Eval (`sam/eval`). `FaultEvidence` (`harness/evals/fault.py:38`) needs our engagement plus the account's median, 10th and 90th percentiles and baseline size "for the same action type and window".
- Context (`sam/context-dynamics`, PR #18). `XReader` (`harness/context/xreader.py:110`) reads statuses and timelines through fxtwitter under a shared 1 request per second limit. Its `Tracker` (`harness/context/tracker.py:66`) polls posts on a log-spaced schedule. `data/context/DESIGN.md:83` treats a post as mature at 48 hours.

## Gaps this branch fills

- Nothing linked an approved action to the status id the bot posted.
- Nothing read our own posts' counts after posting.
- Our own posts had no label that memory would accept.
- An action the bot never posted left no trace, though a skip is the plainest signal of what the bot accepts.

## Design

### Matching an action to its post

`sync` reads the account's timeline with `XReader.profile_statuses(handle, limit, with_replies=True)`. X's Posts tab, the default, leaves replies out: on @CClarkReport on 2026-10-16, its replies to other accounts appeared only with the flag. The replies feed also shows each post the account answered, by its own author, which the reader marks `timeline_context`. `reader.py` drops those posts, reposts, and posts repeated across pages.

`match.py` normalizes both texts. It strips links and `[VERIFY: ...]` gaps, folds case and curly quotes, and for a reply action also strips the leading @mentions that X adds as the reply prefix. A mention keeps its @handle, because the handle is what tells two mention drafts apart. It then compares the texts with `difflib.SequenceMatcher`. A post is a candidate only if it was made between the approval, less 5 minutes of clock skew, and 7 days after it. Thresholds:

| Case | Rule | Why |
| --- | --- | --- |
| Reply whose post replies to `target_url`, or quote whose post quotes it | similarity at least 0.5 | X itself ties the post to the target, so the text only has to tell two replies to one target apart |
| Anything else | similarity at least 0.8 | the text is all there is |
| Unanchored text under 20 normalized characters | exact, except that each `[VERIFY]` gap matches up to 40 characters | "this team 🫶" scores 0.91 against "this team 💛" |

None of these numbers is calibrated, because the bot hasn't edited a real draft yet. Matches are one-to-one: anchored candidates go first, then higher scores, and on a tie the earlier approval takes the earlier post.

`reader.py` sets a post's `kind` from `replying_to`. On @CClarkReport post 2106410826999500938, fxtwitter dropped `replying_to` from a reply, so that post reads as an original. A reply action that lands on such a post loses its anchor and needs the 0.8 text match.

### Checkpoints

A snapshot taken at age `a` belongs to checkpoint T when `T <= a < 2T`: +1h covers 1 to 2 hours, +6h 6 to 12, +24h 24 to 48, and +48h everything from 48 hours on. The +48h window has no end because the context worker treats a post as settled at 48 hours. A checkpoint is due when the post's age is inside its window and the ledger has no snapshot for it. `sync` then calls `XReader.status` once. A sync that runs after a window closes records that checkpoint as missed rather than filing a late count under it. Reads stop 7 days after posting, so a deleted post fails loudly for at most a week and then stops being fetched. The factor of 2 has no evidence behind it.

### Labels

Each snapshot gets a label from `label.py`:

- `success`: engagement (likes + reposts + replies + quotes) above the median of the account's earlier posts at the same checkpoint.
- `failure`: at or below that median, matching memory's definition.
- `insufficient_baseline`: fewer than 10 earlier posts at that checkpoint.
- `not_posted`: see the next section.

The baseline comes from the timeline reads. Every read stores each own post's counts under the checkpoint its age falls in, keeping the first reading in each window. Replies are compared only with replies, and posts and quotes with each other. Thread continuations (self-replies) are left out, as the context worker does. The baseline uses at most the 30 most recent earlier posts, so the norm follows the account as it grows. That cap is a guess. A success or failure label is final. `insufficient_baseline` is recomputed on every sync.

For an established account, +48h has a baseline from the first read, because every older post on the page is already past 48 hours. The earlier checkpoints only fill as sync keeps running: a post's +1h reading exists only if a read happened while it was 1 to 2 hours old. A new account will show `insufficient_baseline` for a while, which is the honest answer.

The +48h baseline is biased toward older posts. A post read at 300 hours has had longer to collect engagement than one read at 49 hours, so the bar for success is slightly higher than "at 48 hours". On the dev-tool dataset the context worker saw mature posts' counts barely move over an hour (median change 0, `data/context/DESIGN.md:23`), but that is one hour on one dataset, not a growth curve.

### Not posted

An unmatched action becomes `not_posted` when a timeline read happens 24 hours or more after approval and that read reaches back past the approval time. Without the second condition, a busy account could push the post past the read limit and sync would call a posted action skipped. When no read covers the approval, `status` says `unverified` and suggests a larger `--limit`. An unverified action keeps the timeline due on every sync until a read covers it.

How far a read reached is the oldest of the account's own posts in it, with two exceptions:

- A post that something in the feed replies to doesn't count. The feed shows a reply's parent right above the reply, whatever the parent's age, so an old self-thread parent would make the read look weeks deep.
- An empty feed reaches nothing. A feed that ends early doesn't count as reaching the account's first post either, because one empty page from fxtwitter looks the same. As a result, an account whose whole history is newer than an approval leaves that action `unverified`, never `not_posted`.

The clock starts at an action's first approval. `memory.set_review` restamps `approved_at` whenever a reviewer re-saves, and the bot may have posted in between. A post made up to 7 days after approval still matches, so a late post replaces `not_posted` with real numbers.

### The ledger

`ledger.py` keeps a separate SQLite file, `data/outcomes.db` by default, which `data/*.db` already gitignores. It isn't in `data/harness.db`, because Memory owns that schema and teammates delete the file after schema changes. Tables:

- `actions`: every approved action seen, with its first approval time and `not_posted_at`.
- `posts`: the account's own posts from timeline reads.
- `matches`: action id to status id, one each way (`UNIQUE`).
- `snapshots`: our matched posts, from `XReader.status`, one per checkpoint.
- `baseline`: every own post, from the timeline, one per checkpoint.
- `reads`: each timeline read and how far it reached.
- `labels`, and `meta`, which holds the handle the ledger belongs to.

Primary keys make a rerun harmless: a second insert of the same snapshot is ignored. A sync reads the timeline only when an action is still open and the last read is at least 30 minutes old. That gap is half of the +1h window, so syncing every half hour reads each new post inside that window. Before recording, `sync` compares each payload with what `outcomes` already holds and writes only the ones that changed. If someone deletes `data/harness.db`, the next sync writes the rows again. A ledger refuses a second handle, so a stand-in account can't leak into the brand's baseline.

### What memory receives

One row per action. The top level describes the latest checkpoint that has a success or failure label, or else the latest snapshot. `history` keeps every checkpoint, because the table holds one row per action. Real output from the @IowaWBB dry run:

```json
{"checkpoint": "+48h", "age_hours": 58.89, "likes": 220, "reposts": 12, "replies": 4, "quotes": 0,
 "views": 4635, "author_followers": 122162, "engagement": 236, "outcome": "failure",
 "baseline_n": 30, "baseline_median": 474.5, "baseline_p10": 229.1, "baseline_p90": 1728.7,
 "status_id": "2110064336911597749", "url": "https://x.com/IowaWBB/status/2110064336911597749",
 "posted_at": "2026-10-13T17:45:15+00:00", "approved_at": "2026-10-13T17:15:15+00:00", "history": [...]}
```

A skipped action records `{"outcome": "not_posted", "approved_at": ..., "checked_at": ...}`. A post found before its first checkpoint records `"outcome": "posted"` with its status id and an empty `history`, so a late post replaces `not_posted` as soon as sync sees it. Memory's `run_episodes` labels only `success` and `failure` and leaves `posted`, `not_posted` and `insufficient_baseline` unlabeled. That is right, because none of them judges the draft. The count names follow `x_common.XPost.metrics()` on `main`, not the X API's `like_count` names that memory's `Outcome` comment mentions.

### Alternatives ruled out

- Matching on text alone. A bot that rewrites a reply would turn it into `not_posted`. The anchor on `replying_to_status` or `quoted_id` comes from X, so it costs nothing to trust.
- Finding replies through `XReader.conversation` on the target post. That endpoint returns about 35 replies, ranked by likes or recency. A two-follower account's reply sinks under the likes ranking and drops off the recency page on a busy thread.
- Reusing `Tracker`. It tracks posts, not actions, and has no matching or labels. Its 10-poll schedule doesn't line up with these four checkpoints. Registering our matched posts with it for dense curves would still be useful once PR #18 merges.
- Reading baseline counts with one `status` call per post. One timeline page gives about 20 posts' counts in a single request.

## Open questions

1. What is the brand's real handle? `sync` reads `_sgu_` until `[brand] x_handle`, `HARNESS_ACCOUNT_HANDLE` or `--handle` names another, and refuses when all three are empty.
2. Can 8VC's bot report the status id it posted for each action id? If it can, the ledger's `matches` table takes those ids directly, and text matching becomes a fallback.
3. fxtwitter sometimes drops `replying_to` from a reply (see Matching). X's `display_text_range` still marks the reply prefix, and `XPost` could expose it. That would keep such a post's anchor and put it in the reply baseline.
4. Follows aren't tracked. fxtwitter's `/2/profile/{handle}/following` could confirm them, but nothing here reads it yet.
5. Who runs `sync` every 30 minutes? It belongs in Dhilan's engine loop next to `learn`.
6. Memory's table holds one row per action, so per-checkpoint numbers live inside `history`. If reflection wants one episode outcome per checkpoint, memory has to read `history` or change the schema.
7. A reviewer can withdraw an approval after the bot has read the file. `sync` still tracks everything it has ever seen in an approved file, so a withdrawn and unposted action reads as `not_posted`.
8. Every threshold here is a guess with no evidence behind it: `TEXT_MIN`, `ANCHORED_MIN`, `SHORT_TEXT`, `WINDOW_FACTOR`, `BASELINE_MAX`, `TRACK_FOR` and `TIMELINE_EVERY`. The first real bot posts will show whether the matching thresholds are too strict.

## What this branch adds

- `harness/outcomes/`: `handoff.py` (approved files and handle), `reader.py` (fxtwitter adapter), `match.py`, `label.py`, `ledger.py`, `sync.py` and `cli.py`.
- `harness/cli.py`: one additive block that registers `harness outcomes sync` and `harness outcomes status`.
- `tests/test_outcomes.py`: 43 offline tests for matching, checkpoint windows, labels, `not_posted` timing and coverage, the replies feed filter, rerun idempotency, the handle check and the missing or outdated reader.
- Two live stand-in runs on 2026-10-16 against public accounts, not the brand's. With @IowaWBB, 9 actions produced 7 matches, 1 `not_posted`, 1 waiting and 3 `failure` labels at +48h, using 1 timeline read and 6 status reads. The rerun read nothing. With @greptile, 2 replies and 1 quote matched on their targets, one of them a rewrite at similarity 0.71 that text alone would have rejected. The PR description has the full output.
