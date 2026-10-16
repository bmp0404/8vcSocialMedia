# Deep and recent popularity / cascade prediction, read against our data

Reader: the 8VC harness team deciding what to build with 1,665 labelled dev-tool posts (final counts, Snowflake time, text, media) plus keyless reads of current counts, views, bookmarks, author followers, reply lists with reply timestamps and reply-author followers, and live snapshots. No retweeter lists, no retweet times, no follower graph.

Data regimes used below:
- (a) final counts only
- (b) reply timestamps (and reply-author follower counts)
- (c) repeated snapshots of counts and views on live posts
- (d) full cascade graph with timestamps (who retweeted whom, when)
- (e) content only, before publication

Tags: [verified] means I opened the abstract, paper, or code during this session (2026-10-15). [recalled] means from memory or a search snippet only; check before quoting.

## 1. X's ranking code (most useful section for this team)

### 1.1 xai-org/x-algorithm (2026) [verified, read the source]
- Repo: https://github.com/xai-org/x-algorithm . First public drop 2026-01-20 (Engineering on X: https://x.com/Engineering/status/2013471689087086804 [recalled date via 36kr/keywordseverywhere]). Large 187-file drop 2026-05-15 (https://www.thedeepfeed.ai/posts/2026-05-16-x-algorithm-phoenix-release/ [verified]). Scoring weights published 2026-08-13 (https://keywordseverywhere.com/news/social-algorithm-updates/x-algorithm-open-source/ [verified]). Commits still landing: GitHub API shows commits on 2026-10-09, 10-13, 10-14, 10-15.
- Pipeline: Home Mixer orchestrates; Thunder holds in-network posts; Phoenix (a transformer "ported from the Grok-1 open source release") does retrieval and ranking; Grox runs Grok vision-language models for content labels (quality "banger" score, slop_score, reply ranker 0-3, spam, safety). Grox prompts are withheld.
- Phoenix predicts per-viewer probabilities of actions. Final score = sum of weight_i x P(action_i), then an offset/normalization, then multipliers.
- Default weights, read directly from `home-mixer/params/param.rs` (raw file downloaded 2026-10-15):

| action | weight |
|---|---|
| favorite (like) | 0.66 |
| reply | 5.0 (plus a 15.0 boost when viewer and author follow each other) |
| retweet | 1.0 |
| quote | 5.0 |
| share | 2.0 |
| share via DM | 5.0 |
| share via copy link | 20.0 |
| follow author | 4.0 |
| click | 0.3 |
| open link | 0.2 |
| continuous click dwell time | 0.4 |
| dwell | 0.05 |
| continuous dwell time | 0.006 |
| photo expand | 0.05 |
| video open | 0.02 |
| profile click, VQV (video quality view) | 0.0 |
| not interested | -52.272 |
| block author | -31.2 |
| mute author | -58.8 |
| report | -234.0 |
| not dwelled | -0.02 |

  The file's own comment warns these weights multiply predicted probabilities, so "one report cancels 468 likes" is wrong; report base rates are said to be over 1000x lower than likes. Weights live in feature switches and can vary by user and experiment, so these are defaults, not a guarantee of what runs.
- Author diversity and out-of-network (OON) multipliers exist in `xai-value-model/scoring.rs`, but `home-mixer/scorers/value_model.rs` sets `enable_author_diversity: false`, decay 1.0, floor 1.0 in this path. The production OON factor value is not in the repo; 0.75 appears only in a unit-test fixture.
- Cold-start boost (`home-mixer/scorers/author_cold_start.rs`): original posts (not replies or retweets) from authors with at most 50,000 followers, under 2 hours old, with fewer than 200 home-timeline impressions, can be lifted into ranked slot 15-16. With Thompson sampling on (default true), the pick samples Beta(0.75 + likes, 49.25 + home_views - likes), i.e. a prior like-per-home-view rate of 1.5%, and takes the best of the top 2 samples by model score. So early like rate per home view is literally the bandit signal for small accounts.
- Changes Aug-Sep 2026 per keywordseverywhere [verified page, not each commit]: topic-repeat diversity added beyond author (17-18 Aug), ranker gets author follower counts (17 Aug), dwell added to ranking weights and VQV removed (25 Aug), default Phoenix aggregation switched to a "long-dwell" variant (2 Sep).
- What it gives the harness: an explicit, current ranking objective. Replies and quotes are worth 5-7.5x a like per predicted probability, a copy-link share 30x a like, and a single predicted "not interested" or mute wipes out roughly 80-90 likes' worth of probability mass. For a brand account the action implication is to draft for replies, quotes, shares and profile follows, and to treat anything that invites mutes/"not interested" (slop, engagement bait, off-topic promo) as heavily negative. Grox's slop_score and banger screen mean a Grok model is literally grading post quality before amplification.
- Regime: none required; it is a prior. Our observable counts (likes, replies, reposts, quotes, bookmarks, views) cover only a subset of the heads. Bookmarks and copy-link shares are not the same action, but bookmarks are the closest public proxy for "save/share" intent.

