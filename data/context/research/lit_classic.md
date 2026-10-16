# Classic popularity and cascade dynamics: what each paper implies for the harness

Reader: whoever builds `harness/context/` detectors. Question answered: which classic (mostly pre-2018) results turn into a feature or detector we can compute, and from which data.

Data regimes used below:

- (a) final counts only (the CSV: final engagement, date, text, media; exact post time from the Snowflake ID).
- (b) reply/quote arrival times decoded from Snowflake IDs, plus each replier's follower count. For an old post this reconstructs the reply-count time series after the fact, if the reply list is complete.
- (c) repeated snapshots of a live post's likes/reposts/replies/quotes/bookmarks/views.
- (d) full retweet cascade with timestamps and the follower graph. We do not have this.

[verified] means I read the claim in the paper text (PDF via arXiv or the publisher) or its abstract during this session. [recalled] means it is from memory and was not checked. "Implication" lines are my inference for this harness, not a claim from the paper.

Cross-cutting caveats for (b):

1. Replies are not retweets. Every model below that uses resharer follower counts assumes the event exposes the resharer's followers. A reply mostly reaches people reading the thread, not the replier's followers [recalled, X product behaviour, unverified]. Quotes do broadcast to the quoter's followers, so quotes are the better stand-in for reshares.
2. Check reply-list completeness per post: `len(reply_list) / reply_count`. If a keyless endpoint returns a ranked or capped subset, arrival-time fits are biased toward early, high-ranked replies.
3. Power-law exponent fits need roughly 50 or more events after the peak to be usable. Most dev-tool posts will have far fewer replies, so fit θ pooled per platform or per account, and classify individual posts against pooled values.
4. The team's data note says the CSV has no posts with engagement 11-99. Any check that the size distribution follows a power law (needed for Cheng's doubling equivalence) has to account for that gap.

---

## 1. Crane & Sornette 2008, "Robust dynamic classes revealed by measuring the response function of a social system"

- PNAS 105(41):15649, 2008. arXiv 0803.2189, https://arxiv.org/abs/0803.2189 (full text read via the arXiv PDF). [verified]
- Finding: about 90% of ~5M YouTube daily-view series look like noise (Poisson). The other ~10% (~500k) relax after a burst as a power law whose exponents fall into three classes set by one memory exponent θ = 0.4 ± 0.1. [verified]
- Model: self-exciting Hawkes process λ(t) = V(t) + Σ_{t_i ≤ t} μ_i φ(t − t_i). Bare memory kernel φ(t) ~ 1/t^(1+θ), 0 < θ < 1. μ_i is the number of people that viewer i directly influences, and mean μ near 1 means critical. [verified]
- Exponent rules for the activity A(t) around the peak at t_c: [verified]
  - Exogenous subcritical (⟨μ⟩ < 1): A ~ 1/(t − t_c)^(1+θ). With θ = 0.4, p = 1.4.
  - Exogenous critical (⟨μ⟩ ≈ 1): A ~ 1/(t − t_c)^(1−θ), p = 0.6.
  - Endogenous critical: A ~ 1/|t − t_c|^(1−2θ) both before and after the peak, so power-law precursory growth and an almost symmetric decay, p = 0.2.
  - Endogenous subcritical: A ~ η(t), noise.
