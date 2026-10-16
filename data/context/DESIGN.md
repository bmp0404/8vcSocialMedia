# Context and engagement dynamics

This is for teammates deciding what to build on top of the dev-tool dataset and the X reader. It answers four questions. What can we know about a post beyond the counts X returns? Where does each piece come from, and what does it cost? What does the first pass over David's CSV say about Super Viral against Deadweight? What should be built next?

Code lives in `harness/context/`, data in `data/context/`. Every command below runs from the repo root.

## What existed before this branch

- `data/devtools_virality_30d.csv` (David Gross, commit `c9f8904`): 1,665 posts from 91 accounts, 2026-09-15 to 2026-10-16, with Category, counts, Media, Date and Segment. No times of day, follower counts, views, quotes or curves.
- `harness/sources/x_search.py` was a stub that raised `NotImplementedError`. There was no X reader.
- Since then, Observe's `harness/sources/x_fx.py:21` and `harness/sources/attention.py:60` on `main` import this package lazily, so merging this branch turns both on.
- `harness/dataset.py` on `aarav/self-improving` loads the CSV into `Post` objects. The enriched CSV keeps David's eleven columns first and unchanged, so `dataset.load(path=...)` reads it as-is. `harness/context/backfill.py:load_dataset` also reads the CSV, as raw dicts, because enrichment has to copy every original value verbatim. Once Aarav's loader merges, that is the only reason to keep a second reader.
- Krish's memory store (`memory/README.md` on `main`) and Dhilan's reference judge (`dhilan/version-1`) consume labels and exemplars. Memory's reflection job filters on `boost_suspect`, `is_thread_continuation` and `label_mature` from `boost_flags.csv`.

## What the CSV really contains

These facts change how any model trained on the CSV should be read. Each one was checked against the data or against X.

- Every row has an exact timestamp. A status ID is a Snowflake: `(id >> 22) + 1288834974657` is the creation time in Unix ms. `harness/context/snowflake.py` decodes it, and the test pins two real posts to the second X reports. All 1,665 decoded dates match the `Date` column.
- `Engagement` is likes + replies + retweets + quotes. On Deno's acquisition post the difference is 618, exactly X's quote count, and on Supabase's "Love that guy" it is 25. So `outcome_quotes` comes free for every row.
- `Category` is a threshold on `Engagement`: Deadweight 0 to 10, Medium Viral 100 to 749, Super Viral 753 and up. The file holds no post with engagement between 11 and 99. On the accounts' own timelines that band is 52% of original posts, so the CSV compares the tails of each account's distribution and drops the middle.
- 718 of the 1,133 Deadweight rows, and 19 Medium Viral rows, are the second or later post of the account's own thread (`replying_to` is the account itself). A thread tail is not a failed post. They are flagged `is_thread_continuation` and excluded from labels and baselines.
- The CSV was committed at 01:07:58 UTC on Oct 16, which bounds the scrape time. 201 rows were under 48 hours old then; `label_mature` marks the rest. Counts moved little between the scrape and the X fetch an hour later (median change 0), so the fetch-time counts and the CSV agree for mature posts.

## Sources

All of these work without a key. "History" means the source can say what was true at a past time; "forward" means it only reports the present, so it has to be recorded as it happens. Results of the last live check are in `data/context/live_checks.json` (`python -m harness.context check`).