### 1.2 twitter/the-algorithm-ml heavy ranker (March 2023) [verified]
- https://github.com/twitter/the-algorithm-ml/blob/main/projects/home/recap/README.md
- Weights then: fav 0.5, retweet 1.0, reply 13.5, good_profile_click 12.0, video_playback50 0.005, reply_engaged_by_author 75.0, good_click 11.0, good_click_v2 10.0, negative_feedback_v2 -74.0, report -369.0. README says the weights were set so each term contributes about equally on average, then tuned for platform metrics.
- What changed by 2026: reply-engaged-by-author (75) is gone as a separate head; reply fell from 13.5 to 5.0 relative to like at roughly 0.5-0.66. Replies still dominate likes. The "author replies to repliers" heuristic that 2023 growth guides pushed no longer has a dedicated weight in the published 2026 defaults (it may still matter through the mutual-follow reply boost and Phoenix's learned features).

### 1.3 Views data on X
- Pfeffer, Matter, Sargsyan 2023, "The Half-Life of a Tweet", https://arxiv.org/abs/2302.09654 [verified]. Repeated snapshots of impression_count: impressions per second peak about 72 s after posting, median half-life of views about 80 minutes, and for about 95% of tweets no relevant impressions arrive after 24 h. Implication: a snapshot at 1-2 h already sees roughly half of final views for a typical post, so early-snapshot prediction is feasible. Regime (c).
- Elmas, Selim, Houssiaux 2023 (WWW poster), https://arxiv.org/abs/2303.06120 [verified]. Using Twitter's own "Viral Tweets" topic as ground truth, the best rule was retweets / author followers > 2.16; it favors small accounts. A transformer early-detector reached F1 0.79. Implication: follower-normalized reposts is the virality definition that matched X's own label. Regime (a).
- "How Algorithms Can Influence Content Visibility on Twitter/X", https://arxiv.org/abs/2410.17390 [recalled from search snippet]. 40M tweets with view counts; tweets without external URLs get systematically more views. Consistent with the 2026 weights (open_link 0.2, click 0.3) and with posts being judged on in-feed dwell. Regime (a) plus views.
- Community Notes rollout study, https://arxiv.org/abs/2307.07960 [recalled from snippet]: models retweets and likes as binomial in impression count, i.e. engagement rate per view. That is the right likelihood for our views data.
- Industry benchmarks (Metricool/Statista, https://metricool.com/x-Twitter-Statistics/ [recalled from snippet]): 1.1M posts in 2025, mean 2,711 impressions/post, engagement rate per impression about 1.58%. Close to the 1.5% like-per-home-view prior in X's cold-start code. Different vendors report 0.015% to 1.6% because denominators differ (followers vs impressions). Pick one definition and say which.
- Milli et al. 2025, PNAS Nexus, "Engagement, user satisfaction, and the amplification of divisive content", https://academic.oup.com/pnasnexus/article/4/3/pgaf062/8052060 [recalled from snippet]: Feb 2023 audit of 806 users; engagement ranking amplified emotionally charged out-group hostile content relative to chronological feeds. Relevant mainly as a caution that what X amplifies and what readers say they want diverge.

## 2. Deep cascade models (regime d, mostly useless to us)

All of these take the retweet cascade (who reshared, from whom, when) for the first T minutes or hours and predict the increment or final size. Standard data: Weibo (about 119k cascades, 1-3 h observation), APS citations, a 2012 Twitter hashtag set. We do not have retweeter lists, retweet times, or a follower graph, and we have 1,665 posts. None of these can be trained on our data, and none has a public X-trained checkpoint we can reuse.

| model | citation | what it adds | regime | use to us |
|---|---|---|---|---|
| DeepCas | Li, Ma, Guo, Mei, WWW 2017, https://arxiv.org/abs/1611.05373 [verified] | random walks over cascade graph, end-to-end; says node embeddings alone are not enough | (d) | none |
| DeepHawkes | Cao, Shen, Cen, Ouyang, Cheng, CIKM 2017, https://github.com/CaoQi92/DeepHawkes [verified repo] | deep version of Hawkes: user influence, path encoding, learned time-decay kernel | (d), needs retweet paths | the idea (decaying self-excitation) transfers to replies; the model does not |
| CasCN | Chen et al., ICDE 2019 [recalled] | graph conv over sub-cascade snapshots + LSTM | (d) | none |
| Cascade2vec | Huang, Wang, Zhang, IEEE Access 2019 [recalled via snippet] | recurrent GNN improving CasCN's kernel | (d) | none |
| VaCas | Zhou, Xu, Zhang, Trajcevski, Zhong, INFOCOM 2020 [recalled] | variational latent factors over sub-cascades | (d) | none |
| CasFlow | Xu, Zhou, Zhang, Liu, Trajcevski, TKDE 2021/2023, https://ieeexplore.ieee.org/document/9611000/ [verified abstract via search] | normalizing flows for uncertainty; claims 21% error reduction after 30 min observation | (d) | none; uncertainty idea is fine but a quantile GBM gets it cheaper |
| TempCas | Tang et al., AAAI 2021 [recalled] | adds macro time-series branch | (d) | none |
| MUCas | Chen, Zhang, Zhou, Bonsangue, IJIS 2022, https://onlinelibrary.wiley.com/doi/abs/10.1002/int.22786 [verified abstract via search] | multi-scale graph capsules | (d) | none |
| CasSeqGCN | Wang et al., ESWA 2022, https://arxiv.org/pdf/2110.06836 [recalled] | GCN on sub-cascades + LSTM | (d) | none |
| CTCP | Lu et al., IJCAI 2023, https://arxiv.org/abs/2306.03756 [verified] | one dynamic graph across all cascades sharing users | (d) plus cross-cascade user overlap | none (we lack users who reshared) |
| CasDO | Cheng, Zhou, Xu et al., TKDE 2024, https://github.com/CZ-TAO12/CasDO [verified abstract via search] | neural ODE + probabilistic diffusion model for irregular timing and uncertainty | (d) | none; >24 h training per the leakage paper |
| ConCat, CasFT | arXiv 2409.16623, 2409.16619 [recalled] | continuous-time and diffusion-driven trend models | (d) | none |
| AutoCas | Zheng et al. 2025, https://arxiv.org/abs/2502.18040 [verified] | tokenizes cascade data and runs an LLM backbone autoregressively; not text-based | (d) | none |
| TFM4POP | Zhu et al., arXiv Aug 2026, https://arxiv.org/abs/2608.25048 [verified] | tabular foundation model (in-context learning) over static views (text, image, tabular) + neural ODE over cascade | static views (a)/(e) plus (d) | the static-branch idea is the useful part: a tabular foundation model like TabPFN suits n=1,700 |
| MMCas | WWW 2026 (https://www.fst.um.edu.mo/personal/wp-content/uploads/2026/01/2026_WWW_MMCas.pdf) [recalled title only] | multimodal cascades | (d)+(e) | unknown |

The paper that should change how the team reads this whole table:
- Peng et al., "From Leakage to Fidelity: Reliable Benchmarking for Temporal Cascade Prediction" (earlier title "Beyond Leakage and Complexity"), https://arxiv.org/abs/2510.25348 , v3 Sep 2026 [verified v2 html]. Most prior cascade papers split cascades randomly, so training sees future bursts. Under a time-ordered split, an MLP on handcrafted features beats CasDO on every dataset and DeepHawkes on one; it does not beat CasCN, CasFlow or CTCP on Twitter/Weibo/APS. CasFlow and CasDO overfit (low train loss, high test loss). The authors say added complexity "largely compensates for flawed evaluation protocols." Their real-data inflation estimate is not measured, only argued with a toy set. Implication: split our 1,665 posts by time, never randomly, and expect simple features to hold up.

Surveys:
- Zhou, Xu, Trajcevski, Zhang, "A Survey of Information Cascade Analysis: Models, Predictions, and Recent Advances", ACM CSUR 54(2) 2021, https://arxiv.org/abs/2005.11041 [verified abstract]. Taxonomy: feature engineering, stochastic/generative processes, graph representation, deep learning.
- Wang, Wang, Xiong, Chen, "A Survey of Deep Learning-Based Information Cascade Prediction", Symmetry 16(11) 2024, https://pure.bit.edu.cn/en/publications/a-survey-of-deep-learning-based-information-cascade-prediction/ [recalled via snippet].
- "Information Cascade Prediction under Public Emergencies: A Survey", https://arxiv.org/abs/2404.01319 [recalled title].
- Community list: https://github.com/ChenNed/Awesome-DL-Information-Cascades-Modeling [recalled].

## 3. Early-signal prediction (regimes b, c)

- Cheng, Adamic, Dow, Kleinberg, Leskovec, "Can Cascades Be Predicted?", WWW 2014, https://arxiv.org/abs/1403.4608 [verified, read PDF]. Facebook photos. After the first 5 reshares, predict "will it double (reach the median final size)": accuracy 0.795, AUC 0.877 with logistic regression. Top vs bottom quartile: 0.926 accuracy, 0.976 AUC. Temporal features alone are within 0.025 of all features; content features alone 0.558 accuracy. Pre-2018, but it is the cleanest statement of the effect: the speed of the first few reactions carries most of the signal. Our analogue: time to the k-th reply (from reply IDs) and snapshot velocity of likes and views.
- Martin, Hofman, Sharma, Anderson, Watts, "Exploring Limits to Prediction in Complex Social Systems", WWW 2016, https://arxiv.org/abs/1602.01013 [verified, read PDF]. Ex-ante (before any reshares) Twitter cascade size: content features alone R2 well below 0.05; basic user features about 0.2; adding the user's past success 0.42 (0.48 on a restricted set); a single feature, past user success, does almost as well as all features. Simulations suggest the ceiling is not much higher even with perfect data. This is the single most important number for the team: ex-ante, author history dominates and content barely registers in pooled regression.
- Mishra, Rizoiu, Xie, "Feature Driven and Point Process Approaches for Popularity Prediction", CIKM 2016, https://arxiv.org/abs/1608.04862 [verified abstract via search]. Marked Hawkes per tweet plus a learned predictive layer beats pure feature and pure generative models on retweet cascades. Hawkes needs retweet times; we could fit the same model to reply arrival times plus reply-author follower counts as marks.
- Haimovich, Karamshuk, Leeper, Riabenko, Vojnovic, "Popularity Prediction for Social Media over Arbitrary Time Horizons", PVLDB 2022, https://arxiv.org/abs/2009.02092 [verified abstract]. Facebook pages. Accurate predictions across horizons need both static features (content, author) and the observed growth so far, fed through a Hawkes-shaped feature model. Implication for us: one model that takes static features plus "counts at time t" for any t we happened to snapshot, rather than one model per horizon.
- Kong, Rizoiu, Xie and the HIP / SIR-Hawkes line (Rizoiu et al. WWW 2017, 2018) [recalled]: Hawkes with exogenous input; good for "how much more will this get".
- Bluesky reply-cascade benchmark (MMG-PopNet), https://arxiv.org/html/2606.27539 [verified partial]. Cascades are reply trees (root post plus replies), which is exactly our regime (b). Multimodal LLMs: zero-shot (cascade serialized as JSON) has the highest error, "lacks numerical calibration"; retrieval-augmented few-shot with 4 similar training posts matches or beats fine-tuning when only the root or a short early window is seen; fine-tuning wins as the window grows; the purpose-built graph model beats every LLM setting. Graph baselines (DeepHawkes, DeepCas, CasSeqGCN, Graph-LSTM, MLP) are within 5-17% MSE of the winner.

## 4. Content-only and LLM prediction (regime e)

### Pairwise "which post wins" results with LLMs
- Ye et al., "LOLA: LLM-Assisted Online Learning Algorithm for Content Experiments", https://arxiv.org/abs/2406.02611 [recalled via search snippet]. Upworthy, 17,618 headline tests. Picking the CTR winner among multiple arms: random 33.0%, GPT-3.5 at chance, GPT-4 zero-shot 37.85%, GPT-4 in-context 38-40%, OpenAI embeddings + linear regression 46.3%, LoRA Llama-3-8B 46.9%, human respondents near random.
- Maiorano, "Do Synthetic Personas Predict Real Audience Response?", https://arxiv.org/html/2609.25010v1 (2026) [verified]. Upworthy, 1,695 tests, 6,978 variants. Only 23.5% of tests have a statistically reliable winner; on the rest no predictor can recover the order. On the 399 reliable tests: no-persona LLM ranker top-1 49.2% (random 30.4%), Kendall tau 0.361; 10-persona panel top-1 34.6%, tau 0.084. GPT-4.1 replication: tau 0.300 vs 0.082. Persona panels made predictions worse. On MIND news the gap shrinks; on Reddit reposts both are near zero. Author has a commercial interest in copy-simulation tools, which makes the negative persona result more credible, not less.
- Hu, Jin, Ye, Divakaran, Kumar 2025, "The Influence of Text Variation on User Engagement in Cross-Platform Content Sharing", https://arxiv.org/abs/2505.03769 [verified]. Same YouTube video posted to Reddit with different titles. Fine-tuned BERT 74% pairwise; GPT-4o near chance.
- Bojić et al. 2026, "Persona-prompted LLM agents achieve modest but genuine prediction of human social media reactions", Scientific Reports, https://www.nature.com/articles/s41598-026-66277-8 ; preprint https://arxiv.org/abs/2604.19787 [verified preprint abstract]. 1,511 Serbian participants, 27 LLMs, 120k persona agents. 70.7% overall reaction accuracy; like/dislike MCC 0.29; TF-IDF classifier MCC 0.36 beats the agents. Without persona information agents were near 50%. Persona detail adds little (71.5% full vs 69.6% demographics).
- Qiu et al. 2025, "Can LLMs Simulate Social Media Engagement?", https://arxiv.org/abs/2502.12073 [recalled via snippet]. Per-user retweet/quote/rewrite on X: zero-shot LLMs 33-53%, fine-tuned BERT 57%.
- Spotify, "When Can LLMs Replace Humans in A/B Tests?" (Aug 2026), https://engineering.atspotify.com/2026/8/when-can-llms-replace-humans-in-a-b-tests and "Statistical Foundations of LLM-based A/B Testing", https://arxiv.org/html/2606.17165 [recalled via snippet]. gpt-4o-mini CTR predictions recovered 39% of the human treatment effect raw; nonparametric calibration fixed it, linear calibration did not.
- Count Bayesie 2023 blog, https://www.countbayesie.com/blog/2023/3/23/replacing-an-ab-test-with-gpt [recalled]: GPT-3-based pairwise model 87% on the easiest (clear-winner) Upworthy cases; bag-of-words AUC about 0.8.
- Yale SOM "Why it Works" (2025), https://insights.som.yale.edu/sites/default/files/2025-12/Why%20It%20Works.pdf [opened, could not read numbers]. Knowledge-guided DPO on Upworthy pairs; an 80.62% pairwise figure circulates in search snippets but I could not confirm the subset. Do not cite the number.
- BuzzProphet, Xu et al., CIKM 2025, https://arxiv.org/abs/2510.08481 [verified]. LLMs "misestimate numbers"; using LLM-written rationales as extra features for a tabular regressor improved correlation up to 30% and RMSE up to 2.8% on 7,532 hashtags.

Takeaway: prompted LLMs judging "which of two posts gets more engagement" sit a little above chance overall, reach about tau 0.3-0.36 or 49% top-1 on cases where a real winner exists, and get worse with persona panels. Supervised models on controlled pairs reach 65-74%. LLMs help most as feature generators feeding a small supervised model, or as retrieval-augmented few-shot rankers.

### SMP Challenge (ACM MM, Flickr SMPD) [recalled via snippets]
- Overview: https://dl.acm.org/doi/pdf/10.1145/3664647.3688996 (2024 summary). 2025 video track winner MVP, https://arxiv.org/abs/2507.00950 : XCLIP video features + engineered user/time metadata + CatBoost, five-fold ensemble; removing user features hurt most. Winners across years are engineered features plus gradient boosting. Retrieval augmentation (SKAPP, AAAI 2025, https://www.xoveexu.com/file/paper/25-02-AAAI-SKAPP.pdf) reports large MSE gains by pulling similar past posts.
- Implication: the competition that most resembles "content + author metadata, no cascade" is won by GBMs on user features plus frozen embeddings, not end-to-end deep nets. With 1,700 posts that is the ceiling of what we should build.

## 5. Separating account size from content

- Tan, Lee, Pang, ACL 2014, "The effect of wording on message propagation: Topic- and author-controlled natural experiments on Twitter", https://aclanthology.org/P14-1017.pdf [verified, read PDF]. Pairs of tweets by the same user with the same URL, different wording. Humans (106 AMT workers) 61.3% at picking the more-retweeted version; the classifier 66.5% cross-validated, 65.6% held-out, beating a classifier trained on uncontrolled most/least-retweeted tweets even when that one had author and time metadata. Features that helped: asking people to share, more informative/longer text, headline-like phrasing, community-typical language, some sentiment. Implication: within-author pairwise comparison is the right evaluation for content quality, and about 65% is a realistic accuracy target.
- Hessel, Lee, Mimno, WWW 2017, "Cats and Captions vs. Creators and the Clock", https://arxiv.org/abs/1703.01725 [verified via search detail]. Reddit pairs posted in the same community within seconds to minutes. Text + image 62.7-67.1%; user features 54.6-68.4%; content beats user features in 5 of 6 communities; humans 60-73%. Time-only models near chance, so the pairing controls timing.
- Lakkaraju, McAuley, Leskovec, ICWSM 2013, "What's in a Name?", https://www.researchgate.net/publication/290014012 [recalled via snippet]. 132k Reddit submissions of 16.7k images resubmitted with different titles. A community model (subreddit, time, resubmission decay) plus a title language model fit the residual; reported combined R2 about 0.64 per a secondary summary. Method to copy: model the "context" first, then let the text model predict the residual.
- Martin et al. 2016 (above): pooled, author history explains almost everything that is explainable ex ante.
- Yu, Chen, Romero, Dhillon, CSCW 2025, "Signals in the Noise", https://arxiv.org/html/2509.08128v1 [verified]. Predict each engagement type from the other two (90th-percentile quantile regression), call the ratio observed/predicted "unexpectedness", then explain it with content. Topic is the biggest driver of the engagement mix (news/politics draw retweets and comments, sports/games draw likes and comments); URLs and longer text push retweets; concrete language pushes likes. R2 0.09-0.24. Useful as a way to read reply-heavy vs like-heavy posts without a follower model.
- Gligorić, Anderson, West, CSCW 2019, "Causal Effects of Brevity on Style and Success in Social Media", https://arxiv.org/abs/1909.02565 [verified via search]. Crowd-shortened tweets rated against originals: shorter wins up to 30-40% cut, best at 10-20% cut. Ratings, not real engagement.
- Elmas et al. 2023 (above): retweets/followers > 2.16 matched X's viral label.

## 6. What each item gives a team with about 1,700 posts

| finding | feature / detector / model | regime |
|---|---|---|
| X 2026 weights | target = weighted engagement score, e.g. 0.66 x likes + 1 x reposts + 5 x replies + 5 x quotes (+ bookmarks as share proxy), per view; penalize drafts likely to draw mutes | (a) plus views |
| X cold-start bandit | early like-per-view rate in the first 2 h, for authors <=50k followers | (c) |
| Martin 2016 | author baseline (log median engagement per author, follower count) as the first feature; never compare raw counts across authors | (a) |
| Tan 2014, Hessel 2017 | within-author pairwise eval set; target 60-67% | (a) |
| Lakkaraju 2013 | two-stage: context model (author, hour, weekday, media) then text model on residual | (a) |
| Elmas 2023 | label "breakout" as reposts/followers above a threshold, or residual above author's own distribution | (a) |
| Cheng 2014 | time to 5th reply, replies in first 30/60 min | (b) |
| Mishra 2016, Haimovich 2022 | Hawkes-shaped features on reply arrivals, marked by reply-author log followers; static + growth-so-far features in one model | (b), (c) |
| Pfeffer 2023 | snapshot at 1-2 h sees about half of final views | (c) |
| Leakage paper 2025-26 | time-ordered split only | all |
| LOLA, Maiorano, Hu 2025, Bojić 2026 | do not use persona panels; prompted LLM pairwise as a weak feature; prefer embeddings + linear model or LLM rationale features | (e) |
| BuzzProphet, TFM4POP, SMP winners | LLM-generated rationales/labels and embeddings as columns into a GBM or TabPFN-style model | (e) |
| deep cascade models | skip | (d), unavailable |
