# Watcher design

This is for whoever runs `harness watch`, posts from `_sgu_` by hand, or decides what to build on the threads it collects. It answers four questions. How does a fresh post become a draft while the thread is young? What does the watcher write, and where? What does the operator do with a hot target, and how is the result measured? What did the first live run show?

## Why it exists

The reply lane abstained on almost everything because its targets were 90 minutes to 2 days old. Top replies land a median 28 minutes after the post, 53 of 72 within an hour (`action/DESIGN.md`). Observe runs in batches, so a post is usually old by the time a run sees it. The watcher reads the watched accounts continuously and drafts for a post while it is under 10 minutes old. The same reads also feed a benchmark: every watched thread, read again at +1h and +24h, becomes a time-stamped item, so the reply benchmark grows on its own and a later batch can confirm a change only on threads collected after it was proposed.

## How it runs

`python -m harness watch` is one process for both profiles (`wnba` and `devtools`; `--profiles` picks others). Its main thread polls each handle on the watch lists with `XReader.profile_statuses`, one fxtwitter request per poll. Each handle comes round once per `--interval` (120 seconds by default). The handles are spread evenly over the interval with the two profiles interleaved, so 46 handles make a steady 0.38 timeline requests per second, about 0.2 per list. Every request goes through the shared throttle in `harness/context/http.py`, one request per second across every process on the Mac, and skips the disk cache, because the watcher needs live counts.

Between polls the main thread runs at most one due ingestion job every 15 seconds (three requests, so at most 0.2 requests per second) and, every 30 minutes, `harness outcomes sync`. One worker thread drafts hot targets, one at a time, so a slow LLM call never delays a poll.

A failed read on one handle is logged to `out/watch/watch.log` and skips that handle for the cycle. A handle that fails twice in a row waits 2 intervals, then 4, then 8, because a failed poll spends up to five requests in retries. Nothing one account does stops the watcher. SIGTERM or SIGINT stops it after the hot job in flight; `python -m harness watch stop` sends SIGTERM to the pid in `out/watch/watch.pid`.

### What counts as a watched post

A profile's watch list is the `[reply_targets]` section of its `observe.toml`, the same list Observe ranks reply targets from. `accounts` always count. `broad_accounts` count only when the text contains one of `keywords`. Only the account's own original posts count: replies and reposts are skipped, so every watched post is the root of its own thread.

The WNBA list is the 11 accounts and 11 broad accounts already in `mission/profiles/wnba/observe.toml`. The dev-tools list is 24 accounts chosen on 2026-10-16 by how often each posts (`timeline_posts_per_day` in `data/context/accounts.csv`) and how open its threads are to small accounts: across its threads in the corpus reply samples (sam/corpus), the share of the three most-liked replies written by accounts under 1,000 followers, the measure in `harness/rank/target.py`. The rule, the numbers per account and the accounts left out are in the comment above the list in `mission/profiles/devtools/observe.toml`. Together the 24 posted about 25 original posts a day in the dataset window. Openness was measured on at most 22 threads per account, and on 1 or 2 for seven of them, so the ranking is rough.

### Restarts

`out/watch/state.db` remembers every post the watcher has seen, every hot job and every ingestion job, each keyed by status ID (and checkpoint), so a restart that reads the same timelines again queues nothing twice. On start, a hot job a crash left running counts as done if its file exists and goes back to the queue if not; an ingestion job left running goes back to pending. A queued hot job whose post has passed the lane's own 30-minute cutoff (`ActionConfig.reply_max_age_minutes`) by the time the worker reaches it is marked stale instead of drafted.

## The hot path

A post under 10 minutes old when a poll read it is a hot target. The worker runs only the reply lane, for that one post, and writes `out/hot/<status_id>.json`.

| Step | Code | Change for the hot path |
| --- | --- | --- |
| Plan: reply or abstain, and with what intent | `lanes.plan_targets` | None. One target instead of a batch. |
| Read the thread's real replies | `lanes.answer_target` through its `fetch` argument | The thread is read through the watcher's XReader, inside the shared throttle, instead of `thread.fetch_replies`, which keeps its own per-process limit. |
| Drop bot-like replies from the page, write, gate | `lanes.answer_target` | The arm forces the writer by setting `min_page_replies` on a copy of `ActionConfig`: 0 always continues the page, `page_replies + 1` never does. `answer_target` reads only that knob to choose. |
| Veto in two orders | `veto.veto` | The copy sets `veto_parallel_orders`, a new `ActionConfig` field (default off) that runs the forward and reversed passes at once. Same two calls, same verdicts. |

Orient is skipped, so there is no narrative. The facts block the gate and veto read is the mission plus the post itself. The hot path never reads the run budget: the operator decides what to post. A rival thread under `rival_thread_mode = "banter"` gets the banter writer whatever the arm; the file's `method` says what actually ran.