| Source | Module | Gives | History | Limits seen on 2026-10-15/16 |
| --- | --- | --- | --- | --- |
| fxtwitter API | `xreader.py` | Post counts with views, bookmarks and quotes; author followers; timelines with reposts; first page of replies in both sort orders; reposters, largest first | Counts are current only; timelines and reply timestamps reach back weeks | Third-party scraper of X with unclear terms. X's terms from 2026-10-09 forbid scraping "in any form" without consent; whether using a proxy is acceptable is Sam's call. Can break without notice. Conversation cursors 404, so about 65 replies at most. No search, no quote list. |
| Snowflake decode | `snowflake.py` | Exact creation time of any post, reply, quote or account | Yes | None |
| Wikipedia pageviews | `collectors/wikipedia.py` | Daily views per article (REST); hourly views from the dump files; top articles per day | Yes | Per-article `hourly` returns HTTP 400. Daily lags about a day. A dump hour is 55 to 66 MB and lands 2 to 2.5 hours late. |
| Wikidata | `collectors/wikipedia.py` | X handle to Wikipedia article through property P2002 | Yes | A case-insensitive query got HTTP 429; the exact-match query works. 19 of 91 dev-tool accounts have an English article. |
| Hacker News (Algolia) | `collectors/hackernews.py` | Stories with points and timestamps; title-only search | Yes, but points are current | `front_page` tag only works for today. Typo tolerance turned "vLLM" into 944 stories until it was switched off. Common-word names (Amp, Modal) still match unrelated stories. |
| Google Trends | `collectors/google_trends.py` | Trending-now RSS; interest over time, hourly for windows up to a week, daily beyond | Yes for interest over time; RSS is live only | Unofficial explore API, tagged as a scraper; values are 0 to 100 within each request, not comparable across requests. |
| trends24 | `collectors/x_trends.py` | Hourly lists of 50 X trends per country, no volumes | Live page covers 24 hours; Wayback captures are sparse | 10 days between Aug 31 and Oct 16 have a usable US capture. getdaytrends dated pages return 520. |
| GDELT DOC 2.0 | `collectors/gdelt.py` | News article counts per 15 minutes, hour or day | Last 3 months | One probe in twelve succeeded; most calls get 429 from this IP. It also misses most dev-tool news. Best effort only. |
| ESPN site API | `collectors/espn.py` | Game schedule and the first and last play wallclock, so "minutes since the final buzzer" | Yes, one date per call | Date ranges return 400. |

Every collector goes through `harness/context/http.py:PoliteClient`: one User-Agent, a per-host minimum interval shared by every process on the machine through timestamp files in `~/.cache/media-harness/throttle/`, retries with backoff on 429 and 5xx, and a disk cache in `data/context/cache/` (gitignored). The bulk dataset jobs run at 0.5 requests per second because Observe shares the IP's fxtwitter budget.

## Data dictionary

Columns are grouped by when they are knowable, so a model cannot mix a feature with its own outcome. `pre_*` is known when the post goes out. `outcome_*` is measured after posting, mostly at fetch time, and `outcome_spillover_*` is outside attention after the post, which the post may have caused. `label_*` columns are targets or decide which rows a target is fair for. Three names stay unprefixed because teammates already filter on them: `eng_ratio_author_median` (a label), `boost_suspect` and `is_thread_continuation`.

Cost is per post unless stated. "Free" means computed from data already present.

### Timing

Hypothesis: posting into an awake audience and away from your own last post earns more first-hour engagement, and X's ranker decays repeat posts by one author in the same feed batch.

| Columns | Source | Keyless | History | Cost |
| --- | --- | --- | --- | --- |
| `created_at_utc`, `pre_hour_utc`, `pre_hour_local`, `pre_hour_local_frac`, `pre_dow_local`, `pre_is_weekend_local`, `pre_part_of_day_local`, `pre_minute_of_week_local`, `pre_audience_tz` | Snowflake decode; audience time zone from `lexicons.json` (Pacific for dev tools, Indianapolis for the fan account) | Yes | Yes | Free |
| `pre_round_minute` | Minute divisible by 15, the mark of a scheduler | Yes | Yes | Free |
| `pre_seconds_since_prev_own_post`, `pre_author_posts_prev2h` | Own posts from the timeline plus the CSV | Yes | Yes | One timeline read per account |

### Text and format

Hypothesis: format and framing shift engagement a little within an account; most of the spread is the account itself (Martin et al. 2016 found content explains under 5% of cascade size).

| Columns | Source | Keyless | History | Cost |
| --- | --- | --- | --- | --- |
| `pre_n_chars`, `pre_n_words`, `pre_n_lines`, `pre_first_line_chars`, `pre_n_links_text`, `pre_n_mentions`, `pre_n_hashtags`, `pre_n_emoji`, `pre_n_exclaim`, `pre_has_question`, `pre_has_number`, `pre_caps_ratio`, `pre_starts_with_mention`, `pre_is_thread`, `pre_has_list`, `pre_first_person_singular`, `pre_first_person_plural`, `pre_all_lowercase`, `pre_short_no_link`, `pre_media`, `pre_has_media` | Post text and the Media column (`features.py:text_features`) | Yes | Yes | Free |
| `pre_hook_announce`, `pre_hook_feature_tip`, `pre_hook_funding_deal`, `pre_hook_hiring`, `pre_hook_event`, `pre_hook_social_proof`, `pre_hook_humor`, `pre_mentions_big_name` | Keyword lists in `data/context/lexicons.json`; brand-specific names live there, not in Python | Yes | Yes | Free |
| `pre_is_self_reply`, `pre_is_reply_to_other`, `pre_is_quote_post` | fxtwitter `replying_to` and `quote` | Yes (scraper) | Yes | One status read |

