# Observe and orient

This doc is for teammates deciding what to build next in the observe stage. It covers what observe reads, how orient turns that into ranked narratives, where reply targets come from, what is still missing, and what was ruled out. Everything here describes branch `sam/observe` as of 2026-10-15.

## What exists now

Observe is a set of source plugins behind one function. `observe(names, mission)` in `harness/sources/__init__.py:30` runs each named source, prints and skips any that raise, and returns a flat list of `Signal`. Orient (`harness/orient.py:270`) sends those signals to the LLM to cluster and score for relevance, then measures velocity from timestamps and counts itself.

### Sources

Every source reads its brand-specific settings from the active mission profile's `observe.toml` (`mission/profiles/<name>/`). `HARNESS_PROFILE=devtools` switches the same code to a dev-tool brand. The loader is `harness/sources/watch.py:102`, and it rejects unknown keys so a typo fails instead of silently doing nothing.

| Name | Reads | Key | Signal kind | Counts |
| --- | --- | --- | --- | --- |
| `espn_news` | ESPN site API league and team news | none | `news` | none |
| `espn_scores` | ESPN scoreboard, plus odds from `summary?event=` for games within 48 h | none | `schedule` | scores, live and final flags, `starts_at_unix` |
| `espn_injuries` | ESPN league injury report | none | `news` | none |
| `google_news` | Google News RSS search, sorted by date before the per-query cut, links resolved to the publisher | none | `news` | `url_resolved` |
| `bluesky` | Bluesky public AppView search, top posts of the last 24 h | none | `social` | likes, reposts, replies, quotes |
| `x_targets` | fresh posts from watched X accounts, plus X search when a key exists | none for timelines | `reply_target` | likes, reposts, replies, quotes, views, followers |
| `x` | X search for the `[x]` queries | X or xAI key | `post` | as the backend reports |
| `x_brand` | the brand's own posts and posts that mention it | handle, plus a key for mentions | `own_post`, `mention` | as above |
| `attention` | the context worker's attention series, as a spike ratio | none | `attention` | `spike_ratio`, `level`, `baseline` |
| `agent` | whichever of the above an LLM picks for this round, with its own queries | as the feeds need | as the feeds give | as the feeds give |

