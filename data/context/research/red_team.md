# Context and engagement dynamics: research and red-team

This is for the context-dynamics worker (`sam/context-dynamics`) and the lead who relays to it. Findings are ranked by how much they change what the worker should build. Code references point at the worker's tree as of commit `172ad8a` plus its uncommitted files at about 02:30 UTC on Oct 16, so line numbers may drift.

"Verified" below means I ran the check myself or read the source. "Agent-verified" means a research subagent read the paper or code this session and I did not re-read it. "Recalled" means nobody opened the source. Scratch scripts and raw responses are in `/tmp/8vc-research/context/`. The subagents' full notes, with every citation, are `lit_classic.md`, `lit_deep_recent.md`, `lit_inauthentic.md` and `features.md` in that folder. `/tmp` gets swept, so copy anything worth keeping.

## Ranked findings

### 1. The labels are a filter on the outcome, and account identity explains most of them

- **The middle of the distribution is missing.** `Category` is a pure threshold on `Engagement`: Deadweight is 0 to 10, Medium Viral 100 to 749, Super Viral 753 and up. The file has no posts with engagement between 11 and 99. Supabase's own timeline (the worker's fxtwitter cache) holds 17 posts in the window with engagement 11 to 99, and none of them are in the CSV. For Supabase that's about 60% of its original posts dropped. Verified.
- **Account identity predicts the label.** Measured on the CSV:
  - Between-account differences explain 58% of the variance in log engagement.
  - The author's leave-one-out share of non-Deadweight posts predicts a post's label at AUC 0.90. Using only that author's earlier posts gives 0.90 too.
  - Log follower count alone gives AUC 0.83 for non-Deadweight and 0.83 for Super Viral.
  - 66 of 91 accounts have no Super Viral post, and ten accounts hold 79% of them. "Smaller/closest" accounts have none.

  Verified (`/tmp/8vc-research/context/prof.py`, `prof2.py` and the AUC script).
- **The literature agrees.** Martin et al. (WWW 2016) found content alone explains under 5% of cascade-size variance, while the author's past success reaches R² of about 0.42 to 0.48. Tan, Lee and Pang (ACL 2014) got only 61 to 66% on same-author, same-URL pairs. Agent-verified from the PDFs.
- **Recommendations:**
  - Make the training target the post's lift over the author's own baseline, computed from the full author timeline rather than the CSV rows. `harness/context/backfill.py` already fetches timelines.
  - Recompute each post's category from timeline counts so the 11 to 99 band comes back.
  - Publish the author-prior AUC (0.90) as the floor that every model and the eval team's backtest must beat.
  - Tell eval to score content with same-author pairs close in time, split by time and never at random. Arxiv 2510.25348 (agent-verified) shows random splits are what make the deep cascade models look good.

### 2. Paid reach shows up as views without engagement, and `boost.py` checks the opposite tail