Sentiment, humor, controversy and arousal are not built. A keyword lexicon would be too crude to trust, and an LLM labeler would spend credits on a signal the analysis suggests is small. It is on the build list.

### Author baseline and normalised targets

Hypothesis: the author's own history predicts most of a post's engagement, so a fair target is the lift over that history. The past-only baseline is a feature; the whole-window baseline sees later posts and is a label.

| Columns | Meaning | Keyless | History | Cost |
| --- | --- | --- | --- | --- |
| `pre_author_prior_n`, `pre_author_prior_median_eng`, `pre_author_prior_median_views`, `pre_author_prior_viral_share` | Median over the author's original posts made at least 48 hours before this one (timeline and CSV, no thread continuations, at least 5 posts). The 48-hour gap matters because every count is read at fetch time: a post from 30 minutes earlier still carries engagement it gathered after this post went out | Yes | Yes | Timeline reads, about 8 pages per account |
| `outcome_author_followers_at_fetch`, `outcome_author_verified` | Read at fetch time. A post that went viral grew the number it would be normalised by, so these are outcomes. For a follower feature, join `accounts.csv` on the account | Yes | No (current only) | Free with the status read |
| `label_lift_prior` | `log1p(engagement) - log1p(pre_author_prior_median_eng)` | | | Free |
| `label_above_author_prior`, `label_above_author_median` | Within-account binary tiers | | | Free |
| `eng_ratio_author_median`, `label_lift_window`, `label_author_window_median_eng`, `label_author_median_source` | Engagement over the author's median for the whole window, from the timeline when it has 10 or more originals, else from the CSV. The CSV median is distorted because the file drops the 11 to 99 band | | | Free |
| `label_category_csv`, `label_category_x` | David's category, and the category recomputed from fetch-time counts with the restored `Low` band (11 to 99) | | | Free |
| `label_age_at_scrape_hours`, `label_mature`, `label_eligible`, `label_continuation_source` | Age at the CSV commit; mature at 48 hours; eligible means mature and not a thread continuation | | | Free |

### Outcomes at fetch time

Hypothesis: splitting engagement into reach (views per follower) and conversion (engagement per view) tells distribution apart from resonance. A post that reached 20 times its usual audience and converted at its usual rate was distributed, not loved.

| Columns | Source | Keyless | History | Cost |
| --- | --- | --- | --- | --- |
| `outcome_x_fetched_at`, `outcome_age_at_fetch_hours`, `outcome_x_likes`, `outcome_x_reposts`, `outcome_x_replies`, `outcome_x_quotes`, `outcome_x_bookmarks`, `outcome_x_views`, `outcome_eng_delta_since_csv` | fxtwitter status, value at `outcome_x_fetched_at` | Yes (scraper) | No | One request |
| `outcome_quotes`, `outcome_log_engagement` | CSV | Yes | Yes | Free |
| `outcome_x_weighted_engagement` | `0.66 likes + reposts + 5 replies + 5 quotes`, X's 2026 ranker weights (`xai-org/x-algorithm`, `home-mixer/params/param.rs`). The ranker multiplies predicted probabilities by these weights, not counts, so this is a proxy for what X values | Yes | Yes | Free |
| `outcome_eng_per_1k_followers`, `outcome_views_per_follower`, `outcome_eng_per_view`, `outcome_bookmarks_per_like` | Ratios of the above | Yes | No | Free |

### Outside attention

Hypothesis: a post rides attention that already exists. Only counts from strictly before the post are features; counts after it are spillover, because the Deno HN story went up 2 minutes after the Deno post.