The file holds the target, the plan, the arm and its seed, the offer (`draft`, `human_prompt` or `abstain`), the draft or the abstention reason, every candidate with its gate findings and veto verdicts, and timings: plan, draft, total, detection to finish, and the post's age at detection and at finish. `models` lists every LLM call with the model requested, the model the gateway reported and its seconds. The lane builds its own Anthropic clients and none returns the response, so `modelrec.py` wraps `anthropic`'s `Messages.create` once in the watcher process to read `response.model`.

## Thread ingestion

Each watched post gets checkpoints at +1h and +24h. Checkpoint T runs inside [T, 2T) after posting, the rule `harness/outcomes/label.py` uses. A post first seen after a window closed never gets that checkpoint (`missed`), because a late read filed as +1h would carry likes the thread had not earned by then. A read takes the first page of replies in both of X's sort orders and the post itself, the reads `FxTwitterReader.replies` makes. Coverage is unique replies over the post's reply counter. A thread is kept at coverage 0.8 or more, the corpus benchmark's high-coverage line; below it the middle of the thread is missing and losers skew late.

Kept threads are appended to `data/threads/` in the five files `harness.corpus.load` (sam/corpus) requires, so `load(Path("data/threads"))` reads the directory as it is:

- `author_timelines.jsonl.gz`: the post, an `XPost.to_dict()` row, since it came off the account's own timeline. It adds `checkpoint`, `watch_profile`, `coverage`, `reply_count`, `sampled` and `age_minutes_at_read`.
- `reply_samples.jsonl.gz`: each reply, an `XPost.to_dict()` row with `reply_to_post` and `coverage` like the context worker's reply samples, plus `checkpoint`, `watch_profile`, `minutes_after_parent` and `minutes_after_post` from the Snowflake IDs. Each row keeps the reply's likes and its author's follower count. Repliers' bios are dropped: nothing reads them, and the repository is public.
- `x_metrics.jsonl.gz`, `boost_flags.csv.gz` and `timeline_posts.csv.gz` exist empty, because the loader requires them and the watcher has no dataset rows or flags.

Each append is one more gzip member, and Python's gzip reads a multi-member file as one stream. The same post read at +1h and +24h appears twice; the loader merges rows by status ID and keeps the later counts, as it does for the context worker's repeated reads. A crash between appending and marking the job done can append a thread twice, which the same merge absorbs. I ran the sam/corpus loader, unedited, over a directory these functions wrote: it read the post, its direct replies with `thread_root` and `thread_coverage`, and a nested reply with its own parent. `test_the_corpus_loader_reads_the_threads_directory` repeats that check once sam/corpus is merged; until then it skips.

## The `_sgu_` protocol

The operator posts by hand from `_sgu_`; the harness never posts. For each hot target a seeded coin (`arms.py`) picks one arm, so outcomes can compare arms on targets assigned at random:

- `continuation`: the continuation writer, forced on however few replies the thread has.
- `human`: no draft. The file's `prompt` asks the operator for a one-liner of their own. No draft is shown, so none anchors it. The plan still runs, so an off-mission post is abstained on in every arm.
- `styled`: the styled-prompt fallback, forced on however many replies the thread has.

The coin is sha256 of `<seed>:<status_id>`, so a post always gets the same arm, on any machine and after any restart; `--arm-seed` starts a new assignment.

`harness outcomes sync --hot-dir out/hot` (the default, and the watcher runs it every 30 minutes) loads each hot file that offered a draft or a prompt as a reply action that matches only a reply to that status, whatever its text, because the operator may rewrite the draft or write their own. At match time it logs the arm, the minutes between the parent and the reply and `_sgu_`'s follower count. At +1h and +24h it records `rank_in_thread` from the parent's likes-ordered conversation and labels the reply by its rank percentile, never against `_sgu_`'s median, which for a new account is 0. `harness/outcomes/DESIGN.md` has the rule. Hot targets have no row in memory's `actions` table, so their outcomes stay in `data/outcomes.db`; `harness outcomes status --json` lists them with arm and rank.

What the operator does: open the newest `out/hot/*.json`, post the draft (edited or not) or a one-liner of their own as a reply from `_sgu_` within minutes, and leave the watcher running. Answering only some hot targets is fine. Answering only the ones whose drafts look good is not, for the arm comparison: that picks targets by draft quality, which the coin exists to prevent. Answer a hot target whatever its arm, or record why not.

## Commands

```sh
HARNESS_PROVIDER=anthropic uv run python -m harness watch  # until stopped; --duration 1800 stops after 30 min
uv run python -m harness watch status                      # running or not, and the counts in state.db
uv run python -m harness watch stop                        # SIGTERM, then wait for the hot job in flight
uv run python -m harness outcomes sync                     # match _sgu_'s replies, rank them; the watcher also runs it
```

Without `HARNESS_PROVIDER=anthropic` (in the environment or `.env`) the hot path drafts with the mock provider. `--provider anthropic` also drafts for real, but the memory store reads `config.PROVIDER`, so under it each profile's lessons come from a throwaway store instead of `memory/store/<profile>/`. The three live runs below used `--provider anthropic`.