Google News links are `news.google.com/rss/articles/<id>` redirects, and qualify's critic vetoed captions that used them. Following the redirect doesn't reach the publisher: it 302s to itself and serves a page whose script looks the URL up. `Resolver` in `harness/sources/google_news.py` makes the same two requests the page does (the page, for an id, timestamp and signature, then Google's `batchexecute` endpoint), caches each answer in `out/gnews_urls.json`, and resolves at most 25 new links a run (`gnews_resolve_max`), newest first. That took 29 s for 25 links on 2026-10-15. A link left unresolved keeps the Google URL with `url_resolved: False`, and the resolver stops for the run after its first failure, since Google rate-limits in bursts. The endpoint is undocumented and can change without notice; when it does, links come back unresolved rather than wrong.

All five keyless sources answered live on 2026-10-15: 52 ESPN articles, 1 game with odds, 49 injury entries, 52 Google News items and 66 Bluesky posts in one run.

### X access

The X sources never talk to X directly. They ask `get_backend(capability)` in `harness/sources/x_common.py:111` for the first backend whose credential is present:

- **X API v2** (`x_api.py`), switched on by `X_BEARER_TOKEN`. Search and timelines, counts from X. It costs $0.005 per post read, so one `x_targets` run over 13 accounts and 4 queries reads about 230 posts, roughly $1.15. Set a spending cap in the X console before anyone loops it.
- **Grok `x_search`** (`x_grok.py`), switched on by `XAI_API_KEY`. Search and timelines through xAI's Responses API at $5 per 1,000 posts plus tokens. Only posts whose status id appears in Grok's citations survive, and their counts are the model's reading, so each comes back with `metrics_verified=False`. `verify_counts` in `x_fx.py:88` replaces those counts with fxtwitter's reading of the same post when fxtwitter is available.
- **fxtwitter timelines** (`x_fx.py`), no key. A third-party read-only proxy, reached through the context worker's `FxTwitterReader`. It has no search. On a 404 the backend waits 2 s and retries once, then checks the profile to tell a suspended account from fxtwitter flaking; 3 of 13 real timelines 404'd once on 2026-10-15 and worked on retry.

`HARNESS_X_BACKEND=api|grok|fx` forces one. Without a key tonight, `x_targets` runs on fxtwitter timelines and `x` raises with the env vars that would switch it on.

### Reply targets

A reply from an account the viewer doesn't follow never reaches For You. X's home mixer drops out-of-network replies (`OONRetweetReplyFilter` in xai-org/x-algorithm at `92f0918`). So a two-follower account's reply is seen only inside the parent's thread, and the parent's live traffic is the whole opportunity. `score_target` in `harness/sources/reply_targets.py:87` multiplies four factors:

- **Traffic.** Views per hour when the backend reports views, else engagements per hour. The rate comes from the delta against the previous run's snapshot in `out/x_snapshots.json` when one exists, else the average since posting.
- **Freshness.** `0.5 ** (age / 60 min)`. Posts older than 90 minutes are dropped.
- **Crowding.** `(1 + replies) ** -0.5`, because each reply already there pushes ours down and the first few cost the most.
- **Author size.** 1.0 for 10k to 250k followers, 0.5 for 5k to 10k, and 0 otherwise, except a post from an account above 250k still scores 0.3 inside its first 10 minutes with fewer than 50 replies. X runs an LLM reply-quality check on replies under 250k+ accounts and can label a low-credibility reply `RiskyHighVizReply` (`grox/flows/reply_spam` in the same repo). A new account has near-zero credibility.

A post with 0 replies despite more than 50 likes after 15 minutes probably has replies restricted, which fxtwitter can't confirm, so its score is multiplied by 0.1 and it carries `replies_possibly_restricted`. Tonight's only target, a @valkyries post with 263 likes and no replies at 75 minutes, prompted the rule; the cutoffs are uncalibrated.

`rank_targets` drops replies, so every target is the root of its own conversation, and keeps at most two targets per author because several fast replies to one account look like reply spam. Each emitted target also carries `thread`, its 15 most-liked direct replies, so the reply minds see the thread they would join. `observe()` reads `x_targets` after every other source, since `attention` can spend minutes in GDELT backoff and the targets would age while it waits. Broad outlets such as SportsCenter count only when a post matches a keyword, since most of what they post is other sports.

### Orient

Orient builds the prompt to a character budget instead of slicing a string. The old code cut its JSON at 60,000 characters, and ESPN plus Google News alone reached 58,828, so reply targets fell off the end. `order_signals` (`harness/orient.py:172`) puts reply targets and mentions first, drops items older than 72 hours, leaves out the brand's own posts, and interleaves the remaining sources so 60 headlines can't crowd out 5 posts. `fit_payload` then adds rows until 45,000 characters and logs what it dropped. The LLM cites evidence by signal id, and code maps ids back to URLs, so a URL in a narrative is always one a source returned.

Velocity is measured, not guessed, whenever signals have timestamps:

1. `signal_heat` gives each signal a 0 to 1 heat. X and Bluesky posts use engagement per hour on a log scale that saturates at 10,000 an hour. News decays with a 12-hour half-life from 0.6. A live game is 1.0, and a scheduled one warms as tip-off nears. An attention spike scores 0.5 at twice its median and 1.0 at four times.
2. `member_heats` keeps one heat per distinct author or publisher and collapses copied text. Raw item counts measure bots: the bursts the research run found on 2026-10-15 were one account posting the same link six times.
3. `narrative_velocity` is `log1p(total heat) / log1p(5)`, capped at 1, so one hot item counts and many lukewarm ones count more.

`velocity_basis` says `data` when this happened and `llm` when no member had a timestamp, as with the sample fixtures. Measured velocity is absolute. A single run has no history of the same narrative to compare against.

Every age, the 72-hour staleness cut and velocity are measured from `orient(..., now=)`. A live run leaves it out. A replay or backtest passes the replay cutoff, or orient would measure from today and drop every historical signal as stale. If `now` is left out and every timestamped signal is past the cut, `replay_now` measures from the newest signal instead and logs that it did, so a forgotten `now=` costs accuracy, not the whole input.

`Narrative.reply_targets` holds only X status URLs, fastest first, with `created_at` and the raw counts so a UI can draw the post. X URLs also lead `signal_urls`, because main's mock `decide` replies to `signal_urls[0]`. Reply targets no narrative cited become a `reply-lane` narrative, ranked like the rest so a hot lane can win one of the two slots `decide` drafts for.

### Knobs

Every tunable number lives in `ObserveKnobs` (`harness/sources/knobs.py`), with today's values as defaults and the reason for each. `orient()` takes `knobs=`, and `HARNESS_OBSERVE_KNOBS` can point at a JSON file of overrides. None of the values is calibrated against outcomes. They are judgment, or they come from X's ranking code where a comment says so. Brand-level settings, such as queries, accounts, the 90-minute target window and the target count, stay in the TOML.

## What other people are building

- **Context worker, `sam/context-dynamics`.** Owns `harness/context/`: `FxTwitterReader` and the attention collectors. Observe imports both lazily (`x_fx.py`, `attention.py`) and raises a named error until that branch merges. The reader keeps one request per second per host across processes, and both workers share that budget because they run from Sam's IP.
- **Action, Rushi's `decide()` on main.** Drafts for the top two narratives plus one random lower one. The action worker was asked to take reply `target_url`s only from `reply_targets`.
- **Dhilan's `engine/`.** Calls `observe()` and `orient()` directly. If it polls `x_targets` every few minutes, target velocity comes from snapshot deltas.
- **Aarav's `aarav/self-improving`.** Adds `harness/sources/devtools.py` and `harness/sources/hackernews.py`, retargets the mission to Coordigent, and caps orient's output at `top * 2` narratives with one-sentence summaries. This branch takes the same output cap, as `narrative_overdraw`, and keeps sources config-driven so his brand would be one more TOML file. His `orient.py` and `sources/__init__.py` edits will conflict with this branch.

## Gaps

- **No keyless X search.** fxtwitter's `/2/search` returned 404 four times out of four. Until someone has an X or xAI key, targets come only from watched timelines.
- **The brand's handle is unknown**, so `x_brand` raises. The fan account and a dev-tool brand both have a TOML; nobody has confirmed which brand the harness serves.
- **The watch list is a first guess.** Most of tonight's accounts are above 250k followers and so score 0 after their first 10 minutes. Beat writers in the 10k to 250k band need picking by someone who knows them; today's repliers in that band were mostly fan accounts.
- **No velocity baseline.** A z-score per narrative across runs would say "faster than usual". That needs a narrative identity that survives between runs, which LLM-made ids don't have.
- **fxtwitter's terms are unclear,** and it can break without notice. It sits behind `XBackend` so it can be dropped in one line.

## Ruled out

- **A browser backend.** X's Terms of Service, effective 2026-10-09, forbid scraping "in any form, for any purpose" without written consent and set liquidated damages per million posts (x.com/en/tos). A logged-out probe also showed X returns 403 to headless Chromium unless its user agent is spoofed, which would be getting around a bot block. The drafted backend was not committed.
- **Nitter, xcancel and `syndication.twitter.com`.** Nitter returned 410, xcancel 451, and syndication 429.
- **Reddit.** JSON returns 403 on every host; RSS returns 429 within three requests and has no scores.
- **A second fxtwitter client.** The context worker already wrote one with rate limiting and a cache, and two clients would split the shared budget.
- **LLM-guessed velocity.** It was the only velocity before this branch. It stays as the fallback when signals have no timestamps.
- **URLs copied out of LLM text.** Google News links are 200-character redirects that a model can mangle. Ids map back to exact URLs.

## The observation agent

The second whiteboard row has one agent reading several feeds for a goal. `harness/sources/agent.py` builds it as the `agent` source, so `HARNESS_SOURCES=agent` turns it on:

1. Its tools are the other sources. Each tool entry has the source's docstring line and, for `google_news`, `bluesky`, `x`, `x_targets` and `attention`, the list the agent may replace for one call, with its default from the TOML.
2. Feeds that would only raise this round, such as `x` without a key or `x_brand` without a handle, are left off the tool list, and the trace notes why.
3. Round one plans from the mission and the lessons. Round two gets a summary of each call (count, error, three hottest items) and may add calls that dig into what round one found, or stop. Exact repeats are dropped.
4. With the mock provider, or when planning fails, it runs the default plan: `[agent] default_sources` from the TOML, or else every configured feed.
5. It writes `out/observe-agent-trace.json` with every call, its parameter, its reason, its count and its error. When eval decides observe was at fault for a bad action, the trace shows whether the agent skipped the feed that had the story.

In a real run on 2026-10-15 (Sonnet 5.5), round one picked six of the seven available feeds and skipped Bluesky. Round two noticed the Sophie Cunningham and Angel Reese story in the results, searched Google News, Bluesky and attention for it by name, and re-read `x_targets` with six accounts of its own choosing because round one had found only one target. The agent plans with `HARNESS_PROVIDER`, not the CLI's `--provider` flag, because a source plugin doesn't see CLI arguments. Its budget is in `ObserveKnobs`: two rounds, six calls per round, six items per list.

## Open questions

- Which brand does the harness serve tonight: the Caitlin Clark fan account, a dev-tool startup, or Coordigent?
- What is the brand's X handle?
- Will anyone fund an X API key with a spending cap, or an xAI key? Either one turns on search, mentions and reply targets beyond the watch list.
- Does Sam keep fxtwitter, given its unclear terms?
- Merge order: `sam/context-dynamics` should land before or with this branch, or `x_targets` has no keyless backend.

## What this branch built

- Keyless sources: `espn_news`, `espn_scores`, `espn_injuries`, `google_news`, `bluesky`.
- One read-only X interface with three backends, and the `x`, `x_targets` and `x_brand` sources on top of it.
- Reply-target discovery with snapshot velocity, size bands, crowding and per-author caps.
- Data-driven orient velocity, a budgeted prompt, and X-only reply targets on each narrative.
- `ObserveKnobs`, the brand TOMLs, and offline tests under `tests/test_observe_*.py`, with fixtures in `observation/fixtures/`. `tests/conftest.py` fails any unit test that opens a socket.
- The observation agent.