| Columns | Source | Keyless | History | Cost |
| --- | --- | --- | --- | --- |
| `pre_hn_stories_prev24h` | HN stories whose title names the account, created in the 24 hours before the post | Yes | Yes | One request per account for the window |
| `outcome_hn_max_points_prev24h`, `outcome_hn_500plus_stories_prev24h` | Points on those stories, and all HN stories with 500+ points in the previous 24 hours. Points are read at fetch time, so a story posted a minute before the post carries points it earned later; a point-in-time version needs Wayback snapshots of the front page | Yes | Points are current | One request per day |
| `pre_wiki_article`, `pre_wiki_views_prev_day`, `pre_wiki_prev_day_ratio` | Wikipedia daily views of the account's article on the day before, against the 14-day median | Yes | Yes | One request per article |
| `pre_news_articles_prev_day`, `pre_news_prev_day_ratio` | GDELT daily news count, when GDELT answers | Yes | Last 3 months | One request per account, mostly 429 |
| `pre_x_trend_lists_available`, `pre_mentions_x_trend_prev24h` | trends24 lists published in the 24 hours before the post, from Wayback captures | Yes | Only days with a capture | One request per capture |
| `outcome_spillover_hn_stories_next24h`, `outcome_spillover_hn_max_points_next24h`, `outcome_spillover_wiki_same_day_ratio` | The same sources after the post | Yes | Yes | Free once collected |

### Replies, reposters and cascades

Hypothesis: who amplified a post explains its jumps. Goel et al. 2016 found popularity tracks the size of the largest single broadcast.

| Columns | Source | Keyless | History | Cost |
| --- | --- | --- | --- | --- |
| `outcome_reply_coverage`, `outcome_reply_sample_n` | fxtwitter conversation in `likes` and `recency` order, merged; coverage is unique replies over X's reply count | Yes (scraper) | Reply timestamps, yes | Two requests |
| `outcome_reply_low_follower_share`, `outcome_reply_dup_share`, `outcome_reply_big_accounts`, `outcome_reply_max_follower`, `outcome_reply_insider_share` | Replier follower counts, duplicate texts, repliers whose bio names the brand | Yes | Yes | Free with the sample |
| `outcome_reply_burst_share`, `outcome_reply_median_delay_min`, `outcome_t_to_reply_1_min`, `outcome_t_to_reply_5_min`, `outcome_t_to_reply_10_min` | Reply timing, only when coverage is 0.8 or more. The likes-ranked page alone puts early replies first, so on a partial sample these would make every post look like an early spike | Yes | Yes | Free with the sample |
| `outcome_reposters_sampled`, `outcome_reposters_complete`, `outcome_max_reposter_followers`, `outcome_top10_reposter_followers_sum`, `outcome_reposter_staff_share`, `outcome_reposter_new_account_share` | fxtwitter reposters, roughly largest first (seen on one post). Reposts have no timestamps. New-account share is only filled when the sample covers 90% of reposts | Yes | Who, not when | One request per 100 reposters |

### Boost suspicion

The team rule is to mark these rows, not drop them. `harness/context/boost.py` splits the old single flag into three, because they mean different things to a brand.