## First live runs

On 2026-10-16 the watcher ran four times between 09:32 and 12:15 UTC, 5:32 to 8:15 in the morning in New York. The second run lasted 60 minutes and the fourth, on the merged code, 68; the first and third were stopped after 12 minutes, to change code and to merge sam/platform. The fourth was stopped when free disk fell under 2 GB (other runs' swap), the rule this batch ran under.

| | Run 2, 09:44 to 10:44 | Run 4, 11:07 to 12:15 |
| --- | --- | --- |
| Handles polled successfully | 45 of 46 | 45 of 46 |
| Polls | 1,343, 1,218 succeeded (91%) | 1,468, 1,303 succeeded (89%) |
| Failed polls | 122 fxtwitter 404s, 3 HTTP 500s, over 44 handles | 163 404s, 2 HTTP 500s, over 46 handles |
| Requests | 1,781, 0.49 per second | 1,956, 0.48 per second |
| Timeline requests per second | devtools 0.26, wnba 0.23 | devtools 0.26, wnba 0.23 |
| New watched posts | 0 | 2, both under 10 minutes old |
| Hot targets | 0 | 2, each file written within 6 s of detection |

`@ChantelJennings` is a protected account, so fxtwitter can't read its timeline; the backoff cut it to 6 or 7 polls an hour. Every other failing handle succeeded on a later cycle. The timeline rate is above the planned 0.2 per list because xreader retries every 404 once. Until 12:10 no watched account had posted an original since 03:18 UTC; fxtwitter itself was live, since @Reuters showed a post 3 minutes old.

The two hot targets:

| Post | Seen at | Arm | Result | Plan | File written at |
| --- | --- | --- | --- | --- | --- |
| @HerHoopStats 2111067014940803107, wnba | 77 s old | styled | plan abstained, `no_angle`: the post names neither player nor team | 3.0 s | 81 s old |
| @Netlify 2111067657877487647, devtools | 54 s old | styled | plan abstained, `off_mission`: a welcome to new web-hosting sign-ups, nothing on coding agents | 5.3 s | 60 s old |

Thread ingestion over all four runs read 39 threads: 23 kept at coverage 0.8 or more, 12 below it, 4 with no replies. All were +24h reads of posts 24 to 48 hours old at the first poll; the two hot posts' +1h reads were still pending when the watcher stopped. `data/threads/` holds the 23 kept threads in 60 KB.

Neither hot target reached the writer, so the draft path's timing comes from runs of `hot.run_hot` with the real gateway on two posts from the watch lists, 11 hours old, written to a scratch directory instead of `out/hot/`:

| Post | Arm | Result | Plan | Draft steps | Total |
| --- | --- | --- | --- | --- | --- |
| supabase 2110857277494169647, 42 replies | continuation | draft: "is the local runtime persistent or is this session only" | 3.0 s | 27.9 s | 31.0 s |
| same | styled | draft: "been waiting for this. agent A drops a table while agent B is mid-migration is the kind of bug that makes you mass delete your logs" | 3.4 s | 35.7 s | 39.1 s |
| WNBA 2110844208961708245, 7 replies | continuation, styled | plan abstained (`no_angle`: the post doesn't say who made the All-Defensive teams) | 4.7 s, 5.9 s | none | 4.7 s, 5.9 s |

Before `veto_parallel_orders`, the styled draft took 55.1 s, 24 s of it in the two sequential veto passes, and a continuation draft took 32.9 s and was vetoed. The largest remaining step is the page bot filter, one Opus 5.5 call over the page's replies: 14 to 16 s in every draft. Every call reported the model it requested: `claude-sonnet-5-5` for the plan, `claude-opus-5-5` for the bot filter and the veto, `claude-opus-4-5-20251101` for both writers. Each draft made two fxtwitter requests: the post and its conversation.

## Not verified, and open questions

- No hot target has been answered from `_sgu_`, so matching a hot target and ranking a real reply have run only against fakes.
- Both live hot targets ended at the plan, so the writer, gate and veto have run on a fresh post only with the mock provider; with the real gateway they ran on 11-hour-old posts.
- No +1h read of a post first seen fresh has run live; every live ingestion was a +24h read.
- The 48-hour maturity rule in sam/corpus (`MATURE_AFTER`) excludes every item collected here from the current reply benchmark, because the last read is at +24h. Either add a +48h checkpoint or let the benchmark use the +24h counts; the brief asked for +1h and +24h, so this is for the lead.
- `data/threads/` holds replies by individual X users, with handles and follower counts. Decide before committing it to the public repository; the corpus snapshot raised the same question.
- `success` at rank percentile 0.5 and `insufficient_thread` below 5 replies are guesses.
- The 10-minute cutoff is the brief's. Nothing measured where a reply's chance of reaching the top falls off.
