# Engagement features beyond the X API: evidence notes

Date of research: 2026-10-15. Read-only. Clones used: `/tmp/8vc-research/context/src/x-algorithm` (xai-org/x-algorithm, HEAD `92f0918f`, committed 2026-10-15T00:55Z) and a sparse `/tmp/8vc-research/context/src/the-algorithm` (twitter/the-algorithm, HEAD `c54bec0d`, 2025-09-03).

Tags: **[verified]** means I opened the source or the code and read the number. **[secondary]** means I read only a news write-up or a search summary of the primary source. **[recalled]** means from memory, not checked this session.

Input codes used below:
- `T`: post text
- `M`: media type
- `TS`: post time from the Snowflake ID
- `F`: final likes, replies, retweets and quotes
- `C`: current counts, including bookmarks and views (the fetch-time snapshot)
- `A`: author followers, following, statuses and join date (fetch-time snapshot)
- `H`: the account's recent timeline
- `R`: the reply list, with each reply's arrival time and author follower count

Timing codes:
- **PRE**: known before posting.
- **EARLY**: observable in the first minutes or hours. It is reconstructable only from timestamped inputs, which today means `R`.
- **FETCH**: known only at fetch time. Using it as a predictor of the same post leaks the outcome.

---

## 0. Leakage you have to fix before any feature matters

1. **`A` is a fetch-time snapshot.** Follower and statuses counts were read after the post. A launch that went viral added followers, so `followers_at_fetch` partly encodes the outcome. Fix: estimate followers at post time. One option is to interpolate between snapshots across the timeline. Another is to treat `A` as valid only for posts that are old relative to the snapshot, and flag the rest. `statuses_count` at post time can be rebuilt as `statuses_now − (number of the account's posts in H newer than the target)`. That works only if H is complete back to the target.
2. **`H` must be filtered to posts with Snowflake time earlier than the target's.** Every "author history" feature below assumes this.
3. **`C` mixes ages.** Views and bookmarks fetched 3 hours after posting and 3 months after posting are different quantities. Every rate needs `post_age_at_fetch = fetch_time − TS`. Do not compare raw counts across ages. X's For You `AgeFilter` drops posts older than 48 h (`home-mixer/filters/age_filter.rs`; README pre-scoring filter table) [verified]. Most For You distribution is therefore over by 48 h, but views keep accruing from search, profile visits and embeds.
4. **The X view count is not the same as "home impressions".** X's cold-start code uses `view_count_on_home` (see feature 4). The public view count includes every surface. Treat likes per public view as a noisy proxy.

---

## 1. X ranker weights (exact numbers)

### 1a. 2023 heavy ranker: twitter/the-algorithm-ml [verified]

File: `projects/home/recap/README.md`, https://github.com/twitter/the-algorithm-ml/blob/main/projects/home/recap/README.md. The README says "the current weighting of probabilities (April 5, 2023)".

```
scored_tweets_model_weight_fav: 0.5
scored_tweets_model_weight_retweet: 1.0
scored_tweets_model_weight_reply: 13.5
scored_tweets_model_weight_good_profile_click: 12.0
scored_tweets_model_weight_video_playback50: 0.005
scored_tweets_model_weight_reply_engaged_by_author: 75.0
scored_tweets_model_weight_good_click: 11.0
scored_tweets_model_weight_good_click_v2: 10.0
scored_tweets_model_weight_negative_feedback_v2: -74.0
scored_tweets_model_weight_report: -369.0
```

- Score = Σ weight_i × P(engagement_i). The README says the weights were originally set so that each term contributes about equally, then "periodically adjusted ... to optimize for platform metrics".
- Same era, `HomeGlobalParams.scala` at twitter/the-algorithm commit `fb54d8b5` (2023-05-22): `home_mixer_blue_verified_author_in_network_multiplier` default 4.0 and `..._out_of_network_multiplier` default 2.0 [verified]. URL: https://raw.githubusercontent.com/twitter/the-algorithm/fb54d8b5/home-mixer/server/src/main/scala/com/twitter/home_mixer/param/HomeGlobalParams.scala

### 1b. Sept 2025 update to twitter/the-algorithm [verified]