| Column | Rule | Calibrated? |
| --- | --- | --- |
| `boost_paid_or_external_reach` | Views at least 5 times the author's median views and engagement per view at most 0.4 times the author's median, for authors with 5 or more posts with views. X counts promoted impressions in public views, so promotion looks like views without engagement; a repost by a much larger account looks the same | No. The cutoffs come from the red-team review and were chosen by eye on Qodo 3.0, CockroachDB and Supabase's Grok launch |
| `boost_coordinated_authentic` | 20% or more of 10 or more sampled reposters or repliers name the brand in their bio | No |
| `boost_likely_inauthentic` | Two or more votes from: reposts per like above the dataset's 99th percentile, bookmarks or replies per like below its 1st, throwaway repliers (under 20 followers) at 50% or more, duplicate replies at 20% or more, a reply burst, new-account reposters at 50% or more, and the curve flags `drip_feed`, `like_only_burst`, `count_drop` | No. Percentile votes describe outliers in this dataset, not proven fraud |
| `boost_suspect` | Paid or external reach, or likely inauthentic | |
| `boost_class`, `boost_inauthentic_votes`, `boost_reasons`, `boost_weak_signals` | The most severe class, the vote count, the votes, and the signals that are recorded but do not vote: `like_rate_high` (on this dataset it flagged only organic memes) and `author_outlier` (launches are meant to beat the author's median) | |

`data/context/boost_flags.csv` carries these columns plus `is_thread_continuation`, `label_mature` and `label_eligible`, keyed by `Post URL`.

## Engagement curves

X returns only current counts. A curve exists only if someone records snapshots while the post is live, or rebuilds one from the timestamps of replies and quotes (each one a Snowflake). `harness/context/curves.py` is the model; `harness/context/tracker.py` records the snapshots.

### Snapshot schema

`curves.Snapshot` holds `post_id`, `observed_at` (UTC), `likes`, `reposts`, `replies`, `quotes`, `bookmarks`, `views` and `source`. The tracker appends one JSON line per poll to `data/context/tracker/snapshots.jsonl`. `curves.build_curve` sorts snapshots, adds the zero at creation, and keeps both the running maximum and the raw counts, because a falling count is itself a signal.

### Shapes and flags

`curves.detect` resamples the cumulative curve onto 15-minute bins and assigns exactly one shape, checked in this order. Every threshold is a field of `InflectionParams`, and `tests/test_context_curves.py` pins each definition with a synthetic curve.

| Shape | Definition | What usually causes it |
| --- | --- | --- |
| `insufficient` | Observed under an hour, or fewer than 3 points | Not enough data |
| `stillborn` | Observed long enough, total under 20 | Nobody saw it |
| `step_jump` | One bin, or two adjacent bins when the jump straddles a grid edge, after the first hour holds 25% or more of the total; the bin before is at most a fifth of the largest; the next bin falls | A large account quotes or reposts |
| `circadian_echo` | Two or more separate peaks, each 20 to 28 hours after the last | The audience waking up again (Kobayashi and Lambiotte 2016) |
| `multi_wave` | Two or more peaks at least 30% of the highest (and at least 5 events in the peak bin), 3 hours apart, with a trough under 30% of the smaller | Re-ignition from news, an HN front page, a newsletter |
| `delayed_takeoff` | Quiet first hour (mean rate under 20% of the peak), gradual rise over 2 or more bins, peak 3 hours or later | Algorithmic pickup |
| `early_spike` | Peak in the first 2 hours, rate halves within 3 hours | A post that lands on followers and stops |
| `slow_burn` | Peak under 3 times the mean rate, half the total after 6 hours | Reference content |
| `unclassified` | None of the above | Says so rather than guessing |

Flags are independent of the shape. `reply_ratio` (replies plus quotes at least equal to likes, 20 or more replies), `drip_feed`, `like_only_burst`, `count_drop`, `unexplained_step` (a step with no large reposter or replier at that moment, raised only when the tracker snapshotted reposters), `view_surge_low_conversion` (view rate 5 times the earlier median while engagement per view falls below 0.3 times the rate so far) and `slow_start` (below the author's median at 60 minutes, when supplied).

`drip_feed` uses the variance-to-mean ratio of raw increments over 16 regular polling intervals after the second hour. A Poisson process gives about 1, organic engagement more, and delivery at a fixed rate near 0. Against 300 simulated Poisson tracks with 15-minute polling, the 0.15 cutoff fired on none of the 14-hour tracks and 2 of the 48-hour tracks; a 12-interval window at 0.2 fired on 50. It runs on raw snapshots only, because interpolation across a long gap makes increments look perfectly regular.

`harness/context/decay.py` fits the post-peak power-law exponent, pooled over an account's posts (each post keeps its own amplitude), by Poisson maximum likelihood, and maps it to Crane and Sornette's classes: sudden onset with exponent near 1.4 is exogenous subcritical, near 0.6 exogenous critical, and gradual onset near 0.2 endogenous critical. The fit recovers the exponent to 0.001 on exact synthetic counts. It needs about 50 post-peak events per account, so it waits on tracker data.

Not built from the red-team taxonomy: author-fueled threads (the author replying to keep the thread alive), synchronised replies by scan statistic, event-locked onsets against ESPN plays or keynotes, and converting delays to audience-activity time (the TiDeH correction).

### Tracker

`python -m harness.context track watch <handles>` and `track loop` poll on a log-spaced schedule by default: 5, 15, 30, 60 and 120 minutes, then 4, 8, 24, 48 and 168 hours, recording views every time. That is 10 requests per post per week. Early speed predicts final size (Szabo and Huberman 2010; Cheng et al. 2014), so the early points matter most. `TrackerParams(dense=True)` polls every 5 to 15 minutes for the first day, about 168 requests per post, and is the only mode fine enough to see a step jump on the 15-minute grid; use it for the brand's own posts. `track loop --reposters` snapshots the 100 largest reposters at each poll; a big account's first appearance brackets when it reposted, which is the only repost timing fxtwitter exposes and what `unexplained_step` needs. `track report <id>` adds minutes to the 1st, 5th and 10th reply and the like-per-view rate in the first 2 hours, the number X's cold-start code uses for authors under 50,000 followers.

A watch pass reads each watched timeline every 15 minutes and starts tracking any post under 6 hours old, so a post is caught near its start. With a 1 request per second budget shared with Observe, the log schedule supports hundreds of tracked posts. A live loop ran tonight on @CaitlinClark22, @IndianaFever, @WNBA and @AtlantaDream and tracked 8 posts. It started after those posts went out, so each curve runs straight from zero to a first snapshot 1 to 5 hours in, and `detect` calls them `unclassified` rather than guess. The watch has to be running before a post to see its start.

## First analysis: Super Viral against Deadweight

`python -m harness.context analyze` writes every number below to `data/context/analysis.json`. Rows are restricted to `label_eligible` (mature, not a thread continuation): 93 Super Viral, 372 Medium Viral and 363 Deadweight. X metrics cover 1,664 of 1,665 rows and timelines all 91 accounts.

The short version: at the two tails, which account posted explains nearly everything, so Super Viral against Deadweight is not a content question. The CSV's construction exaggerates this. Content effects that survive a within-account comparison are small, mostly about format, and rest on about 20 accounts. What separates Super from Medium Viral is reach, not conversion.

### Selection effects come first

- 718 of 1,133 Deadweight rows are thread continuations. Left in, "what makes a post fail" learns "being tweet 4 of a thread".
- The missing band is half of what accounts post. Of 1,824 original timeline posts in the window, 942 (52%) have engagement 11 to 99, and 940 of those are absent from the CSV. `timeline_posts.csv` has every one with recomputed categories (`label_category_x`).
- 25 of 91 accounts have a Super Viral post, ten hold 79% of them, and no "Smaller/closest" account has one.

### At the tails, the account is the answer

The author's past-only median engagement (`pre_author_prior_median_eng`: original posts at least 48 hours older, at least 5 of them) separates eligible Super Viral from Deadweight at AUC 0.993 (93 against 355 posts). With thread continuations left in, the same feature gives 0.909, close to the red-team pass's 0.90 on the raw CSV; continuations cluster in a few accounts (Resend alone has 150), so dropping them changes which accounts the Deadweight rows come from.

That number looked too good, so I checked it for leakage. Rebuilt from timeline posts only (never CSV rows, which were selected on the outcome) and ending 72 hours before each post, so no earlier post's count includes engagement that arrived after the target, it stays at 0.994 (93 against 341). A 0, 24 or 72 hour gap gives the same result, the window is strictly before the post, and the 19 duplicated texts in the CSV are same-category boilerplate ("download Zed and view the full changelog here"). The separation is real and built into the labels. Deadweight is an absolute 10 or fewer, Super Viral an absolute 753 or more, and once thread tails are gone only 5 of 91 accounts have an eligible post in both.

The comparison where content can matter is Super against Medium Viral, where the same timeline-only floor is 0.67 (93 against 350). Medium against Deadweight is 0.991. Eval should report every model against these floors, score on `label_lift_prior` with time-ordered splits, and not spend effort on Super Viral against Deadweight.

### Content effects within an account are small

Each row compares an account with itself: the mean difference between its posts in the two categories, averaged over accounts that have both, with a 95% interval from resampling accounts. About 35 features were tested in each comparison, so one or two intervals clearing zero is what chance alone would produce.

| Feature | Comparison (accounts) | Within-account difference | 95% interval | Accounts lower / tied / higher |
| --- | --- | --- | --- | --- |
| Quote post (`pre_is_quote_post`) | Medium vs Deadweight (17) | -39 points of share | -57 to -22 | 11 / 5 / 1 |
| Own posts in the previous 2 hours | Medium vs Deadweight (18) | -0.44 posts | -0.86 to -0.11 | 11 / 6 / 1 |
| Announcement wording (`pre_hook_announce`) | Medium vs Deadweight (18) | +21 points of share | +9 to +35 | 1 / 8 / 9 |
| Length (`pre_n_chars`) | Medium vs Deadweight (18) | +81 characters | +28 to +133 | 5 / 0 / 13 |
| Links in text (`pre_n_links_text`) | Medium vs Deadweight (18) | -0.25 per post | -0.49 to 0.00 | 10 / 5 / 3 |
| Event or webinar promo (`pre_hook_event`) | Medium vs Deadweight (18) | -6 points of share | -10 to -2 | 6 / 12 / 0 |
| Local hour | Medium vs Deadweight (18) | -2.3 hours | -4.0 to -0.7 | 13 / 0 / 5 |
| Has media (`pre_has_media`) | Super vs Medium (21) | +19 points of share | +4 to +35 | 3 / 3 / 15 |
| Mentions (`pre_n_mentions`) | Super vs Medium (21) | -0.23 per post | -0.44 to -0.07 | 12 / 7 / 2 |

Read these with care:

- Length flips sign between views. Pooled, Super Viral posts are shorter than Deadweight (AUC 0.42), because big accounts post short. Within an account, longer posts do better. Any pooled content finding here can be an account finding in disguise.
- Quoting someone else's post is the clearest within-account loser. A plausible reason is that the quoted post takes the attention, but this data cannot tell.
- Posting soon after your own last post does worse. That matches the author-diversity decay in X's 2026 ranker (repeat posts in a batch scaled by 0.75 times 0.5 to the k, plus 0.25), but bursts of low-effort posts would look the same.
- Media is the one format feature that separates Super from Medium Viral within accounts, in 15 of 21.
- Hour of day: pooled it looks strong (AUC 0.64), within accounts it points the other way, and the eval worker found no within-account signal (pair accuracy 0.499). Treat it as context, not a lever.

Against the within-author lift (`label_lift_prior`, 806 eligible posts with a prior), no content or timing feature reaches a Spearman correlation of 0.14 in absolute value. The strongest are links (-0.135), all-lowercase text (+0.135), event promos (-0.133), first-person singular (+0.10) and scheduler round minutes (-0.10). That fits Martin et al. (2016): once the author is known, content explains a few percent of the spread.

### Super Viral is distribution, not conversion

These are outcomes, so they describe what happened rather than predict it.

| | Super Viral | Medium Viral | Deadweight |
| --- | --- | --- | --- |
| Median views per follower | 1.08 | 0.24 | 0.042 |
| Median engagement per view | 1.22% | 1.17% | 0.58% |
| Has a reposter larger than the author | 39% (of 93) | 31% (of 133) | 4% (of 23) |

Super and Medium Viral posts convert viewers at the same rate. Super Viral posts reached 4.6 times as many people per follower, and more often had a reposter bigger than the author. Deadweight posts were both shown less and converted half as well. The lever that separates a hit from a solid post is distribution: a larger account reposting, or the ranker picking it up. For the fan account, that argues for posts a bigger account would want to quote.

### Reply curves check the curve model on real arrival times

Fifty-nine eligible posts had a reply sample covering at least 80% of their replies and 20 or more replies. Rebuilt from reply timestamps and run through `curves.detect`, 52 come out `early_spike`, 4 `delayed_takeoff` (all Super Viral) and 3 `unclassified`. Pooled per account, the post-peak decay exponent sits between 1.09 and 1.67 for the 13 accounts with enough events. Eleven have mostly sudden onsets and land in the exogenous subcritical class, a shock that replies do not carry forward. Supabase and Expo have mostly gradual onsets with the same fast decay, which the theory does not predict, so they are labelled `mixed`. Replies reach few non-followers, so this tests the model rather than measuring virality. The first run called 33 of the 59 curves `multi_wave`, because three replies landing in one 15-minute bin looked like a second wave; wave peaks now need 5 events.

### Boost flags

36 posts are `boost_paid_or_external_reach` (9 Super Viral, 20 Medium Viral, 7 Deadweight), including Supabase's Grok Bot post (1.1M views against a typical 7k to 13k), CockroachDB's "cockroaches will outlast us all" (4.5M views, 13.8k followers) and Qodo 3.0 (414k views, 19.8k followers). 9 are `boost_coordinated_authentic`, all from repliers whose bios name the brand (Expo 3, PostHog 2, Resend 2, Zed 1, vLLM 1). One is `boost_likely_inauthentic`: Polar's post "hello", whose replies are mostly "hello" back. That is a community joke, not a bot ring, and it is the clearest example of why these labels are uncalibrated.

### What is small-sample or confounded

- The within-account tables rest on 17 to 21 accounts, and about 35 features were tested per comparison.
- Followers, views, bookmarks and HN points are fetch-time values and appear only as outcomes.
- Reposter samples cover all Super Viral posts but only a sample of Medium Viral and Deadweight posts.
- The keyword hooks are crude. "Announcement" means the text matched one of 20 phrases.
- 201 rows were under 48 hours old at the scrape and are excluded as immature.

## Interfaces for other workers

- Observe calls `XReader` (`status`, `profile_statuses`, `conversation(status_id, ranking_mode)`, `replies`, `reposters`) through `harness/sources/x_fx.py`, and `collectors.attention(term, start, end, errors=...)`, which returns `{source: Series}` with `Series(term, source, resolution, points, fetched_at, note)`.
- Eval reads `devtools_enriched.csv`. Train on `pre_*` only, filter on `label_eligible`, and score against `label_lift_prior` or `label_above_author_prior`. Beat the author-prior floor in the analysis, split by time, and compare same-author pairs.
- Memory reads `boost_flags.csv` and `accounts.csv` (one row per account with followers and medians).
- Action: the reach and conversion split says the gap between Medium and Super Viral is distribution, so drafts should aim to be reposted by larger accounts. X's 2026 ranker removes posts labelled as insults or engagement bait from non-followers' feeds (red-team report, section 8), so rivalry banter should name the rival without insulting anyone.
- UI: one row of `devtools_enriched.csv` or `timeline_posts.csv` is a post card; `curves.Inflection.to_dict()` (shape, flags, features, stats) and `tracker.Tracker.report()` describe one curve.
- The iterate worker tunes `harness/context/settings.py:ContextConfig`: `inflection` (shape and flag thresholds), `boost` (quantiles, reach ratios, vote count), `enrich` (thread gap, maturity age, baseline sizes, ranker weights), `tracker` (schedule, start age, cold-start window) and `fetch` (intervals). Only the shape thresholds have an exact test; nothing has labelled outcomes to tune against yet.

## Ruled out

- pandas and scikit-learn. The analysis is counts, medians, AUC and bootstrap intervals, which the standard library covers, and a new dependency means `uv.lock` churn across five branches.
- GDELT's raw 15-minute files as a fallback for the 429s. The red-team pass found no Deno rows in them; GDELT misses dev-tool news.
- Per-article hourly Wikipedia. The API rejects it. The dump reader exists for the few hours around a fan-account post.
- getdaytrends dated pages (520) and US trends24 captures for October (404 on Wayback).
- A single `boost_suspect` vote count. It mixed paid reach, staff pushes and bots, and its likes-per-view rule flagged memes.

## Open questions

- Is fxtwitter acceptable given X's terms? Everything X-side here depends on it.
- What is the fan account's handle? The tracker should watch it in dense mode the moment Grok posts.
- Should eval treat the CSV or `timeline_posts.csv` as the training set? The timeline file restores the missing band and follows David's thresholds.
- Who owns labelling a few known boosted posts? Without them no boost cutoff can be calibrated.

## Ranked build list

1. Merge this branch. It turns on Observe's fxtwitter backend and attention source.
2. Point Eval at `label_eligible`, `label_lift_prior` and the author-prior floor; retrain on `timeline_posts.csv`.
3. Run the tracker in dense mode on the brand's own posts and on the reply targets Observe picks, so the curve model gets real data. The decay fit and every curve flag wait on it.
4. Re-run `python -m harness.context backfill-x` and `enrich` 48 hours after the scrape to replace immature labels with settled counts.
5. Label 20 known launches by hand as paid, staff-pushed or organic, and calibrate the boost cutoffs on them.
6. Add reposter features for every Medium and Super Viral post (only a sample has them now) and a reposter step attribution for `step_jump`.
7. Add an LLM labeler for arousal, humor, controversy and engagement-bait phrasing, used as features, after the cheap features are exhausted.
8. Add Bluesky search and HN Wayback front-page snapshots as point-in-time context for dev tools; Google Trends 8-minute buckets for the fan account's 7-day window.

## Reproduce

```sh
uv run python -m harness.context backfill-x      # X counts for every post, then author timelines (0.5 req/s)
uv run python -m harness.context collect context # HN, Wikipedia, trends, GDELT if it answers
uv run python -m harness.context collect all-x   # reply samples and reposters
uv run python -m harness.context enrich          # writes the CSVs
uv run python -m harness.context analyze         # writes analysis.json
uv run python -m harness.context check           # one live data point per source
uv run python -m pytest tests/test_context_*.py
```

Research notes behind the citations, copied from `/tmp` before it is swept, are in `data/context/research/`: the red-team report (`red_team.md`) and the literature and feature surveys it relied on.
