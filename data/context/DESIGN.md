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
- `Category` is a threshold on `Engagement`: Deadweight 0 to 10, Medium Viral 100 to 749, Super Viral 753 and up. The file holds no post with engagement between 11 and 99. On the accounts' own timelines that band is most posts (see the timeline numbers in the analysis), so the CSV compares the tails of each account's distribution and drops the middle.
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
| `pre_author_prior_n`, `pre_author_prior_median_eng`, `pre_author_prior_median_views`, `pre_author_prior_viral_share` | Median over the author's earlier original posts (timeline and CSV, no thread continuations, at least 5 posts) | Yes | Yes | Timeline reads, about 8 pages per account |
| `pre_author_followers_at_fetch`, `pre_author_verified` | Read at fetch time. A post that went viral grew the number it would be normalised by, so this overstates followers at post time for exactly the posts that matter | Yes | No (current only) | Free with the status read |
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
| `pre_hn_stories_prev24h`, `pre_hn_max_points_prev24h` | HN stories whose title names the account, created in the 24 hours before the post | Yes | Yes (points are current) | One request per account for the window |
| `pre_hn_500plus_stories_prev24h` | All HN stories with 500+ points in the previous 24 hours: competition for developer attention | Yes | Yes | One request per day |
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
| `step_jump` | One bin after the first hour holds 25% or more of the total, the bin before it is at most a fifth of it, and the next bin falls | A large account quotes or reposts |
| `circadian_echo` | Two or more separate peaks, each 20 to 28 hours after the last | The audience waking up again (Kobayashi and Lambiotte 2016) |
| `multi_wave` | Two or more peaks at least 30% of the highest, 3 hours apart, with a trough under 30% of the smaller | Re-ignition from news, an HN front page, a newsletter |
| `delayed_takeoff` | Quiet first hour (mean rate under 20% of the peak), gradual rise over 2 or more bins, peak 3 hours or later | Algorithmic pickup |
| `early_spike` | Peak in the first 2 hours, rate halves within 3 hours | A post that lands on followers and stops |
| `slow_burn` | Peak under 3 times the mean rate, half the total after 6 hours | Reference content |
| `unclassified` | None of the above | Says so rather than guessing |