- Classification rule: F = views on the peak day / total views. Class 1: 80% ≤ F ≤ 100% (exo-subcritical). Class 2: 20% < F < 80% (exo-critical). Class 3: 0% ≤ F ≤ 20% (endo-critical). Exponent histograms within each class peak at p ≈ 1.4, 0.6 and 0.2. The paper says these results are robust to changes of the threshold percentages. Validation: a peak-aligned, per-video-normalized average shows precursory growth only for the p ≈ 0.2 group. [verified]
- Companion paper, Sornette, Deschâtres, Gilbert, Ageon 2004, "Endogenous versus exogenous shocks in complex networks: an empirical test using book sale ranking", PRL 93:228701, arXiv cond-mat/0310135. Amazon sales give θ = 0.3 ± 0.1 with the same exo 1−θ and endo 1−2θ laws. The dressed kernel behaves as 1/t^(1−θ) for t < t* ∝ 1/(1 − n)^(1/θ), then as 1/t^(1+θ) after t*, where n is the branching ratio. [verified]
- Implied detector, "relaxation-class classifier", on reply arrival times:
  1. Estimate θ for X once, pooled. Use the reply-delay distribution of clearly subcritical posts (small accounts, low engagement), where the decay is the bare 1+θ. SEISMIC's Twitter retweet kernel gives θ = 0.242 as a prior (see entry 5). That prior predicts p_exo-sub ≈ 1.24, p_exo-crit ≈ 0.76 and p_endo ≈ 0.52.
  2. Per post, with τ_i = reply time − post time in seconds, find the peak of the binned reply rate (log-spaced bins, or activity-clock bins per entry 9). Let t_c be the peak bin start.
  3. Fit λ(t) = K (t − t_c + c)^(−p) on [t_c, T] by inhomogeneous-Poisson maximum likelihood: log L = Σ_i log λ(t_i) − ∫_{t_c}^{T} λ(t) dt. K has a closed form given (p, c), so profile it out and grid-search p ∈ [0.1, 2], c ∈ [10 s, 10 min]. A cruder fallback is OLS on log rate vs log time over log bins.
  4. Assign the class whose exponent (1+θ, 1−θ, 1−2θ) is nearest. Also compute F_bin = replies in the peak bin / total replies, with thresholds re-tuned for the bin width, because the paper's 80/20 cutoffs assume daily bins over months.
  5. Precursory growth: a peak more than about 2 bins after posting, preceded by rising reply rates, is the endogenous signature. A single tweet is itself a delta shock at t = 0, so most posts will be exogenous by construction. A late, rising peak usually means a second shock (a big-account quote) or algorithmic resurfacing.
  6. Crossover: if the fitted slope steepens from about 1−θ to about 1+θ at time t*, a larger t* means the branching ratio n is closer to 1.
- Regime: (b) for replies/quotes. (c) works for likes/views at coarse resolution. Not computable from (a).
- What it gives the harness: a label per historical post (direct-audience-only, cascading, or slow-burn word of mouth) usable as a target or feature. Exo-subcritical with high F plus views far above the follower base is one leg of a boost-suspect rule (entry 6).

## 2. Lehmann, Gonçalves, Ramasco, Cattuto 2012, "Dynamical classes of collective attention in Twitter"