- **X counts promoted engagement in public counts.** Public metrics on a promoted post are the "combined total (organic + promoted)" ([X API metrics docs](https://docs.x.com/x-api/fundamentals/metrics), agent-verified). So promotion inflates views far more than engagement.
- **The current rule flags memes.** On the worker's own `x_metrics.jsonl` (1,000 rows), the `like_rate_high` rule (likes per view above the 99th percentile, `boost.py:34`) flags exactly six posts:
  - Supabase "Love that guy" (10.2% likes per view)
  - Zed "Timeline today be like"
  - Supabase ".btn-large"
  - three Bun or Zed image posts

  All six are organic memes. Verified.
- **A views-based rule catches launches instead.** Flag posts with views at least 5 times the author's median views and engagement per view at most 0.4 times the author's median, among authors with 5 or more posts. It fires on 17 of 664 posts. Verified; the 5x and 0.4x cutoffs are mine and uncalibrated.

  | Account | Post | Engagement | Views | Author followers |
  |---|---|---|---|---|
  | CockroachDB | "They say cockroaches will outlast us all" | 651 | 4.54M | 13.8k |
  | ToolJet | "One prompt. Three AI app builders" | 6 | 62k | 2.0k |
  | Prisma | "Early bird tickets won't be around much longer" | 8 | 92k | 79k |
  | Qodo | "Introducing Qodo 3.0" | 157 | 414k | 19.8k |
  | Supabase | "Supabase now works with Grok Bot" | 949 | 1.10M | 212k |

  For comparison, Supabase's typical post gets 7k to 13k views.
- **Big accounts look the same.** A repost by a very large account also brings views at a low conversion rate, so this rule detects "reach the brand's audience didn't earn". It doesn't detect bots.
- **Recommendations:**
  - Add `paid_or_external_reach` with this rule, measured against the author's baseline rather than the whole dataset.
  - Demote `like_rate_high` to a weak signal, or drop it.
  - Report three separate labels, following `lit_inauthentic.md`: paid reach, coordinated but plausibly authentic (staff or community push), and likely inauthentic.
  - Stop counting `author_outlier` (`boost.py:113`) as one of the two votes behind `boost_suspect`. Launches are supposed to beat the author's median, so the outlier test should only decide which posts get the other checks.

### 3. Many "Deadweight" rows are thread continuations, not failed posts

- **The numbers.** 415 of the 1,133 Deadweight rows follow the same account's previous post within 120 seconds, against 19 of 431 Medium Viral and 0 of 101 Super Viral. In my 143-post re-read, 50 of 86 Deadweight posts were self-replies. 60 more Deadweight rows start with `@`, which makes them replies to others. Verified.
- **What the worker has.** It already flags `is_thread_continuation` (gap under 180 seconds) and `is_self_reply`.
- **Recommendation:** Exclude these rows from labels, from author baselines (`_account_stats` at `enrich.py:57`) and from any Deadweight analysis, instead of only flagging them. Otherwise "what makes a post fail" learns "being tweet 4 of a thread".

### 4. Leakage and fetch-time columns in `devtools_enriched.csv`

Each item below gives a column, the problem, and the fix.

- **`other_accounts_super_viral_same_day` (`enrich.py:196`).** It's built from the labels of posts made the same day, including later ones. That's outcome data. Drop it as a feature.
- **`dataset_posts_same_day` (`enrich.py:195`).** It measures how the CSV was sampled, not what was happening. Thin days have inflated medians: 2026-09-27 has 10 rows with median engagement 224, and 2026-10-10 has 10 rows with median 190.5, against a typical 4 to 8. A model will learn "weekend means viral". Verified. Drop it.
- **`author_posts_same_day` (`enrich.py:193`).** It counts posts later that day, and the CSV is missing the 11 to 99 posts. Replace it with "own posts in the 2 hours before", taken from the timeline. That's also the window that matters for X's author-diversity decay (item 8).
- **`eng_ratio_author_median`, `above_author_median` and the `author_outlier` vote (`enrich.py:237`, `enrich.py:239`).**
  - The median covers the whole window, including the target post and later posts.
  - The dataset fallback, `median_eng_dataset` (`enrich.py:69`), uses the selected sample. For Supabase that's 190 against roughly 90 on the timeline.
  - The fix: keep a whole-window lift as a label column, and add a past-only rolling `author_prior_median_eng` as the feature. Exclude thread continuations from both.
- **`mentions_x_trend_same_day` (`enrich.py:248`) and `hn_stories_pm24h` (`enrich.py:117`).** Same-day trends and stories within 24 hours either side include ones the post caused. The Deno HN story went up 2 minutes after the Deno post (agent-verified via Algolia). Keep only "strictly before the post". Move the after-post counts to an `outcome_spillover_*` group.
- **`author_followers`, `eng_per_1k_followers` (`enrich.py:222`), `x_views` and `x_bookmarks`.** These are read at fetch time. A viral post grows the follower count it's normalised by. Treat views and bookmarks as outcomes. Treat followers as a feature only for posts more than a few days old, and note the direction of the bias.
- **`scrape_time_est` (`enrich.py:155`).** It uses the newest post time, 00:48:27 UTC. The CSV was committed at 01:07:58 UTC (`git log c9f8904`), so the scrape falls between those two times. Pass 01:07:58 as `scrape_time`.
- **Immature labels.** 22 rows were under 6 hours old at the scrape and 115 under 24 hours. The worker's `label_mature` (48 hours) is right; make the eval team exclude immature rows. Pfeffer et al. 2023 found the median tweet gets half its views in about 80 minutes (agent-verified).
- **Recommendation:** split the enriched columns into two named groups, `pre_*` (known at post time) and `outcome_*` or `label_*`, so a model can't mix them by accident.

### 5. Reply timestamps are capped at about 35 per sort order and biased early

- **The cap.** `/2/conversation` returns about 35 replies, and cursor pages return 404. The worker found this too (`xreader.py:176`).
- **What the worker misses.** fxtwitter also accepts `ranking_mode=recency` (verified from [FxEmbed `routes.ts`](https://github.com/FxEmbed/FxEmbed/blob/main/src/realms/api/routes.ts) and live). That returns the newest 32 or so, a second, independent sample.
- **The default sort puts early replies first.** On Deno's post, the likes-ranked page puts 97% of its replies within 24 hours. The newest-first page shows replies still arriving at 92 hours, and about half of those 32 arrived after 24 hours. Verified (`replies_an.py`).
- **Consequences for the worker's code.** Early replies collect more likes, so they rank first:
  - `curve_from_event_times` (`curves.py:228`) would call most reply curves `early_spike`.
  - `reply_burst_share` (`boost.py:81`) is inflated.
  - `reply_low_follower_share` is understated. X also ranks Premium replies higher and hides likely spam (agent snippet, unverified).
- **Coverage.** The two pages together cover every reply on posts with up to about 65 replies. That's 99% of Medium Viral and 51% of Super Viral posts. Verified.
- **Recommendations:**
  - Fetch both sort orders and merge them.
  - Store `reply_coverage = unique replies sampled / x_replies`.
  - Fit curves and burst statistics only when coverage is at least 0.8. Otherwise say "partial sample".
  - End event-time curves at the fetch time, not at the last reply, so a post whose replies stopped still covers its full duration.

### 6. The retweeter list is available, ordered by follower count

- **The endpoint.** `GET api.fxtwitter.com/2/status/{id}/reposts?count=100` works and paginates. Verified live on Deno: 2 pages of 99 users, each with followers, following, statuses, joined date and verification.
- **The ordering.** Results come back roughly in descending follower order: Matthew Prince, Cloudflare's CEO, with 346k first, then about 1.5k by entry 198. I inferred that ordering from one post.
- **The limits.** Reposts carry no timestamps. `/2/status/{id}/quotes` and `/2/search` return 404.
- **Why it matters.**
  - It gives the amplifier list that Goel et al. 2016 ("popularity tracks the size of the largest broadcast") and Bakshy et al. 2011 depend on. Both agent-verified.
  - It gives an actor population for bot rule R9 (account age, follower ratio), which `boost.py` currently approximates from repliers only.
  - It can explain a `step_jump` that no reply explains.
- **Recommendations:**
  - Add `XReader.reposters(post_id, max_pages)`.
  - Add the features `max_reposter_followers`, `top10_reposter_followers_sum`, `reposter_staff_share` (a bio mentioning the brand) and `reposter_new_account_share`. The last needs the full tail, so paginate to the end for posts under about 1,000 reposts.
  - Compare each against the author's other posts, not against a global cutoff.

### 7. Inflection taxonomy with detection rules

The worker's six shapes and five flags (`curves.py:1-35`) are a sound start. The table merges them with the literature and marks what's missing. Regimes: S means snapshots, R means reply or quote timestamps, F means final counts only.

| Type | What produces it | Detection rule | Regime | Source | In worker code? |
|---|---|---|---|---|---|
| Exogenous subcritical | Outside shock, little word of mouth | Sudden onset, peak bin over 80% of total; decay exponent near 1+θ (about 1.4) | R, S | Crane and Sornette 2008, agent-verified | Partly (`early_spike`) |
| Exogenous critical | Outside shock that word of mouth carries | Sudden onset; slow power-law decay near 1−θ (about 0.6); peak share 20 to 80% | R, S | Same | No |
| Endogenous critical | Built by sharing alone | Power-law growth before the peak, near-symmetric decay near 1−2θ (about 0.2) | R, S | Same; Sornette 2004 | No (closest is `slow_burn`) |
| Endogenous subcritical | Noise | No peak above baseline | R, S | Same | `unclassified` or `insufficient` |
| Delayed takeoff | Algorithmic pickup, or a later share by a big account | Quiet first hour, rise to a peak 3 hours or more later | S, R | Yang and Leskovec 2011 shape 3 | Yes |
| Step jump | Big account quotes or reposts | One bin holds 25% or more of the total, with an abrupt onset | S | Goel 2016 | Yes; explain it with item 6's reposters |
| Second wave | News pickup, HN front page, newsletter | Two peaks 3 hours or more apart with a deep trough | S, R | Yang and Leskovec shapes 4 and 5 | Yes (`multi_wave`) |
| Circadian echo | The audience wakes up again | A second peak 20 to 28 hours after the first, at the audience's local morning | S, R | Kobayashi and Lambiotte 2016 (TiDeH); Yang and Leskovec shape 4 (second peak about 24 hours later) | **No, and `multi_wave` will mislabel it as news** |
| Slow burn or evergreen | Reference content | No sharp peak; half the engagement after 6 hours; many bookmarks | S | Yang and Leskovec shape 6 | Yes |
| Stillborn | Nobody saw it | Engagement in the first hour below the author's median for that hour | S, R | Szabo and Huberman 2010; Shulman 2016 | No (`insufficient` merges "tiny" and "dead") |
| Controversy ("ratio") | Dunking or disagreement | Replies plus quotes at least equal to likes, with 20 or more replies | F | Minot 2021, agent-verified | Yes (`reply_ratio`) |
| Author-fueled | Author keeps replying in the thread | Author replies make up 20% or more of early replies; the reply rate rises after each | R | 2023 ranker weight of 75 for "reply engaged by author", gone from the 2026 code | No |
| Paid reach | Promoted impressions | A views jump with engagement per view at 0.3 times the earlier rate or less | S (F version in item 2) | X metrics docs; HIP (Rizoiu 2017) | **No; no flag uses views** |
| Drip feed | Bought engagement delivered at a fixed rate | Variance-to-mean of increments 0.15 or less over 16 intervals | S | De Cristofaro 2014; Ikram 2017 | Yes (`drip_feed`) |
| Like-only burst | Bought likes | Likes jump with no other metric moving | S | Same | Yes |
| Count drop | X removing fake engagement | A count falls 5% or more from its peak | S | Same | Yes |
| Synchronised replies | Engagement pod or bot ring | Burst of reply gaps under 2 seconds (scan statistic against a fitted decay); near-duplicate texts | R | Ghosh 2011; Keller 2020; Schoch 2022 | Partly; exact-duplicate text only (`boost.py:79`) |
| Event-locked | A game moment or a launch event | Onset within 15 minutes of an ESPN play or a scheduled keynote | R, S plus context | Lehmann 2012 reactive class | No |

Recommendations for `curves.py`:

- **Circadian guard.** Before calling `multi_wave`, check whether the second peak falls 20 to 28 hours after the first, or at the audience's local morning peak. If it does, label it `circadian_echo`. A better fix is to convert delays to "audience activity time" using an hour-of-week profile built from every reply timestamp collected (the TiDeH approach, agent-verified).
- **Views flag.** Add `view_surge_low_conversion`, built from `Snapshot.views`. Views are already stored, but no flag reads them.
- **Exogenous versus endogenous.**
  - Fit the decay exponent after the peak by Poisson maximum likelihood, pooled per account. Single posts rarely have the roughly 50 events a fit needs.
  - Assign the nearest Crane and Sornette class.
  - Fit on quotes where you can. Quotes reach the quoter's followers the way retweets do; replies mostly don't (recalled).
- **Separate stillborn from insufficient.**

### 8. Optimise for X's 2026 ranking objective

- **The 2026 weights.** [xai-org/x-algorithm](https://github.com/xai-org/x-algorithm), `home-mixer/params/param.rs` lines 320 to 432. Verified by reading the file.

  | Action | Weight |
  |---|---|
  | Favorite | 0.66 |
  | Retweet | 1.0 |
  | Reply | 5.0 (plus a boost between mutual follows) |
  | Quote | 5.0 |
  | Share | 2.0 |
  | Share via DM | 5.0 |
  | Share via copy link | 20.0 |
  | Follow author | 4.0 |
  | Dwell | 0.05 |
  | Not interested | −52.3 |
  | Mute | −58.8 |
  | Block | −31.2 |
  | Report | −234 |

  The file's own comment says these multiply predicted probabilities, not raw counts.
- **The 2023 weights.** Reply 13.5, reply engaged by author 75, like 0.5, report −369 ([the-algorithm-ml README](https://github.com/twitter/the-algorithm-ml/blob/main/projects/home/recap/README.md), agent-verified).
- **Mechanisms in the same repo** (agent-verified; I didn't re-read these files):
  - `author_cold_start.rs` gives original posts a chance at a mid-feed slot (about position 16) when the author has at most 50k followers, the post is under 2 hours old and it has under 200 home views. It samples a Beta(0.75 + likes, 49.25 + views − likes) like-per-view rate, a 1.5% prior.
  - Repeat posts by one author in a ranked batch are multiplied by 0.75·0.5^k + 0.25.
  - Grok-based labels (`SpamHighRecall` for engagement bait, `FosnrAbuseInsults` for targeted insults) remove a post from non-followers' feeds.
- **Recommendations:**
  - Add `x_weighted_engagement = 0.66·likes + reposts + 5·replies + 5·quotes` as an outcome column next to raw engagement.
  - Have the tracker log views and likes at about 15, 30, 60 and 120 minutes for every account under 50k followers. The like rate per view in those first 2 hours is the number X itself uses to promote small authors.
  - Pass the insult and engagement-bait findings to the Action worker. The fan account's rivalry banter should mention the rival without insulting anyone (item 10).

### 9. Early speed predicts final size; design the snapshot schedule around it

Sources, all agent-verified from the PDFs:

- **Szabo and Huberman 2010.** Log final popularity is linear in log early popularity, with the slope near 1.
- **Cheng et al. 2014.** After 5 reshares, whether a cascade doubles is predictable at AUC 0.877, and timing features alone come within 0.025 of that.
- **Shulman et al. 2016.** The time to the first few adopters dominates. Once early speed is matched, content predicts below 65%.
- **SEISMIC (Zhao et al. 2015)** works on arrival times plus follower counts. Use quotes as the stand-in for retweets. Its exposure term is wrong for replies.

Recommendations:

- Snapshot on a log-spaced schedule: 5, 15, 30, 60 and 120 minutes, then 4, 8, 24, 48 and 168 hours. Record views every time.
- Store `t_to_kth_reply` for k = 1, 5 and 10, plus reply velocity in the first 30 and 60 minutes weighted by log follower count.
- Normalise every early count by the author's median at the same age.
- Expect an R² of 0.5 at most before posting (Martin 2016). Higher means leakage.

### 10. Features Sam didn't list, ranked by evidence

Each entry gives the feature, its evidence, whether it's known at post time, and its verification status.

1. **Lift over the author's prior baseline.** Martin 2016, Bakshy 2011. Known at post time. Agent-verified. See item 1.
2. **Like rate per view in the first 2 hours.** X's cold-start code (item 8). Known only live; leaks the outcome if measured afterwards. Agent-verified.
3. **Largest early broadcaster.** Goel 2016. Covers the biggest replier, quoter and, now, reposter (item 6). Early-window only. Agent-verified.
4. **Mentions the rival without insulting anyone.** Out-group words raised share odds about 67% per word (Rathje et al., PNAS 2021). A targeted insult triggers X's `FosnrAbuseInsults` label and loses non-follower reach. Known at post time. Agent-verified. This matters directly for tonight's fan account.
5. **Engagement-bait phrasing.** It gets `SpamHighRecall`, which removes the post from non-followers' feeds. Known at post time. Agent-verified from the code; the prompts behind the label are withheld.
6. **Own posts in the previous 2 hours.** X's author-diversity decay (item 8). Known at post time. Agent-verified.
7. **Shareability.** Copy-link share carries weight 20. Berger and Milkman 2012 found practical value raised virality 30% per standard deviation. Bookmarks per like is the visible proxy, but it's an outcome. Agent-verified.
8. **Arousal and moral-emotional words.**
   - Berger and Milkman: anger +34% and awe +30% per standard deviation.
   - Brady 2017: +20% per moral-emotional word.

   Known at post time. Agent-verified.
9. **Scheduler-posted.** 12% of posts land within the first 30 seconds of a quarter-hour mark. Those have half the non-Deadweight rate of the rest (17% against 34%). The worker's `round_minute` already captures this. Verified, but confounded by account and by thread tails. Known at post time.
10. **Audience local hour and live-event window.**
    - X's ranker takes the viewer's local hour as an input.
    - For sports, compute the game window from the ESPN scoreboard by date.
    - For dev tools, the post time against a scheduled keynote.

    Known at post time. Agent-verified.
11. **Outside attention before the post.** Wikipedia daily pageviews against a 14-day median; HN stories from the previous 24 hours. Known at post time if filtered to "before". The link to X engagement is unmeasured.
12. **How much a draft reads like LLM text.** Grox computes a `slop_score`, and accounts labeled `llm_slop_user` get `SpamHighRecall` for 7 days. The harness writes its drafts with an LLM. Agent-verified; the weight isn't public.
13. **A displayed Community Note.** It cut reposts 62% (Chuai 2024). An outcome. Agent-verified.
14. **Outbound links.** Musk said in July 2026 that links haven't been penalised "for over a year". Buffer 2026 still finds link posts lowest on X (2.25% against 3.56% engagement), and the public 2026 code has no link penalty. Treat it as a format effect, not a rule. Agent-verified reporting.

### 11. Historical context sources tested without keys

Tested at 01:30 to 02:10 UTC on Oct 16 against Deno (2026-10-09 13:01Z) and Caitlin Clark. Agent-verified; raw responses in `/tmp/8vc-research/context/sources/raw/`. The main rule: counts from these services are today's values. Only Wayback snapshots, old Wikipedia revisions, Google Trends buckets and Polymarket prices show the state at the past time.

| Rank | Source | Resolution and reach | Result |
|---|---|---|---|
| 1 | Google Trends explore and multiline (unofficial) | 8 minutes for 7 days back; hourly for 28 days | Worked. Deno went 0 to 26 between 12:48 and 12:56Z. Google tags these requests as a scraper, so expect breakage. |
| 2 | HN: Algolia `search_by_date`, `news.ycombinator.com/front?day=`, Wayback of the front page | Seconds; full history | Worked. Wayback showed Deno at #1 with 67 points at 13:25Z. Captures are dense Oct 2 to 11 and sparse otherwise. |
| 3 | Bluesky `searchPosts?since=&until=` | Seconds; at least 28 days | Worked. 101 hits in 4 hours. `public.api.bsky.app` returned 403. |
| 4 | Wikipedia revisions and `Portal:Current_events` old revisions | Seconds; full history | Worked. Shows what the page said at the time, without hindsight. Mainstream news only. |
| 5 | Google News RSS with `after:` and `before:` | Day filter, minute timestamps | Worked. The filter leaks; re-filter on publish time. |
| 6 | Wikipedia pageviews | REST is daily only; hourly dumps are about 60 MB per hour, about 3 hours behind | Worked. `hourly` granularity returns 400. |
| 7 | ESPN scoreboard `?dates=YYYYMMDD` | Per date | Worked. Date ranges return 400. The news endpoint has no date filter. |
| 8 | Polymarket price history | Minutes | Worked where a market exists. |
| 9 | Product Hunt daily leaderboard; Lobsters JSON | Daily | Worked. Low volume. |
| 10 | trends24 | Live page covers 24 hours only | US pages for October are 404 on Wayback; worldwide is parseable when a capture exists. Partial. |
| 11 | npm and PyPI downloads | Daily, days behind | Worked; too coarse. |
| 12 | GDELT raw 15-minute files | 15 minutes | Worked, but had no Deno rows. GDELT misses dev-tool news. |
| Failed | GDELT DOC 2.0 API | | 429 on the first request and on a retry 20 minutes later. |
| Failed | GitHub stargazers | | Now 401 without a token. |
| Failed | getdaytrends | | 520. |
| Failed | archive.twitter-trending | | Encrypted POST. |
| Failed | Reddit | | 403. |

Recommendations:

- **GDELT.** The worker's `collectors/gdelt.py` calls the DOC API at 6-second spacing, and this IP got 429 on the first request. Expect it to fail. Fall back to the raw 15-minute files, and don't rely on GDELT for dev tools.
- **trends24.** `collectors/x_trends.py` reads it from Wayback, which only covers worldwide trends for October.
- **Collector priority.** Add Google Trends 8-minute buckets for the 7-day window and HN via Wayback as point-in-time sources. Prefer Bluesky and HN over GDELT for "what developers were discussing".

### 12. Deep cascade models and LLM judges

- **The deep models don't fit this data.** DeepCas, DeepHawkes, CasCN, CasFlow, CasDO and their successors all need retweet trees and tens of thousands of cascades. Under time-ordered splits, an MLP on handcrafted features beats them (arxiv 2510.25348, revised Sep 2026). Agent-verified.
- **LLM persona panels made predictions worse.** On Upworthy tests with a real winner, a plain LLM ranker got 49% top-1 and a 10-persona panel 34.6% (arxiv 2609.25010). Persona agents predicting individual reactions scored MCC 0.29, below TF-IDF at 0.36 (Bojić 2026). Agent-verified.
- **What does work:** boosted trees or tabular models on author features plus frozen embeddings, with LLM rationales added as features. That won SMP Challenge 2025, and BuzzProphet (CIKM 2025) used the rationale approach. Agent-verified.
- **Recommendation:** pass this to the Eval and Action workers and to Dhilan's judge. Use LLM output as features, and treat a pairwise LLM verdict as one weak signal.

### 13. Smaller checks

- **fxtwitter counts are fresh.** Views moved from 1,097 to 1,106 within 75 seconds, with no cache headers. So `drip_feed` and `count_drop` aren't caching artifacts, at least in this one test. Verified.
- **`Engagement` includes quotes.** Engagement minus likes, replies and retweets equals X's quote count: 618 on Deno, 25 on Supabase. Verified, and the worker verified it too.
- **Counts barely moved between the scrape and my re-read 40 minutes later.** Only two of 143 posts changed category, and both were under 10 hours old. That says nothing about final values. Re-run `/tmp/8vc-research/context/drift.py` after 48 hours to measure maturation.

RESULT: done

All five questions are covered. Gaps:

- Several thresholds marked as mine are uncalibrated.
- The reposter ordering rests on one post.
- The GDELT DOC API couldn't be tested because it returned 429.
- Some feature effects (link penalty, Premium reach) rest on secondary reporting.

The three additions that matter most:

1. **Retarget on within-author lift from full timelines.** Restore the dropped 11 to 99 band, exclude thread continuations, and hold every model to the author-prior baseline of AUC 0.90.
2. **Add the views-based paid-reach flag, and stop flagging memes as boosts.** Use views at 5 times the author's median with engagement per view at 0.4 times or less. Split boost output into paid reach, coordinated-authentic and likely inauthentic.
3. **Collect replies and reposters properly.** Fetch replies in both sort orders with a coverage column, and add the `/reposts` reader for amplifier size and reposter accounts. Then fix `curves.py` so a next-day circadian peak isn't read as news, and flag view surges that don't convert.