Flags are independent of the shape. `reply_ratio` (replies plus quotes at least equal to likes, 20 or more replies), `drip_feed`, `like_only_burst`, `count_drop`, `unexplained_step` (a step with no large replier at that moment, when replies were sampled), `view_surge_low_conversion` (view rate 5 times the earlier median while engagement per view falls below 0.3 times the rate so far) and `slow_start` (below the author's median at 60 minutes, when supplied).

`drip_feed` uses the variance-to-mean ratio of raw increments over 16 regular polling intervals after the second hour. A Poisson process gives about 1, organic engagement more, and delivery at a fixed rate near 0. Against 300 simulated Poisson tracks with 15-minute polling, the 0.15 cutoff fired on none of the 14-hour tracks and 2 of the 48-hour tracks; a 12-interval window at 0.2 fired on 50. It runs on raw snapshots only, because interpolation across a long gap makes increments look perfectly regular.

`harness/context/decay.py` fits the post-peak power-law exponent, pooled over an account's posts (each post keeps its own amplitude), by Poisson maximum likelihood, and maps it to Crane and Sornette's classes: sudden onset with exponent near 1.4 is exogenous subcritical, near 0.6 exogenous critical, and gradual onset near 0.2 endogenous critical. The fit recovers the exponent to 0.001 on exact synthetic counts. It needs about 50 post-peak events per account, so it waits on tracker data.

Not built from the red-team taxonomy: author-fueled threads (the author replying to keep the thread alive), synchronised replies by scan statistic, event-locked onsets against ESPN plays or keynotes, and converting delays to audience-activity time (the TiDeH correction).

### Tracker

`python -m harness.context track watch <handles>` and `track loop` poll on a log-spaced schedule by default: 5, 15, 30, 60 and 120 minutes, then 4, 8, 24, 48 and 168 hours, recording views every time. That is 10 requests per post per week. Early speed predicts final size (Szabo and Huberman 2010; Cheng et al. 2014), so the early points matter most. `TrackerParams(dense=True)` polls every 5 to 15 minutes for the first day, about 168 requests per post, and is the only mode fine enough to see a step jump on the 15-minute grid; use it for the brand's own posts. `track report <id>` adds minutes to the 1st, 5th and 10th reply and the like-per-view rate in the first 2 hours, the number X's cold-start code uses for authors under 50,000 followers.

A watch pass reads each watched timeline every 15 minutes and starts tracking any post under 6 hours old, so a post is caught near its start. With a 1 request per second budget shared with Observe, the log schedule supports hundreds of tracked posts. A live loop ran tonight on @CaitlinClark22, @IndianaFever, @WNBA and @AtlantaDream.

## First analysis: Super Viral against Deadweight

`python -m harness.context analyze` writes every number below to `data/context/analysis.json`. Rows are restricted to `label_eligible` (mature, not a thread continuation): 93 Super Viral, 372 Medium Viral and 363 Deadweight.

The short version: the account decides almost everything, the CSV's construction exaggerates that, and the content effects that survive a within-account comparison are small, mostly about format, and come from 18 accounts.

### Selection effects come first

- 718 of 1,133 Deadweight rows are thread continuations. Left in, "what makes a post fail" learns "being tweet 4 of a thread".
- The missing band is most of what accounts post. On the timelines fetched so far, 258 of 487 original posts in the window (53%) have engagement 11 to 99, and 257 of those are absent from the CSV. Recomputed categories for every timeline post are in `timeline_posts.csv` (`label_category_x`).
- 25 of 91 accounts have a Super Viral post, ten of them hold 79% of all Super Viral rows, and no "Smaller/closest" account has one.

### The author is the floor every model has to beat

The author's past-only median engagement (`pre_author_prior_median_eng`, at least 5 earlier posts) separates eligible Super Viral from Deadweight posts at AUC 0.99 (66 against 248 posts) and any viral post from Deadweight at 0.986 (322 against 248). The share of the author's earlier posts that reached 100 does 0.992. Follower count at fetch time does 0.947. With thread continuations left in, the same feature scores 0.909 (66 against 549), close to the red-team pass's 0.90 on the raw CSV. Continuations cluster in a few accounts (Resend alone has 150), so dropping them changes which accounts the Deadweight rows come from. Part of the remaining gap is the CSV's construction: with the middle band gone, an account's posts are either its tails or nothing, which makes accounts look more separable than they are. Eval should report every model against this floor, on `label_lift_prior`, with time-ordered splits.

### Content effects within an account are small

Only 5 accounts have both an eligible Super Viral and an eligible Deadweight post, too few to say anything. 18 accounts have both Medium Viral and Deadweight posts, so the within-account numbers below compare those (mean difference, Medium minus Deadweight, averaged over accounts, with a 95% interval from resampling accounts).

| Feature | Within-account difference | 95% interval | Pooled AUC (Super Viral vs Deadweight) |
| --- | --- | --- | --- |
| Announcement wording (`pre_hook_announce`) | +21 points of share | +9 to +35 | 0.58 |
| Length (`pre_n_chars`) | +81 characters | +28 to +133 | 0.42 (shorter) |
| Links in text (`pre_n_links_text`) | -0.25 per post | -0.49 to 0.00 | 0.37 |
| Quote post (`pre_is_quote_post`, 17 accounts) | -39 points of share | -57 to -22 | 0.52 |
| Event or webinar promo (`pre_hook_event`) | -6 points of share | -10 to -2 | 0.45 |
| Own posts in the previous 2 hours | -0.36 posts | -0.62 to -0.15 | 0.54 |
| Local hour | -2.3 hours | -4.0 to -0.7 | 0.64 |
| Has media (`pre_has_media`) | +16 points of share | -7 to +38 | 0.57 |
| HN story naming the account in the previous 24 hours | +0.08 stories | -0.01 to +0.19 | 0.64 |

Read these with care:

- Length flips sign between the pooled and the within-account view: big accounts post shorter, so pooled data says "short wins" and the same account says "longer wins". Any pooled content finding here can be an account finding in disguise.
- Quoting someone else's post is the clearest within-account loser: lower in 11 of 17 accounts, tied in 5, higher in 1. A plausible reason is that the quoted post takes the attention, but this data cannot tell.
- A post that follows the account's own post within 2 hours does worse: lower in 11 of 18 accounts, tied in 7, higher in none. That matches the author-diversity decay in X's 2026 ranker (repeat posts in a batch multiplied by 0.75 times 0.5 to the k, plus 0.25), but bursts of low-effort posts would look the same.
- Hour of day: pooled it looks strong, within accounts it points the other way (Medium Viral posts earlier), and the eval worker found no within-account signal (pair accuracy 0.499). Treat it as context, not a lever.
- Media and HN attention do not clear zero within accounts. Their pooled strength is mostly which accounts use them.

Against the within-author lift (`label_lift_prior`, 570 eligible posts with a prior), no content or timing feature reaches a Spearman correlation of 0.14 in absolute value. The strongest are event promos (-0.13), scheduler round minutes (-0.11), emoji count (-0.09) and links (-0.08). That fits Martin et al. (2016): content explains a few percent of the spread once the author is known.

### Super Viral is distribution, not conversion

These are outcomes, so they describe what happened rather than predict it. Median views per follower: Super Viral 1.08, Medium Viral 0.24, Deadweight 0.042. Median engagement per view: 1.22%, 1.17% and 0.58%. Super and Medium Viral posts convert viewers at the same rate. Super Viral posts reached 4.6 times as many people per follower. Deadweight posts were both shown less and converted half as well. So the lever that separates a hit from a solid post is getting distributed (reposted by larger accounts, picked up by the ranker), and the reposter features are where to look next.

### Boost flags

24 posts are `boost_paid_or_external_reach`: 7 Super Viral, 11 Medium Viral and 6 Deadweight. They include Supabase's Grok Bot post (1.1M views against a typical 7k to 13k) and CockroachDB's "cockroaches will outlast us all" (4.5M views, 13.8k followers). None is `boost_likely_inauthentic` at this commit, because the reply and reposter samples that supply most votes are still being fetched; the count-ratio votes alone rarely reach two.

### What is small-sample or confounded

- Every within-account number rests on 18 accounts, and Super Viral against Deadweight on 5. Intervals are wide.
- Timeline coverage is partial at this commit (about 20 of 91 accounts), so the author baselines for the rest fall back to CSV posts, whose missing band biases them upward.
- Followers, views and bookmarks are fetch-time values. Followers are features only with that caveat.
- The keyword hooks are crude. "Announcement" means the text matched one of 20 phrases.

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