- WWW 2012, p. 251. arXiv 1111.1896, https://arxiv.org/abs/1111.1896 (full text read). [verified]
- Finding: hashtag popularity peaks fall into 4 discrete classes defined by how activity divides before, during and after the peak. Epidemic spreading over the follower graph plays a minor role, and popularity is mostly driven by exogenous factors. [verified]
- Method: for each hashtag, compute daily counts n(i). Use a sliding window of 2L+1 days, L = 30, with median baseline n_b. Peak if p(i0) = [n(i0) − n_b] / max(n_b, n_min) > p_t, with p_t = 10 and n_min = 10. Keep only isolated peaks, more than a week from any other. Summarize the two weeks centred on the peak by (f_b, f_p, f_a), the fractions of tweets before, on and after the peak day, with f_b + f_p + f_a = 1. Cluster in (f_b, f_a) with a Gaussian mixture fitted by EM. BIC and 10-fold cross-validation both pick 4 components. [verified]
- Classes. No hard thresholds; class membership is GMM assignment. [verified]
  - Anticipatory: activity before and on the peak, sharp drop after (e.g. #masters). Scheduled events.
  - Reactive: activity on and after the peak (e.g. #winnenden). Unexpected events, likely driven by mass media.
  - Symmetric: build-up and decay of similar size (e.g. #watchmen, release day).
  - Transient: almost all activity on the peak day (e.g. #nsotu, #gfail).
- Implication: at post level f_b ≈ 0 by construction, so this detector belongs at topic level. Compute (f_b, f_p, f_a) for the narrative's volume series: mention counts built from reply/quote/search timestamps, Wikipedia pageviews or GDELT, which Sam's context plan already pulls. The class then sets timing. Anticipatory topics (a product launch, the Finals starting Oct 17) reward posting before the peak. Reactive topics reward speed after onset. Transient topics punish anything later than the same day. Use the paper's peak test (p > 10 over a 61-day median) to decide that a topic is peaking at all.
- Regime: topic volume series over time from (b) aggregated across posts, (c), or external sources. Post level: not useful.

## 3. Yang & Leskovec 2011, "Patterns of temporal variation in online media"

- WSDM 2011. PDF http://cs.stanford.edu/people/jure/pubs/memeshapes-wsdm11.pdf (full text read). [verified]
- Method, K-SC (K-Spectral Centroid) clustering: k-means-like clustering under a distance that is invariant to scale and time shift, d(x, y) = min_{α,q} ||x − α y_(q)|| / ||x||. The centroid is the smallest eigenvector of a matrix built from the cluster members. Series are truncated to 128 hours with the peak placed at 1/3 of the window. The data are 170M+ news and blog posts (Memetracker phrases) and 580M tweets (hashtags). [verified]
- Six shapes, C1 largest to C6 smallest: [verified]
  - C1: quick rise, then monotone decay. The most spread out of the single-peak shapes; looks like the average phrase.
  - C2: narrower than C1. Rise and decay at about the same rate.
  - C3: very fast rise in the hour before the peak, then slower decay than C1 or C2.
  - C4: big peak on day one, smaller peak about 24 h later.
  - C5: the opposite of C4. Small peak on day one, bigger peak on day two.
  - C6: stays popular for more than 3 days after the peak, with slowly declining local peaks.
  The three largest clusters peak for less than a day. Twitter hashtag clusters T1-T6 nearly match C1-C6. Which media types mention a phrase first predicts its cluster with 77-89% accuracy as more websites are added as features. [verified]
- Implied detector: run K-SC (k ≈ 6) on the reconstructed reply-rate curves of the 1,665 posts, using activity-clock bins (entry 9). Use cluster membership as a label, and flag C4/C5-like rebounds, a second peak about a day later. A rebound means resurfacing (algorithmic recirculation, a later big-account quote). For live posts, a rebound detected from snapshots means the post is worth replying under again or quote-boosting. Long-lived C6 shapes mark evergreen content. Szabo & Huberman (entry 4) show early prediction is least accurate for this kind.
- Regime: (b) retroactively for all posts. (c) for live posts. Too few replies make the shapes noisy, so cluster pooled engagement curves where possible.

## 4. Szabo & Huberman 2010, "Predicting the popularity of online content"

- Communications of the ACM 53(8), 2010. arXiv 0811.0405, https://arxiv.org/abs/0811.0405 (full text read). [verified]
- Finding: log popularity early and late is strongly linearly correlated. For Digg, votes in the first 2 hours forecast popularity 30 days ahead; YouTube needs about 10 days of views for the same accuracy. Content that decays fast is predicted best, and evergreen content worst. [verified]
- Model: ln N_s(t2) = ln r(t1, t2) + ln N_s(t1) + ξ_s(t1, t2). The regression slope is fixed at 1, and ξ is additive Gaussian noise on the log scale. Indicator time t_i: 1 digg hour for Digg, 7 days for YouTube. Reference time t_r: 30 days. Pearson correlation of log popularities: YouTube 0.92 after about 5 days (0.7 untransformed); Digg 0.98 after the 5th digg hour. A "digg hour" is the time the site takes to accumulate 5,478 diggs, its average hourly digg volume, so time is measured in site activity rather than wall-clock time. [verified]
- Implied detector, the early-to-late predictor: ln E_final = a_b + ln X(t_i) + ξ, where X(t_i) is replies, or engagement from snapshots, at indicator time t_i. Fit a_b per account-size bucket b. Use the residual SD of ξ as the forecast's confidence band. Choose t_i on the data by finding where corr(ln X(t_i), ln E_final) plateaus. There is no evidence yet for X dev-tool posts; with Twitter's short reaction times (entry 5) I expect 1-2 hours to be enough. The extension in Pinto, Almeida, Gonçalves, WSDM 2013 (citation [verified]; method [recalled]) gives each early interval its own weight: E_final ≈ Σ_k w_k Δx_k.
- Regime: (b) retroactively, with replies as the early indicator and the CSV's final engagement as the target. (c) for live posts, the main live use. Not (a).

## 5. Zhao, Erdogdu, He, Rajaraman, Leskovec 2015, "SEISMIC: a self-exciting point process model for predicting tweet popularity"

- KDD 2015. arXiv 1506.02594, https://arxiv.org/abs/1506.02594 (full text read). [verified]
- Finding: needs no training features. Using only reshare times t_i and the resharers' follower counts n_i, it gives about 15% relative error on final size after 1 hour of observation, beating feature-based baselines, especially in the first hour. [verified]
- Model: λ_t = p_t Σ_{t_i ≤ t} n_i φ(t − t_i). Memory kernel φ(s) = c for 0 < s ≤ s0, and c (s/s0)^(−(1+θ)) for s > s0. On Twitter, s0 = 5 min, θ = 0.242 and c = 6.27×10⁻⁴ (per second, chosen so φ integrates to 1); the kernel is estimated once for the whole network. Infectiousness estimate: p̂_t = Σ_i K_t(t − t_i) / Σ_i n_i ∫_{t_i}^{t} K_t(t − s) φ(s − t_i) ds, with triangular kernel K_t(s) = max(1 − 2s/t, 0). The kernel throws away the explosive first half of the history. Prediction: R̂∞ = R_t + α_t p̂_t (N_t − N_t^e) / (1 − γ_t p̂_t n*), where N_t = Σ n_i, N_t^e = Σ n_i ∫ φ, and n* is the mean degree. Correction factors: γ_t n* = 20 for all t; α_t is learned per t. Criticality threshold p* = 1/n*. [verified]
- Can it run on reply arrivals plus repliers' follower counts? The arithmetic runs. The exposure term is wrong for replies, because a reply does not expose the replier's followers the way a retweet does (caveat 1). Usable variants, in order of preference. These are my design, not tested:
  1. With (c): replace inferred exposure with observed exposure. p̂ = ΔE/ΔV, engagements per new view, weighted by the same triangular kernel over snapshots. This measures directly what SEISMIC has to infer.
  2. With quotes and quoter follower counts: SEISMIC as written, with quotes as the reshares and n_0 = author followers. Quotes undercount reshares, so treat R̂ as a lower bound and calibrate α_t on the CSV.
  3. Replies only: set the exposure to the author's followers n_0 alone, n_0 ∫ φ(s) ds, and read p̂ as replies per effective exposure. Use it as a ranking feature, not an absolute size estimate. Replier follower counts are better spent on entry 8.
- Regime: designed for (d). Works approximately on (b) with quotes. Variant 1 needs (c).

## 6. Hawkes intensity process (HIP): Rizoiu, Xie, Sanner, Cebrian, Yu, Van Hentenryck 2017, "Expecting to be HIP: Hawkes intensity processes for social media popularity"

- WWW 2017. arXiv 1602.06033, https://arxiv.org/abs/1602.06033 (full text read). [verified]
- Finding: daily YouTube views are explained as an endogenous response to exogenous promotion, measured as tweets that share the video. Each video gets two numbers: exogenous sensitivity μ and endogenous response. The model cut average forecast error by 28.6% versus history-only methods, given known promotions. [verified]
- Model: ξ(t) = μ s(t) + C ∫_0^t ξ(t − τ) (τ + c)^(−(1+θ)) dτ. Discrete form: ξ[t] = μ s[t] + C Σ_τ ξ[t − τ] (τ + c)^(−(1+θ)). C = κ(α − 1)/(α − β − 1). The user-influence exponent α = 2.016 was estimated from Twitter. HIP is linear and time-invariant, so ξ[t] = Σ_τ s[τ] ξ̂[t − τ], where the impulse response ξ̂ solves ξ̂[t] = 1[t] + C Σ ξ̂[t − τ](τ + c)^(−(1+θ)). The endogenous response is Σ_t ξ̂[t]. Unobserved external influence is absorbed by extra parameters γ and η. Example fits show θ ranging from 0.41 (slow memory decay) to 5.37 (fast). [verified]
- Implied detector, "boost / exogenous injection": with snapshots of views V(t) (c), fit the endogenous part from the post's own history and observed drivers s(t). Candidate drivers: quotes by large accounts (from (b)), and topic volume (entry 2). Large positive residual bursts in views that come with a drop in engagement per view, and no matching quote or topic driver, are the signature of paid promotion or algorithmic injection. μ for organic drivers then says how responsive an account's audience is to amplification. A crude version for (a) plus final views: flag posts whose views/followers is far above the author's median while engagement/views is far below it. Pair this with entry 1's exo-subcritical class.
- Regime: (c) needed for the real version. (a) gives only the crude ratio flag.

## 7. Mishra, Rizoiu, Xie 2016, "Feature driven and point process approaches for popularity prediction"

- CIKM 2016. arXiv 1608.04862, https://arxiv.org/abs/1608.04862 (full text read). [verified]
- Finding: a marked Hawkes process fitted per cascade, with a random-forest prediction layer on top, beats SEISMIC and feature-only models on tweet cascades. A basic set of user features plus event-time summary statistics is competitive, and adding the point-process parameters improves it further. [verified]
- Model: kernel φ_m(τ) = κ m^β (τ + c)^(−(1+θ)), where the mark m is the user's follower count. Branching factor n* = κ (α − 1)/(α − β − 1) · 1/(θ c^θ), for β < α − 1 and θ > 0. Expected final size N∞ = n + [κ / (1 − n*)] Σ_i m_i^β / (θ (T + c − t_i)^θ), for n* < 1. [verified]
- Implied feature: fit (κ, β, c, θ) per post by maximum likelihood on quote arrivals (better) or reply arrivals (proxy), with marks = follower counts. Use n* as a virality score and N∞ as a size estimate, then feed both, with author baseline and time-to-k features, into the harness's regressor. That last step is the paper's own recommendation. With few events per post, fix θ and c at pooled values and fit only κ.
- Regime: (d) as designed. (b) as a proxy with the caveat-1 bias.

## 8. Rizoiu, Mishra, Kong, Carman, Xie 2018, "SIR-Hawkes: linking epidemic models and Hawkes processes to model diffusions in finite populations"

- WWW 2018. arXiv 1711.01679, https://arxiv.org/abs/1711.01679 (abstract and model read). [verified]
- Finding: the event rate of an extended Hawkes model equals the SIR new-infection rate once the unobserved recovery events are marginalized out. HawkesN adds a finite population N: λ(t) = (1 − N_t/N)(μ + Σ φ(t − t_i)). The cascade-size distribution tends to be bimodal, with one mode near zero and one at large sizes. [verified]
- Implication: fit N per account, pooled, to get an audience ceiling, which matters for niche dev-tool accounts. Because outcomes are bimodal, predict P(takes off) together with a conditional size, rather than one point estimate.
- Regime: (b) or (c).

## 9. Kobayashi & Lambiotte 2016, "TiDeH: time-dependent Hawkes process for predicting retweet dynamics"

- ICWSM 2016. arXiv 1603.09449, https://arxiv.org/abs/1603.09449 (full text read). [verified]
- Finding: a SEISMIC-style process whose infectious rate has a daily cycle and ages over time improves retweet-profile prediction in all time regimes. [verified]
- Model: SEISMIC kernel with c0 = 6.49×10⁻⁴ per second, s0 = 300 s and θ = 0.242. Infectious rate p(t) = p0 [1 − r0 sin(2π(t + φ0)/T_m)] e^{−(t − t0)/τ_m}, with T_m = 1 day, −1 < r0 < 1 and 0.5 < τ_m < 20 days. Fitted with Levenberg-Marquardt on 4-hour windows. Parameter estimates are poor for observation windows under 24 h, so for prediction the shape parameters (r0, φ0, τ_m) are fixed from training data. [verified]
- Related: SpikeM (entry 10) uses the same daily multiplier, and Szabo-Huberman's "digg hours" (entry 4) are the same idea as activity time.
- Implied feature, the activity clock: estimate the audience's hour-of-week activity profile a(h) from all reply timestamps in the dataset. Convert every delay to activity time t' = ∫ a(u) du before fitting entries 1, 3, 4, 5 and 7, so a post at 2 a.m. Pacific isn't misread as slow. Posting-hour activity a(h_post) is also a direct feature for timing recommendations.
- Regime: (b), with the profile pooled from the whole dataset.

## 10. Matsubara, Sakurai, Prakash, Li, Faloutsos 2012, "Rise and fall patterns of information diffusion: model and implications" (SpikeM)

- KDD 2012, pp. 6-14. PDF https://sites.cs.ucsb.edu/~lilei/pubs/matsubara2012rise.pdf (model section read). [verified]
- Model: ΔB(n+1) = p(n+1) · [U(n) Σ_{t=n_b}^{n} (ΔB(t) + S(t)) f(n+1−t) + ε]; U(n+1) = U(n) − ΔB(n+1); f(τ) = β τ^(−1.5). The shock S equals S_b at birth time n_b. Periodicity: p(n) = 1 − ½ P_a [sin(2π(n + P_s)/P_p) + 1]. The population N is finite. Seven parameters reproduce all six Yang-Leskovec shapes, and the model supports forecasting, what-if analysis and anomaly detection. [verified]
- Implication: a compact generative fit to a post's binned reply or engagement curve, where β·N plays the role of R0 and residual spikes are anomalies, i.e. second shocks. In practice it overlaps entries 1, 3 and 9. Use it if a single parametric curve fitter is wanted for both forecasting and anomaly flags.
- Regime: (b) or (c).

## 11. Cheng, Adamic, Dow, Kleinberg, Leskovec 2014, "Can cascades be predicted?"

- WWW 2014. arXiv 1403.4608, https://arxiv.org/abs/1403.4608 (full text read). [verified]
- Formulation: having seen the first k reshares, predict whether the cascade reaches the median final size f(k) of all cascades with at least k reshares. Random guessing scores 50%. When the size distribution follows a power law with α ≈ 2, the median is f(k) = 2^{1/(α−1)} k = 2k, so the task is "will it double?". [verified]
- Findings on Facebook photo cascades, k = 5: accuracy 0.795 and AUC 0.877 with all features. Temporal features alone come within 0.025 of all features. The best single feature is the reshare rate over the second half of the first k reshares (time′_{k/2..k}, accuracy 0.73). Next come the view rate and time_5, the time from the original post to the 5th reshare (0.72 each). Early breadth beats depth. Prediction gets easier as k grows. [verified]
- Implied detector, the "doubling" detector, for live posts and backtests: features time_k (k = 5 replies or quotes), mean inter-arrival time over the first and second halves, their ratio (acceleration), and views per unit time. Target: final engagement ≥ 2× current engagement. Live, from snapshots (c): the same ratio on engagement increments. Before trusting the 2k equivalence, check that the CSV's size distribution has α ≈ 2, given the 11-99 gap.
- Regime: (b) retroactively. (c) live.

## 12. Shulman, Sharma, Cosley 2016, "Predictability of popularity: gaps between prediction and understanding"

- ICWSM 2016. arXiv 1603.09436, https://arxiv.org/abs/1603.09436 (abstract and features read). [verified]
- Finding: models that peek at the first k adopters reach up to 83% accuracy on Twitter and transfer across domains. Most of the power comes from how quickly an item reaches its first few adopters. Even the direction of correlation of the other early-adopter and network features is inconsistent across domains. Under "temporal matching", which compares items that reached k adopters in about the same time, accuracy falls below 65%. [verified]
- Implication for the harness eval: when grading content or drafting quality, compare posts inside the same early-velocity bin, and the same author baseline. Otherwise the predictor is just reading speed. Treat any content-only predictor as weak by default.
- Regime: (b) for the time to the k-th reply. (a) for the content side.

## 13. Martin, Hofman, Sharma, Anderson, Watts 2016, "Exploring limits to prediction in complex social systems"

- WWW 2016. arXiv 1602.01013, https://arxiv.org/abs/1602.01013 (full text read). [verified]
- Finding: with rich user, content and past-performance features, the best ex-ante Twitter cascade-size model explains less than half the variance: R² 0.42 unrestricted, up to 0.48 restricted. Simulations on a Twitter-like scale-free graph (degree exponent 2.05, SIR contagion) show that an upper bound well below 1 remains even with perfect knowledge. [verified]
- Implication: use R² ≈ 0.5 as the sanity ceiling for any ex-ante predictor of log engagement. An eval reporting much higher numbers without peeking is probably leaking.
- Regime: (a).

## 14. Bakshy, Hofman, Mason, Watts 2011, "Everyone's an influencer: quantifying influence on Twitter"

- WSDM 2011, pp. 65-74, https://dl.acm.org/doi/10.1145/1935826.1935845. Abstract read via search snippets; R² figures read in Martin et al.'s summary. [verified]
- Finding: the largest cascades come from users with past influence and many followers, but predicting which user or URL will cascade is unreliable. Past success of the seed is by far the most informative feature, and content adds little after it. R² ≈ 0.34 even though the model is well calibrated on average (R² ≈ 0.98 on averages). Under plausible costs, "ordinary influencers" are most cost-effective. [verified]
- Implication: the author's own trailing baseline (Sam's `eng_ratio_author_median`) should be the first feature and the normalizer for every target. For reply-target selection, spread across many mid-size targets rather than chasing the largest account.
- Regime: (a).

## 15. Goel, Anderson, Hofman, Watts 2016, "The structural virality of online diffusion"

- Management Science 62(1):180-196. PDF https://5harad.com/papers/twiral.pdf (read). [verified]
- Finding: structural virality, the Wiener index (mean pairwise distance in the diffusion tree), varies widely across a billion Twitter events but is typically low at every size. Its correlation with size is 0.2 for news, 0.04 for petitions and about 0 for pictures and videos. "If popularity is consistently related to any one feature, it is the size of the largest broadcast." [verified]
- Implied detector, the "largest broadcaster": max follower count among early quoters (and repliers), the time it arrived, and the step change in reply/engagement rate after it, from (b) plus (c). This is the observable part of Goel's finding. The Wiener index itself needs (d).
- Regime: (b) for the proxy. (d) for structural virality proper.

## 16. Wu & Huberman 2007, "Novelty and collective attention"

- PNAS 104(45):17599, 2007. arXiv 0704.1158, https://arxiv.org/abs/0704.1158 (read). [verified]
- Finding: Digg story attention follows multiplicative growth damped by a novelty factor r_t. The fit is a stretched exponential, r_t ~ e^{−0.4 t^0.4}, with half-life τ ≈ 69 minutes. Final popularity is roughly log-normal. [verified]
- Implied feature: a freshness-decay curve for the reply lane. Plot each reply's own engagement against its delay after the parent post, available when reply engagement can be read, and fit a stretched exponential. This gives the cost of replying late to a target post, and the cutoff after which a reply isn't worth drafting.
- Regime: (b), plus each reply's likes if readable.

## 17. Weng, Flammini, Vespignani, Menczer 2012, "Competition among memes in a world with limited attention"

- Scientific Reports 2:335, https://www.semanticscholar.org/paper/1fc5873bdb5532f4cf64ced4d6632757b00ed318. Abstract read via search snippets. [verified, abstract only]
- Finding: an agent-based model with limited attention plus network structure reproduces the heterogeneous popularity and lifetimes of Twitter memes without assuming intrinsic quality differences. [verified]
- Related: Gleeson, Ward, O'Sullivan, Lee 2014, PRL, arXiv 1305.4328. Competition for limited attention poises the system at criticality, and popularity follows a critical branching process with heavy tails, exponent α < 2. [verified] Also Gleeson, Cellai, Onnela, Porter, Reed-Tsochas 2014, "A simple generative model of collective online behavior", PNAS 111(29):10411. Only models that weight recent popularity, the last day or two, over cumulative popularity reproduce the Facebook-app dynamics. [verified, abstract]
- Implied feature, crowding: the number of posts from competing accounts on the same narrative in the same hour or day, using exact post times from Snowflake IDs across the 91 accounts. In momentum features, weight recent velocity over cumulative counts.
- Regime: (a) for crowding. (c) for the recency-weighted momentum.

## 18. Weng, Menczer, Ahn 2013, "Virality prediction and community structure in social networks"

- Scientific Reports 3:2522. arXiv 1306.0158, https://arxiv.org/abs/1306.0158 (abstract read). [verified]
- Finding: most memes spread like complex contagions within communities. Viral memes spread across many communities early, and early community concentration predicts future popularity. [verified]
- Implied feature, "outsider share": the fraction of early repliers or quoters who have never interacted with the author before. Build reply-author history from reply lists across the dataset. A cheap stand-in for community spread when the follower graph is unavailable.
- Regime: (b), with reply-author identities across many posts.

## 19. Romero, Meeder, Kleinberg 2011, "Differences in the mechanics of information diffusion across topics"

- WWW 2011. PDF https://www.cs.cornell.edu/home/kleinber/www11-hashtags.pdf (abstract read). [verified]
- Finding: hashtag adoption differs by topic in "stickiness", the adoption probability, and in "persistence", the extent to which repeated exposures keep having marginal effects. Political and controversial hashtags are unusually persistent, which is evidence for complex contagion. Idioms are non-persistent. [verified]
- Implication: computing exposure curves p(k) needs (d). The usable lesson: for contentious narratives, repeated posts on the same theme keep paying off; for idiom-like memes, the first post is the one that counts.
- Regime: (d) for the metric. Qualitative only here.

## 20. Shen, Wang, Song, Barabási 2014, "Modeling and predicting popularity dynamics via reinforced Poisson processes"

- AAAI 2014. arXiv 1401.0778, https://arxiv.org/abs/1401.0778 (model read). [verified]
- Model: the rate for item d after i events is λ_d f_d(t; θ_d)(m + i − 1). λ_d is fitness, f_d is a relaxation function (log-normal for citations), m is a prior pseudo-count, and a conjugate prior on λ_d prevents overfitting. [verified] The closed form for cumulative count, c_d(t) = m(e^{λ_d F_d(t)} − 1), is [recalled].
- Follow-up: Gao, Ma, Chen 2015, "Modeling and predicting retweeting dynamics on microblogging platforms", WSDM 2015. Citation [verified]. That it adapts the reinforced Poisson process to Weibo with power-law relaxation and a daily time mapping is [recalled].
- Implication: per-post fitness λ_d from reply arrivals is an alternative to Hawkes n*. The conjugate prior suits our few-event posts, since the prior can come from the account's pooled fit.
- Regime: (b).

## 21. Smaller or ex-ante entries

- Zaman, Fox, Bradlow 2014, "A Bayesian approach for predicting the popularity of tweets", Annals of Applied Statistics 8(3). arXiv 1304.6777 (abstract read). [verified] Retweet times plus retweeters' local graph give good forecasts after seeing less than a tenth of the retweet path, within minutes. Same regime as SEISMIC: (d), or (b) with quotes.
- Lerman & Hogg 2010, "Using a model of social dynamics to predict popularity of news", WWW 2010. arXiv 1004.5354 (abstract read). [verified] Modeling the site's interface (Digg front page) beats plain extrapolation of early votes. Implication: the For You ranking is an unmodeled exogenous driver, which is one more reason entry 6's residual detector matters.
- Bandari, Asur, Huberman 2012, "The pulse of news in social media: forecasting popularity", ICWSM 2012. arXiv 1202.0332 (abstract read). [verified] Ex-ante article features predict Twitter popularity ranges with 84% accuracy. Features include a source score, t-density, the source's historical tweets per link. That the source score dominates is [recalled]. Same lesson as Bakshy: source history first. Regime: (a).
- Hong, Dan, Davison 2011, "Predicting popular messages in Twitter", WWW 2011 Companion, pp. 57-58. Citation [verified]. Content (TF-IDF, LDA), user-graph and temporal features with logistic regression, classifying retweet volume buckets: [recalled]. Regime: (a).
- Petrovic, Osborne, Lavrenko 2011, "RT to win! Predicting message propagation in Twitter", ICWSM 2011, pp. 586-589. Citation and task, "will it be retweeted at least once", [verified]. That social features outperform tweet features: [recalled]. Regime: (a).
- Ma, Sun, Cong 2013, "On predicting the popularity of newly emerging hashtags in Twitter", JASIST 64(7):1399. Abstract via search: contextual features from adopters' social graph beat content features, and logistic regression is best. [verified] Implication: for narrative selection, who is already talking matters more than the wording. Regime: (b), adopter identities and follower counts.
- Ratkiewicz, Fortunato, Flammini, Menczer, Vespignani 2010, "Characterizing and modeling the dynamics of online popularity", PRL 105:158701. Abstract via search: popularity bursts are heavy-tailed, and are modeled as preferential growth plus random exogenous popularity shifts. [verified] Supports treating bursts as exogenous shocks layered on a baseline.
- Pinto, Almeida, Gonçalves 2013, WSDM: see entry 4.

---

## Ranked detectors for a harness with reply timestamps and snapshots

1. Early speed and doubling (entries 4, 11, 12, plus 9's activity clock). Inputs: time_k, half-rate ratio, and ln X(t_i) → ln E_final regression with author-bucket intercepts. Regime (b) backfilled, (c) live. [verified]
2. Engagement per new view, SEISMIC with observed exposure (entry 5, variant 1). Regime (c); a quote-based proxy in (b). Formula [verified]; the adaptation is untested.
3. Relaxation-class classifier (entry 1). Inputs: post-peak exponent mapped to 1+θ / 1−θ / 1−2θ, peak fraction F, precursory growth, crossover t*. Regime (b). [verified]
4. Boost / exogenous-injection residual (entry 6, plus entry 1's exo-subcritical class). Regime (c); crude ratio flag in (a). Model [verified]; detector design is inference.
5. Largest broadcaster (entries 15, 14). Inputs: max follower count among early quoters and repliers, its arrival time, and the rate step after it. Regime (b). [verified]
6. Marked-Hawkes n* and N∞, HawkesN audience ceiling (entries 7, 8). Regime (b) proxy. Formulas [verified]; reply-proxy validity unverified.
7. Activity clock and posting-hour feature (entries 9, 10, 4). Regime (b). [verified]
8. Shape and rebound clustering at post level (entry 3), and anticipatory/reactive topic class for narrative timing (entry 2). Regime (b)/(c) plus external topic series. [verified]

Sanity bounds for the eval: R² for ex-ante log engagement should sit near or below 0.5 (entries 13, 14). Content-only signals should be judged within matched early-velocity bins (entry 12).