- Commit `c54bec0d`, 2025-09-03, "update for-you recommendations code".
- `HomeGlobalParams.scala`, `object ModelWeights` (around line 786) lists the heads fav, retweet, reply, good_profile_click, video_playback50, video_quality_viewed(_immersive), reply_engaged_by_author, good_click(_v1/_v2), tweet_detail_dwell, profile_dwelled, **bookmark**, **share**, share_menu_click, negative_feedback_v2, report, weak/strong_negative_feedback, dwell, open_link, **screenshot**, video_watch_time_ms and dwell_0.
- **Every default is 0.0.** The public copy discloses the heads but not the weights.
- `ScoredTweetsParam.scala` in the same commit adds `scored_tweets_grok_slop_score_decay_value` (default 1.0), `out_of_network_scale_factor` 0.75 and `reply_scale_factor` 0.75. It also adds author-diversity decay 0.5 with floor 0.25 and Phoenix enable flags.
- It also adds a `BasketballContextFeatureHydrator`, which attaches live-game context to posts from a configured set of basketball team accounts. This is UI decoration for official team accounts, not fan accounts.

### 1c. 2026 Grok-based ranker: xai-org/x-algorithm [verified]

- History: first published 2026-01-20 [secondary: https://x.com/Engineering/status/2013471689087086804]. The Aug 13, 2026 release "Adds key configuration parameters (including weights ...)". The README says cron jobs set the in-repo defaults to "the primary production values".
- Repo: https://github.com/xai-org/x-algorithm. Commits are now near-daily (most recent 2026-10-15).

`home-mixer/params/param.rs` (lines 320–438):

```
FavoriteWeight            0.66
ReplyWeight               5.0
BidirectionalFollowReplyWeightBoost  15.0   (added to reply weight for ORIGINAL posts from MUTUAL follows)
BidirectionalFollowDwellWeightBoost   0.0
RetweetWeight             1.0
PhotoExpandWeight         0.05
VideoOpenWeight           0.02
ClickWeight               0.3
OpenLinkWeight            0.2
ProfileClickWeight        0.0
VqvWeight (video quality view) 0.0
ShareWeight               2.0
ShareViaDmWeight          5.0
ShareViaCopyLinkWeight   20.0
DwellWeight               0.05
QuoteWeight               5.0
QuotedClickWeight         0.05
QuotedVqvWeight           0.0
FollowAuthorWeight        4.0
PostUnexploredWeight      0.015
ReactWithVideoWeight      0.0
ContDwellTimeWeight       0.006   (multiplies predicted dwell time, not a probability)
ContClickDwellTimeWeight  0.4
NotInterestedWeight     -52.272
BlockAuthorWeight       -31.2
MuteAuthorWeight        -58.8
ReportWeight           -234.0
NotDwelledWeight         -0.02
```

- `vm-ranker/params.rs` holds a second copy whose defaults differ slightly: Favorite 0.5, VideoOpen 0.07, ContDwellTime 0.004, PostUnexplored 0.02, NotInterested −47.52. The README's "468 times" (234/0.5) matches this copy.
- `vm-ranker/params.rs` also sets post-fusion multipliers:
  - `rust_home_mixer_enable_author_diversity` true
  - `author_diversity_decay` 0.5
  - `author_diversity_floor` 0.25
  - `oon_weight_factor` 0.75
  - `topic_oon_weight_factor` 0.5
  - `new_user_oon_weight_factor` 0.00001
- `xai-value-model/scoring.rs`: `diversity_multiplier = (1−floor)·decay^k + floor`. The k-th extra post by the same author in one ranked batch gets ×1.0, ×0.625, ×0.4375, ×0.344 and so on, never below 0.25.
- X's own caveat (README and the comment in param.rs): the weights multiply predicted probabilities, not counts. The comment says "the baseline probability of a Report is more than 1000x lower than a Like". Only engagement on posts served in Home counts; "Directly navigating to a post ... has no ranking impact."
- The 2026 model has **no** `reply_engaged_by_author` head and **no** bookmark weight.
  - Bookmarks appear only as positive labels for retrieval training (`phoenix/xrex/configs/xrecsys_two_tower.py` lines 290 and 385, `CLIENT_TWEET_BOOKMARK`).
  - Profile click went from 12.0 (2023) to 0.0.
  - Report went from −369 to −234.
  - Copy-link share at 20.0 is now the largest positive weight.

### 1d. Other 2026 mechanics a feature model should mirror [verified, code paths in x-algorithm]

- **Phoenix inputs** (`phoenix/README.md` ~line 183). Context features are "timezone, local hour-of-day, product surface, post age". Candidates are represented by semantic IDs, which are residual-quantized codes over a multimodal post embedding, plus hashed author IDs. The README states there are no hand-engineered content features.
- **New-author cold start** (`home-mixer/scorers/author_cold_start.rs`; params in `param.rs` lines 490–560). See feature 4.
- **Spam and abuse labels that drop posts for non-followers.** See features 8 and 9.
- **Debias payload** (`home-mixer/scorers/vm_ranker_debias_payload.rs`). It sends has_video, max_video_duration_ms, has_photo, media_count, weighted_text_len, newline_count and has_url to the reranker. `VMRankerSendDebiasInputs` is false by default. X evidently treats these format variables as biasing the model's predictions.

---

## 2. Feature catalogue

Each entry gives: definition from your inputs | direction and size | source | tag | timing.

### Author and baseline

**F1. Author's own baseline (past success).**
- Definition: median and IQR of log(1+likes), and of replies, RTs and quotes, over the account's previous N posts in `H` that are strictly older than the target and at least 48 h old. Use it as a covariate, or better, model the target as lift = log(likes+1) − baseline.
- Effect: Martin, Hofman et al. (WWW 2016), on 852M tweets. Content features alone gave a "negligibly low R2". Adding past user success raised R² to 0.42 (unrestricted) and 0.48 (restricted). "A single feature (past user success) performs almost as" well as all features together. Bakshy-style ex-ante models reached R² ≈ 0.34.
- Source: arXiv 1602.01013, text at `/tmp/8vc-research/context/pdfs/1602.01013.a.txt` lines 1037–1066 [verified].
- Timing: PRE. Inputs: H.

**F2. Account reputation (PageRank-style cred) and the following/follower penalty.**
- Definition: following/follower ratio, `following > 2500`, account age from `A` joined date, and log(followers).
- Effect:
  - 2023 TweepCred (`src/scala/com/twitter/graph/batch/job/tweepcred/Reputation.scala`) divides PageRank mass when following > 2500, by `exp(3.0·(ratio−0.6)·ln ln following)`, capped at 50× [verified]. URL: https://raw.githubusercontent.com/twitter/the-algorithm/main/src/scala/com/twitter/graph/batch/job/tweepcred/Reputation.scala
  - 2026 `user-cred-v2/UserCredV2.scala`: score = clamp(165.2 + 7.07·ln(mass), 0, 100), with mass from PageRank over follow and engagement edges [verified].
  - High-PageRank or grey-badge authors are **exempt** from several automatic spam and abuse labels (`grox/flows/ptos/task_write_safety_post_annotations_result_sink.py` lines 189–193, 240–262) [verified].
  - Suh et al. 2010 (74M tweets): follower and followee counts and account age affect retweetability; past tweet count does not [secondary, abstract via search summary; DOI 10.1109/SocialCom.2010.33].
- Timing: PRE if snapshotted correctly (see 0.1). Inputs: A.

**F3. Premium or verified status.**
- Definition: blue-check flag, if the proxy exposes it.
- Effect:
  - 2023 code applied ×4 in-network and ×2 out-of-network multipliers [verified, 1a]. The 2026 public ranker has no subscription multiplier (grep of `home-mixer` for verified/premium finds only ads and logging) [verified].
  - Buffer, 18.8M posts from 71k accounts, Aug 2024 to Aug 2025: free accounts typically <100 median impressions per post, Premium about 600, Premium+ >1,550. Median engagement rate for free accounts was 0% by March 2025. Observational; Buffer says Premium users "might have larger audiences". https://buffer.com/resources/x-premium-review/ [verified]
  - X's help pages say Premium replies get prioritized in conversations [recalled].
- Timing: PRE.

### Early dynamics (the "how fast it spiked" family)

**F4. Early like-per-home-view in the first 2 hours. This is what X's new-author boost reads.**
- Definition: likes / views for posts that are less than 2 h old, for accounts with ≤ 50k followers.
- Effect: X's cold-start boost [verified, `author_cold_start.rs` and `param.rs` 490–560]. A post is eligible when all of these hold:
  - it is an original (not a reply or retweet);
  - the author has ≤ 50,000 followers;
  - the post is ≤ 7,200 s old;
  - it has < 200 home impressions;
  - it is within the top 97% of ranked positions.

  Among eligible posts, X draws a Thompson sample from Beta(0.75 + likes, 49.25 + home_views − likes). The prior mean is 1.5% likes per home view. It keeps the top 2 samples (`ColdStartTsTopK` 2), and of those the post with the higher model score wins. The winner's score is raised to at least the score at 0-indexed rank 15 (`random_range(15..16)`), which is about feed position 16. So for a small account, the like rate over the first ~200 home impressions decides whether X explores the post further. The brand account is likely under 50k, so this applies.
- Measurement problem: you only have `C` at fetch time. To use this live, poll views and likes during the first 2 h. Retrospectively it is FETCH-only.
- Timing: EARLY, which needs polling; otherwise FETCH (leaky).

**F5. Reply-arrival velocity and SEISMIC "infectiousness".**
- Definition: from `R`, the reply arrival times t_i and each replier's follower count n_i. Compute replies in the first 10, 30 and 60 min; inter-arrival trend (first-half vs second-half speed); and SEISMIC p̂(t), a follower-weighted self-exciting estimate with a memory kernel that is flat for ~5 min and then decays as a power law.
- Effect:
  - SEISMIC (KDD 2015) uses only reshare times and resharer follower counts. It gets 15% relative error on final retweet count after 1 h and 25% after 10 min, and found 78 of the top 100 tweets by tracking 500.
  - Cheng et al. (WWW 2014, Facebook photos): temporal features alone reach accuracy 0.780 and AUC 0.870 on "will it double", against 0.795 and 0.877 for all features. Content features alone reach 0.558.
- Sources: arXiv 1506.02594, `/tmp/8vc-research/context/pdfs/1506.02594.txt` lines 88–100, 160–172 [verified]; arXiv 1403.4608, `.../1403.4608.txt` lines 268–303 [verified].
- Caveat: these models were fit on retweets. Fitting them on replies is an adaptation with no published validation.
- Timing: EARLY. Valid only if you truncate `R` to t < cutoff.

**F6. Largest early broadcast.**
- Definition: max follower count among repliers (and quote authors, if available) in the first hour, plus the share of total follower reach that comes from the single largest one.
- Effect: Goel, Anderson, Hofman & Watts (Mgmt Sci 2016, about 1B Twitter cascades, 219,855 with ≥ 100 nodes). Structural virality correlates with size at only 0.2 for news and ≈ 0 for pictures and videos. "If popularity is consistently related to any one feature, it is the size of the largest broadcast." `/tmp/8vc-research/context/pdfs/goel.a.txt` lines 498–510, 770–795 [verified].
- Caveat: a reply from a big account reaches that account's followers only through the in-network path. The 2026 `OONRetweetReplyFilter` drops replies and reposts from accounts the viewer does not follow (README filter table) [verified]. Quote posts by big accounts are the stronger signal.
- Timing: EARLY.

**F7. Spread across communities, not depth.**
- Definition: number of distinct "communities" among early repliers. Proxy: cluster repliers by follower-count tier and by whether they appear in the account's historical replier set (in-audience vs new). Also structural virality (average pairwise distance in the reply tree), if threading is available.
- Effect:
  - Weng, Menczer & Ahn 2013: "The more communities a meme permeates, the more viral it is". Future popularity is predictable from early community concentration [verified, abstract at `.../pdfs/1306.0158.a.txt`].
  - Cheng 2014: early **breadth** predicts large cascades better than depth [verified].
  - Brady 2017: moral-emotional content spreads 20% more within ideological networks than between them [verified].
- Timing: EARLY.

### Content (post-time, cheap to compute from `T` and `M`)

**F8. Out-group references, and a targeted-insult flag that works against them.**
- Definition: count of rival-team, rival-player and opposing-fanbase mentions, from a brand lexicon in `mission/mission.md`. Separately, a classifier flag for "insult aimed at a named person".
- Effect:
  - Rathje, Van Bavel & van der Linden (PNAS 2021, 2.73M posts): each out-group word raised the odds of a share or retweet by about 67% (exp(b) = 1.67). In-group words had no significant effect (1.05). Negative affect gave 1.14, moral-emotional 1.10 and positive affect 0.95. https://pmc.ncbi.nlm.nih.gov/articles/PMC8256037/ [verified]
  - **X counteracts this.** The 2026 post-safety pipeline labels English `HateOrAbuseInsultsWithTarget` as `FosnrAbuseInsults` unless the author is high-PageRank or grey-badge (`task_write_safety_post_annotations_result_sink.py` 258–268). The visibility corpus maps that label to `fosnr_abuse_insults_non_follower/drop/undesirable` (`visibility-filtering/staging/replay_corpus.rs` line 221) [verified].
  - Net effect: banter that names the rival raises engagement; insulting a rival player can remove the post from every non-follower's For You.
- Timing: PRE. Inputs: T.

**F9. Engagement-bait wording. Strongly negative for reach.**
- Definition: regex or LLM detection of "like if / RT if / comment X / follow for" and similar asks.
- Effect: Grox labels `SpamEngagementBaiting` and `SpamEngagementFarming` as `SpamHighRecall` for **every** author, with no PageRank exemption (`task_write_safety_post_annotations_result_sink.py` 232–246). `SPAM_HIGH_RECALL_DROP_UNDESIRABLE` sits in the `TIMELINE_HOME_RECOMMENDATIONS` rule set (`visibility-filtering/rules/levels.rs` 49–116, rule at `tweet_rules.rs` 945). The post can still reach followers but not recommendations [verified].
- Plain questions are not bait. I found no measured effect for questions.
- Timing: PRE.

**F10. Emotional arousal and valence.**
- Definition: lexicon or LLM scores for anger, awe, anxiety, sadness, positivity and practical value; plus a count of moral-emotional words.
- Effect:
  - Berger & Milkman (JMR 2012, NYT most-emailed list), per +1 SD: anger +34%, awe +30%, practical value +30%, interest +25%, anxiety +21%, emotionality +18%, surprise +14%, positivity +13%, sadness −16%. Faculty PDF Fig. 2: https://faculty.wharton.upenn.edu/wp-content/uploads/2011/11/Virality.pdf [verified]
  - Brady et al. (PNAS 2017, 563k tweets): +20% retweets per moral-emotional word (IRR 1.20). https://pmc.ncbi.nlm.nih.gov/articles/PMC5514704/ [verified]
  - Robertson et al. (Nat Hum Behav 2023, Upworthy RCTs, 370M impressions): each negative headline word +2.3% CTR [verified, abstract via Europe PMC].
  - Note the conflict: positive words help in Berger (+13%) and hurt in Rathje (−5%). For a fan account, awe ("logo three from 35 ft") is the safe high-arousal lever.
- Timing: PRE.

**F11. Shareability and save-worthiness.**
- Definition, PRE: practical-value or utility score, plus "would someone send this to a friend" (highlight clip, stat graphic, meme template).
- Definition, FETCH: bookmarks/likes and bookmarks/views from `C`.
- Effect: copy-link share 20.0, DM share 5.0 and share 2.0 are the largest positive weights in the 2026 ranker. Follow-author is 4.0 [verified, 1c]. Bookmarks are positive training labels for retrieval [verified]. Berger: practical value +30% per SD [verified].
- No public data maps bookmark ratio to later reach. Treat bookmarks/likes as an outcome label ("valuable vs. merely liked"), not a predictor of the same post.
- Timing: the PRE proxy is the usable one; the ratio is FETCH.

**F12. Media format, video length and links.**
- Definition: from `M` and `T`: photo, video, link, text-only, media count, video duration, has_url.
- Effect:
  - Buffer 2026 (45M+ posts, all platforms; no X-specific n given), median engagement rate on X: text 3.56%, image 3.40%, video 2.96%, link 2.25%. https://buffer.com/resources/data-best-content-format-social-media/ [verified]
  - Twitter 2014 (2M tweets from verified users): photos +35% retweets, video links +28%, quotes +19%, numbers +17%, hashtags +16%. Twitter recommended ≤ 2 hashtags [secondary via richmedia.com; the original at blog.x.com was 403].
  - 2026 code: videos under 10 s are not VQV-eligible (`MinVideoDurationMs` 10_000). VQV weight is currently 0.0 [verified].
  - **Links:**
    - Musk, July 2026, on whether X still penalizes link posts: "We haven't for over a year." https://www.freepressjournal.in/tech/x-product-head-nikita-bier-confirms-link-penalty-removed-over-a-year-ago-tells-mark-zuckerberg-he-can-post-them-directly [verified article]
    - Bier, Oct 19, 2025: link reach is lower because "the web browser covers the post and people forget to Like or Reply". https://www.socialmediatoday.com/news/x-formerly-twitter-testing-links-in-app-link-post-penalties/803176/ [verified article]
    - Musk: "Posting a link with almost no description will get weak distribution" [verified via the same article].
    - Buffer: link posts from free accounts "completely suppressed since March 2025" [verified]. The two engagement denominators in Buffer's two reports differ (0–0.9% vs 2–3.6%), so compare within a report only.
    - The 2026 code has a positive `OpenLinkWeight` 0.2 and no link deboost. The lost likes and replies remain a mechanical penalty.
- Timing: PRE.

**F13. Numbers, stats and concrete detail.**
- Definition: digit count and score/stat tokens.
- Effect: +17% retweets (Twitter 2014) [secondary].
- Repo rule: drafts must write `[VERIFY: ...]` instead of inventing stats, so this lever depends on sourced numbers.
- Timing: PRE.

**F14. Text length and line breaks.**
- Definition: X weighted length and newline count.
- Effect: no effect size found. X lists `weighted_text_len` and `newline_count` among the reranker's debias inputs [verified]. Dwell weights (0.05 on P(dwell); 0.006 per unit of predicted dwell time) reward posts people stop on.
- Timing: PRE. Weak evidence; include as a control.

**F15. LLM-slop signature.**
- Definition: an LLM-likeness score of the draft.
- Effect:
  - Grox's "banger" screen (a Gemma VLM) outputs a `slop_score` per original post (`grox/flows/upa/classifier_banger_initial_screen_gemma.py`, `task_write.py` line 173). The 2025 Scala param `scored_tweets_grok_slop_score_decay_value` exists.
  - Accounts labeled `llm_slop_user` get `SpamHighRecall` for 7 days (`abuse-enforcement-service/service-lib/src/lib.rs` 3564–3569) [verified].
  - The weight on slop_score in ranking is not public.
- Directly relevant: this harness writes drafts with an LLM.
- Timing: PRE.

**F16. Novelty against the account's own history.**
- Definition: 1 − max cosine similarity between the target's embedding and the account's last N posts in `H`. Also the distance to the account's topic centroid.
- Effect:
  - Mechanism only. Phoenix represents posts by semantic IDs shared across same-topic posts [verified]. The reranker runs a determinantal point process that trades "a little score for less similarity between neighbours" (README, vm-ranker row) [verified].
  - Wu & Huberman 2007 (Digg) measured novelty decay with a half-life of about 69 min (`/tmp/8vc-research/context/pdfs/0704.1158.a.txt` lines 276–290) [verified]. That is about topics, not authors.
  - No direct measurement of self-similarity vs reach found.
- Timing: PRE.

### Timing and context

**F17. Posting cadence and bursts.**
- Definition from `H`:
  - number of the account's own posts in the 2 h before the target;
  - minutes since the previous post;
  - posts per day over 7 d;
  - weekly consistency (weeks with ≥ 1 post out of the last 20).
- Effect:
  - Author-diversity decay: ×0.625 for the second post, ×0.4375 for the third, floor ×0.25, within one ranked batch [verified, 1c].
  - Bier (May 2026) said the client pulls 35 posts at a time and pushes repeat authors to the next pull [secondary; X trending page returned 402].
  - Buffer consistency study (100k+ users, all platforms): ≥ 1 post a week for 20+ weeks gave 450% more engagement per post [secondary].
- Timing: PRE.

**F18. Viewer-local hour and audience time zone.**
- Definition: post hour converted to the audience's local time. Estimate the audience's activity curve from historical reply arrival hours in `R` across `H`. Feature = audience activity density at TS; also day of week.
- Effect: Phoenix uses the viewer's timezone and local hour-of-day as model inputs [verified]. For HN dev-tool launches, posting hour mattered ("hundreds of additional stars"; arXiv 2511.04453) [verified abstract].
- No rigorous X-specific effect size found.
- Timing: PRE.

**F19. Live-event window (sports).**
- Definition:
  - minutes from TS to tip-off and to the final buzzer of the subject's game;
  - in-game vs post-game vs off-day;
  - win/loss (fetch the schedule and box score from ESPN).
- Effect:
  - Fan, Billings et al. 2020 (2018 World Cup tweets): fans BIRG (associate with the team) when leading or after wins and CORF (distance themselves) when trailing or after losses [secondary abstract].
  - X builds live-basketball context into the product for team accounts [verified, 1b].
  - Timeline in-network and OON only serve posts ≤ 48 h old, and novelty decays on an hour scale (F16). Being early relative to the event plausibly matters.
- Timing: PRE.

**F20. Exogenous attention proxy.**
- Definition: Wikipedia daily pageviews for the subject (Caitlin_Clark, the team, the rival) on the post day, relative to a 28-day median. Endpoint: https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/Caitlin_Clark/daily/YYYYMMDD/YYYYMMDD (keyless).
- My probe, 2026-09-01 to 10-14: median 5,113 per day, peak 17,551 on 2026-09-30 (3.4×) [verified, computed].
- For dev tools: HN front-page presence that day (HN Algolia API, keyless) and GitHub star velocity. Kraishan 2025: 138 launches gained on average 121 stars in 24 h and 289 in a week after HN exposure. "Show HN" gave no advantage after controls (arXiv 2511.04453) [verified abstract].
- GDELT DOC API is keyless but rate-limited to one request per 5 s. My probe was throttled.
- Evidence that pageviews track demand: Mestyán et al. 2013 (PLOS ONE), on movie box office [verified abstract]. There is no direct X-engagement study.
- Timing: PRE if you take the day before; same-day is borderline.

**F21. Attention competition.**
- Definition:
  - concurrent big-news volume in the topic in the posting hour (GDELT timeline volume, or the number of posts from the top-k accounts in the niche from their `H`);
  - for dev tools, the count of other launches on HN or Product Hunt that day.
- Effect: Weng et al. 2012 (Sci Rep) explain the heavy-tailed popularity of memes from limited attention plus network structure, "without the need to assume different intrinsic values among ideas" [verified abstract]. Gleeson et al. 2014: competition puts popularity at criticality [verified abstract]. Both are models; no X field effect size.
- Timing: PRE.

### Outcome-type labels (FETCH only; use as targets, not predictors)

**F22. Ratio and controversy.**
- Definition: replies/RTs and quotes/likes from `F`.
- Effect: Minot et al. 2021 (PLOS ONE) call a tweet "ratioed" when retweets/replies < 1. 18% of Trump's post-candidacy tweets met that bar, against under 1% of Obama's. Median ratio after the 2016 election: Obama 7.77, Trump 1.32. Inflection points cluster at about 1 min and about 1 day. https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0248880 [verified]
- In the 2026 ranker, quote 5.0 equals reply 5.0. The negative heads (mute −58.8, not-interested −52.3, block −31.2) price in what controversy costs.
- Use it to split "loved" from "argued with".

**F23. Community Note attached.**
- Definition: whether the post carries a note (visible on fetch).
- Effect: Chuai et al. (arXiv 2409.08781, 237,677 cascades): a displayed note cut subsequent reposts by 62.0% on average and roughly doubled the odds of deletion (+103.4%). The abstract warns notes may be "too slow" for the viral phase [verified abstract].
- A brand account inventing stats is the risk scenario.
- Timing: FETCH.

### Candidates evaluated and dropped (no usable evidence found this session)

- **Emoji count, first-person pronouns, humor and concreteness as standalone features.** No X-specific measured effect found. Humor partly overlaps awe and amusement (Berger).
- **Day of week.** Only vendor blog posts. Fold into F18.
- **Hashtag count beyond the "≤ 2" folk rule.** The 2014 +16% is old, and Phoenix does not read hashtags as features.

---

## 3. Reading of the evidence

- Ex-ante content features explain little: R² ≈ 0 alone (Martin 2016), content accuracy 0.56 (Cheng 2014). The author baseline and early dynamics carry most of the signal. A recommender for a brand should therefore:
  1. predict lift over the account's baseline, not raw likes;
  2. use content features (F8–F16) for the *which draft* choice, where their effects are 10–70% per unit rather than orders of magnitude;
  3. avoid the hard reach cliffs. Engagement bait (F9), targeted insults (F8) and slop (F15) can each drop a post from non-follower recommendations.
- For a sub-50k account, the 2-hour cold-start window (F4), with its 1.5% prior like-per-home-view rate, is the mechanism that decides whether a post leaves the follower base. That is concrete enough to schedule around (F18, F19).
