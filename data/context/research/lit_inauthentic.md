# Detecting inauthentic amplification of a single X launch post

Research notes, 2026-10-15. Reader: whoever builds the "boosted launch" flag in the harness. Question answered: which rules from the coordination, bot-detection and fake-engagement literature can run on the data we can fetch keylessly, what threshold each uses, and how each one misfires on honest launches.

Tags: [verified] means I read the claim in the paper text or the official doc during this session. [verified-snippet] means a search result quoted the source but I did not open it. [recalled] means from memory, not checked today. [ours] means a threshold I propose with no calibration run behind it; replace it with a percentile from our own corpus before trusting it.

## 1. What our data can and cannot see

Available per post: final and snapshot counts of likes, reposts, replies, quotes, bookmarks, views; author follower count; reply list (ID, text, author handle, author followers, verified flag); reply-author profiles on request (joined date, statuses, following, likes, media count, bio). Reply time comes from the Snowflake ID: `ms = (id >> 22) + 1288834974657` [recalled, standard].

Not available: who liked or reposted, repost times, impression time series, ad library.

Consequences that shape every rule below:

- Most CIB methods (Pacheco 2021, Schoch 2022, Keller 2020, CopyCatch, CooRnet) link accounts by shared actions across many items. Likes and reposts are the actions boost sellers sell, and since June 2024 X shows only the author who liked a post (XEng, 11 June 2024; Time, CBS) [verified-snippet]. So network methods only run on replies, and only across many posts in our corpus.
- A paid like, repost or view package leaves no reply trace. The only handle on it is a distortion in the count mix (likes vs reposts vs replies vs views) or a step in repeated snapshots.
- X's public counts mix organic and paid. The API docs say public metrics on a promoted post are the "Combined total (organic + promoted)", while the split needs user-context auth on the account that ran the ad and is only kept 30 days (docs.x.com/x-api/fundamentals/metrics) [verified]. Views are not unique: repeat views and multi-device views count twice; embeds do not count (help.x.com view-counts page) [verified-snippet].
- Engagement campaigns bill only "first engagements" and leave the organic ones that follow free (business.x.com engagements campaign page) [verified-snippet]. A promoted post's likes are therefore partly paid and partly organic, with no public split.
- Promoted-only posts ("dark posts") are not shown on followers' timelines or in search; they work like normal posts for whoever is targeted (X ads help, via search snippet) [verified-snippet].
- X ranks Premium replies higher (Basic slight, Premium larger, Premium+ largest; X's own wording quoted by TechMoran: "a slight preference for replies from verified accounts") [verified-snippet]. A keyless reply fetch probably returns a ranked and spam-filtered subset, with "probable spam" replies hidden [recalled]. Our reply sample is biased against exactly the spam we want to count.
- Likes went private in June 2024 and Musk said likes rose "massively" after (Time/CBS reporting) [verified-snippet]. Any author baseline that spans June 2024 has a level shift in like counts.

## 2. Literature, with what each contributes

### Coordination networks

**Pacheco et al. 2021, ICWSM, "Uncovering coordinated networks on social media" (arXiv 2001.05658)** [verified, read full text]
- Build a bipartite graph of accounts and behavioural traces, project to account-account similarity, keep the strongest edges, take connected components.
- Co-retweet case (White Helmets): drop accounts with fewer than 10 retweets, TF-IDF weight retweeted tweet IDs, cosine similarity, keep top 0.5% of edges.
- Synchronized-action case (crypto pump and dump): bin tweets into 30-minute intervals, require at least 8 tweets per account, cosine on TF-IDF of time bins, keep top 0.5% edges. They chose 30 minutes because shorter windows give too few matches and daily windows give "many false positive matches".
- Hashtag-sequence case: daily bins, at least 5 tweets and 5 unique hashtags per 24 hours.
- Two findings that matter for us: coordinated accounts often have low Botometer scores ("the majority in two of the three cases"), so bot scoring does not substitute for coordination detection; and "coordinated campaigns may be carried out by authentic users with benign intent", e.g. share buttons producing identical text and activists using hashtags together. Coordination is not proof of inauthenticity.

**Keller, Schoch, Stier & Yang 2020, Political Communication, "Political astroturfing on Twitter"** (South Korean NIS ground truth from court records) [verified, read full text]
- Co-tweet: same tweet text from two accounts within one minute. More than 85% of NIS co-tweets fell within one minute and almost 99% within one work day.
- Co-retweet is looser: only 20% of NIS co-retweets were within one minute, 99% within a week. They still used one minute because repeated same-minute co-retweeting is rare for real users.
- Ties from a single co-tweet were dropped. About 40% of the campaign's retweets came from inside the campaign: astroturf mostly amplifies itself.

**Schoch, Keller, Stier & Yang 2022, Scientific Reports, "Coordination patterns reveal online political astroturfing across the world"** (PMC8930979) [verified, read full text]
- Co-tweet: "two accounts posting the same message within a short (here, one minute) time window". Co-retweet tie: same retweet within one minute, "but only if this occurs at least ten times".
- Across 33 Twitter-released campaigns with 50,000+ tweets, 74% of campaign accounts on average showed co-tweeting or co-retweeting; the same behaviour was "negligible" in activity-matched comparison samples, including an organic German vaccination campaign with some open coordination.
- False positives rise only when the window is relaxed a lot; they call anything under 8 hours reasonable.
- Plain retweeting (rather than co-retweeting) gave the most false positives.
- Astroturf accounts post during office hours and less on days off (principal-agent theory). Not usable on one post but usable on reply authors' history if we fetch timelines.

**Giglietto et al. 2020 / CooRnet** [verified, package docs]
- Coordinated link sharing: entities sharing the same URL "within an unusually short period of time compared to the entire dataset", repeatedly.
- The window is estimated from data: `estimate_coord_interval(q = 0.1, p = 0.5)` takes the 10% of URLs whose second share came fastest and returns the median seconds those URLs took to reach 50% of their shares. Pair must co-share repeatedly; `percentile_edge_weight = 0.90`.
- Rogers & Righetti 2025 describe typical windows as "a few seconds to a minute" [verified-snippet].
- Lesson for us: derive the reply-burst window from our corpus instead of fixing it.

**Weber & Neumann 2021, SNAM, "Amplifying influence through coordinated behaviour in social networks"** [verified-snippet only; Springer full text blocked]
- Latent coordination networks from several behaviours (co-retweet, co-mention, co-hashtag, co-URL, co-reply) in sliding time windows with a decay factor, then highly coordinating communities validated with one-class classifiers. Companion: Weber & Falzon 2021 "Temporal nuances of coordination networks" on window choice. Exact window lengths not checked; I recall 15-minute windows in their experiments [recalled].
- Lesson: co-reply is a legitimate coordination trace, and persistence across windows separates coordination from coincidence.

**Beutel et al. 2013, WWW, CopyCatch** [verified-snippet]
- Lockstep detection on Facebook page likes: groups of at least n users who each liked at least a fraction of the same m pages, with like times within a window (patent example: 24 hours).
- Ikram et al. 2017 later showed stealthy farms that spread likes over time and also like popular pages defeat it (47% precision on one farm). Needs liker lists we do not have, so it only informs the cross-post reply rule.

**Ratkiewicz et al. 2011, Truthy (arXiv 1011.3768)** [verified, read text]
- Classified astroturf memes from diffusion-network topology (nodes, edges, degree and strength stats, component structure) plus sentiment; about 96.4% accuracy on a small labelled set. The useful idea: astroturf is injected by few accounts and spreads in a small, dense, mention-heavy graph. Our analogue is the reply-author graph across posts.

### Temporal signatures

**Chavoshi, Hamooni & Mueen 2016, ICDM, DeBot** [verified-snippet]
- "Humans cannot be highly synchronous for a long duration; thus, highly synchronous user accounts are most likely bots." Warped (lag-tolerant) correlation of activity time series plus lag-sensitive hashing; 94% precision; unsupervised. Needs each account's long activity series, so for us it applies to reply authors seen on many posts, not to one post.

**Ghosh, Surachawala & Lerman 2011, "Entropy-based classification of retweeting activity" (arXiv 1106.0346)** [verified, read text]
- Two features per item: entropy of inter-arrival gaps between successive retweets, and entropy of the per-user retweet counts. Automated retweeting gives "an isolated peak or peaks" in the gap distribution or "bursty behavior with many zero second intervals" (low time entropy). Campaigns show a few users retweeting the same item repeatedly (low user entropy). Newsworthy content: many users once each, broad gaps.
- Directly usable on our reply list: gap entropy and author entropy.

**Crane & Sornette 2008, PNAS, "Robust dynamic classes revealed by measuring the response function of a social system"** [verified-snippet]
- YouTube daily views after a peak decay as a power law with class-specific exponents, θ about 0.4: exogenous subcritical ("junk", burst that does not spread) about 1+θ = 1.4; exogenous critical about 1-θ = 0.6; endogenous critical (word of mouth, with a symmetric build-up) about 1-2θ = 0.2.
- Use with repeated snapshots: a launch is exogenous by design, so expect a fast-decaying burst. A bought burst that does not cascade should decay fast (subcritical); a paced ad budget instead holds a flat rate for days, which no organic class predicts. This is my extrapolation; the paper says nothing about ads.

### Bot detection and its limits

**Varol et al. 2017, ICWSM (Botometer)** [verified, read text]
- 1,150 features in six classes (user metadata, friends, network, temporal, content, sentiment); estimated 9-15% of active accounts were bots then. Per-account features we can compute from a reply-author profile: account age, statuses, followers, following, likes, media count, bio emptiness.

**Cresci et al. 2017, WWW companion, "The paradigm-shift of social spambots"** [verified-snippet]
- Newer "social spambots" fooled Twitter, crowd annotators (who caught under 25% of them) and existing tools; Digital DNA (sequence similarity across accounts) reached F1 0.92 vs about 0.57 for human annotators. Profile features alone fail against modern bots.

**Cresci 2020, CACM, "A decade of social bot detection" (arXiv 2007.03604)** [verified, read text]
- Field moved from per-account supervised classifiers to group approaches that look for coordination and synchrony, because "botnets leave behind more traces of their automation and coordination than those left behind by sophisticated single bots". Open problems: small loosely coordinated groups and "groups of highly coordinated humans that might appear as automated". The second is exactly a launch-day employee push.

**Rauchfleisch & Kaiser 2020, PLOS ONE, "The false positive problem of automatic bot detection"** [verified, read text]
- Botometer at the 0.76 universal-score threshold: precision 0.59 and recall 0.2 on their pooled data (assuming 15% bots); precision 0.24 on German data. At CAP 0.25: precision 0.55.
- Over three months, 27.2% of new bots and up to 7.4% of German politicians crossed the 0.76 threshold in both directions. Recommendation: use bot scores only as one step in a human-checked process. For us: never label a single reply author a bot; compare distributions of reply-author features against a baseline.

### Purchased engagement

**De Cristofaro et al. 2014, IMC, "Paying for likes?"** [verified-snippet] and **Ikram et al. 2017, ACM TOPS, "Measuring, characterizing, and detecting Facebook like farms"** (arXiv 1707.00190) [verified, read text]
- Honeypot pages bought likes from farms and from Facebook ads. Two farm styles: bot-run farms delivering "bursts of a large number of likes" (three farms delivered within about two hours) and stealthy farms that trickle likes over time and like popular pages to look normal.
- Farm accounts liked 600-1,000 pages vs about 40 for regular users [verified-snippet]. Our analogue: reply author `likes count` far above norm.
- Farm timelines: 43% fewer words, poorer vocabulary, lower readability, more re-shares, "more often generate duplicate comments and likes". Lexical classifier reached at least 99% precision and 93% recall per the abstract.
- Facebook's own worldwide ad campaign drew likes mostly from India; paid ads can themselves attract farm accounts.

**Stringhini et al. 2013, IMC, "Follow the green"** [verified, read text]
- Follower markets: customers show sudden follower spikes then steady losses as victims unfollow; victims unfollow at much higher rates than normal users. Customers have a higher follower-to-following ratio. Our analogue: like counts that fall between snapshots after a jump.

**Weerasinghe et al. 2020, WWW, "The Pod People"** [verified, read full text]
- Engagement pods on Telegram: 1.8M Instagram posts, 111,455 accounts, 400+ pods. Rules require members to engage with every post shared in the last 24 hours (80% of "time pods"; others 6 or 12 hours) or within a one-hour round. Over half of users posted to a pod within about an hour of publishing.
- Classifier on posts with matched engagement levels: AUC 0.91 (likes-only pods) and 0.94 (comment+like pods). On a larger set, TPR 0.91; on the matched set TPR 0.87 at FPR 0.14.
- Features that mattered: topic-model probabilities of "generic support" comments ("great, shot, nice, love, wow, amazing, cool, awesome"), comment topic entropy, share of comments in each of the first 24 hours, time until a 24-hour span with zero comments, high comment and like counts, and "skewed comments-to-like ratio".
- False positives were posts with genuinely generic comments; false negatives came from pods that required on-topic or long comments. Authors suggest engagement level as a screen before a finer classifier.

**Dutta & Chakraborty 2019/2020, IEEE TIFS, blackmarket retweeters** [verified-snippet]
- Collusive (freemium "retweet for retweet") users mix organic and inorganic activity and show no synchrony, unlike bot-run retweet fraud. Synchrony rules miss reciprocity markets; content and author-overlap rules are the fallback.

### LLM-era reply spam on X

- **Yang & Menczer 2024, JQD:DM, "Anatomy of an AI-powered malicious social botnet" (fox8)** [verified-snippet]: 1,140 accounts found through accidental self-revealing ChatGPT refusal text; about 50% of their tweets were replies; they replied to and retweeted each other to inflate engagement.
- **Yang, Singh & Menczer 2024, JOTS, GAN-face profiles** [verified-snippet]: 1,420 accounts with GAN faces used for scams, spam and coordinated amplification; 0.021-0.044% of active accounts. Over 40 of a related set had paid blue checks and used ChatGPT (Sherwood News summary).
- **TechCrunch, Jan 2024** [verified-snippet]: only 202 accounts posted OpenAI refusal text in 30 days per Fedica, so self-revealing text has high precision and tiny recall.
- **X statements**: Nikita Bier said X purged 1.7M reply-spam bots in one week (Oct 2025) and 42,000 accounts "automating replies using chatbots" (July 2026), adding most were posting AI "thought-leadership" to "receive paid promotion offers from AI tech companies" [verified-snippet, his posts]. This is our domain: AI and dev-tool launches attract these accounts whether or not the launcher paid.
- **Münker et al. 2026, arXiv 2602.19177**: LLM-written X replies differ from human ones in lexical diversity, readability, POS mix and sentiment; plain TF-IDF features separate them well [verified-snippet]. Terčon 2025 survey: AI text is consistently less lexically diverse and more neutral [verified-snippet].
- Practitioner heuristics (Pigford analysis of 1M posts; ABC News/Graham) [verified-snippet, not peer reviewed]: paid check without legacy verification, very fast replies, many replies in a short window, young accounts.

### Platform takedown reports

- **Meta adversarial threat reports**: CIB networks comment on politicians' and media posts; Q1 2025 Romania network: "The majority of these comments received no engagement from authentic audiences" [verified-snippet]. 2024 Israel-based network posted comments "likely generated by AI" (Quartz on Meta) [verified-snippet]. Meta says its defences "focus on behavior rather than content". Takeaway: inauthentic replies get no likes and no replies of their own. If reply-level like counts are available, the share of zero-engagement replies from non-follower accounts is a cheap check; on small launch posts most replies get zero likes anyway, so it only works as a contrast to the author's baseline.

## 3. Detection rules

Each rule: signature, statistic and starting threshold, evidence, legitimate behaviour that trips it, strength. Thresholds marked [ours] are uncalibrated proposals.

General method, taken from Schoch 2022 and Pod People: compare against the author's own previous posts and a matched set of other launches in the corpus, never against a global constant. Report a rule as "fired" only when it exceeds the corpus 95th or 99th percentile for posts of similar view count.

### R1. Self-revealing machine text in replies (strong precision, low recall)
- Signature: replies containing LLM refusal or meta text ("as an AI language model", "I cannot fulfill", "regenerate response", unfilled template tokens like `{product}`), or the same reply posted under different handles.
- Threshold: any one hit from 2 or more distinct authors on one post fires; one hit is noted. [ours]
- Evidence: fox8 (Yang & Menczer 2024); TechCrunch/Fedica 2024 shows rarity.
- False positives: a reply quoting ChatGPT ironically; someone joking "as an AI language model". Check context.
- [verified-snippet] for the papers.

### R2. Near-duplicate replies from distinct authors (co-tweet inside one post) (strong)
- Signature: two or more accounts post the same or templated reply text.
- Statistic: character 5-gram MinHash Jaccard >= 0.8 (or embedding cosine >= 0.95) between replies from different authors, after stripping @mentions, URLs and emoji. Fire if >= 3 duplicate clusters, or >= 10% of replies sit in clusters, with >= 20 replies. Tighten with the Keller/Schoch time condition: duplicates within 60 s of each other are much stronger. [ours for numbers; one-minute window from Keller 2020 and Schoch 2022]
- Evidence: Keller 2020 (85% of NIS co-tweets within 1 minute), Schoch 2022 (co-tweeting negligible in comparison users), Ikram 2017 (duplicate comments), Pacheco 2021 (content similarity).
- False positives: one-word replies ("congrats!", "🔥", "LFG", "wow"). Exclude replies under ~5 tokens from this rule and handle them in R6. Giveaway or waitlist posts that ask people to reply with a fixed phrase ("reply 'beta' for access"); detect the call-to-action in the post text and suppress. Share buttons that prefill text (Pacheco's example).

### R3. Cross-post co-reply network (strong, needs corpus)
- Signature: the same accounts reply early to many different companies' launches, close in time to each other.
- Statistic: link two reply authors when they reply to the same post within 60 s of each other; keep a tie only if it repeats on >= 3 distinct posts from >= 2 different launch authors. Take connected components; flag a post when >= 5 of its reply authors sit in one flagged component. Schoch used >= 10 repeats on full timelines; our data is sparser, so 3 is a starting point with higher false-positive risk. Optionally estimate the window per corpus with the CooRnet rule (q = 0.1, p = 0.5). [ours for 3 and 5; 60 s and 10 from Schoch 2022]
- Evidence: Schoch 2022, Keller 2020, Pacheco 2021 (top 0.5% edges), Weber & Neumann 2021 (co-reply as a coordination trace), CooRnet, Truthy (dense injection graph), Cresci 2020 (group beats individual).
- False positives: real community regulars and devrel accounts who reply to every launch; investors replying to all portfolio launches; employees of one company replying to a sister company. The 60 s repeated-timing condition removes most of these. Also notification-driven fans of the same big account will co-reply to that account's posts; require multiple launch authors.

### R4. Promoted-only post (strong for "paid", says nothing about bots)
- Signature: the post resolves by ID but never appears in the author's profile timeline or in search.
- Statistic: absent from the author's timeline fetch covering its timestamp. Fire on absence. [ours]
- Evidence: X ads docs on promoted-only posts [verified-snippet].
- False positives: timeline fetch truncation or rate limiting; deleted-and-reposted variants; replies in a thread (not top-level). Confirm with a second fetch.
- Note: paid promotion is legitimate advertising. Report it as "paid reach", separate from "inauthentic".

### R5. Snapshot dynamics (medium-strong, needs repeated snapshots)
- Signatures and statistics:
  - Step jump: likes or reposts added in one snapshot interval exceed 5x the count predicted by a decaying fit to the previous intervals, with no matching rise in replies or quotes. [ours]
  - Flat paid pacing: views per hour hold roughly constant (fitted decay exponent below about 0.2, or ratio of hour-48 rate to hour-6 rate above about 0.5) for 24-72 hours. Organic and exogenous bursts decay with exponents near 0.6-1.4 (Crane & Sornette). [ours]
  - Count loss: likes drop more than 2% between snapshots after a jump (purged or suspended accounts, unfollow-style decay). [ours]
- Evidence: De Cristofaro 2014 and Ikram 2017 (farm likes in bursts within ~2 hours), Stringhini 2013 (bought followers decay), Crane & Sornette 2008 (decay classes), X purge statements.
- False positives: a big-account quote or repost, an HN or newsletter feature, a press hit, or an X "trending" placement causes a genuine second burst; check whether quotes jump at the same time and whether replies arrive alongside. X's spam sweeps also remove a few likes from honest posts. Snapshot cadence matters: hourly snapshots cannot see a 2-hour farm burst shape.

### R6. Reply arrival synchrony (medium)
- Signature: replies arrive in tight clumps or at machine-regular gaps, after excluding the author's own replies.
- Statistics:
  - Scan statistic: max replies in any 60 s window vs the expectation from a smoothed decaying arrival rate (fit a power-law or exponential decay to reply times); fire at p < 0.001 under Poisson. [ours]
  - Fraction of inter-arrival gaps <= 2 s among non-OP replies, vs corpus baseline. [ours]
  - Ghosh gap entropy: bin gaps in log-seconds; low entropy relative to posts with similar reply counts. [ours for binning]
- Evidence: Ghosh 2011 (zero-second gaps, peaked gap distributions in automated activity), DeBot 2016 (synchrony), Pod People (hour-by-hour comment shares, 1-hour round rules), Keller/Schoch one-minute windows.
- False positives: the post gets dropped into a company Slack, Discord or group chat ("go support the launch"), a big account quotes it, or HN links it; all produce real clumps. Employee pushes are coordinated but authentic (Pacheco; Cresci 2020's "highly coordinated humans"). Distinguish by who is in the clump: employees and followers with history vs young accounts with generic text (combine with R2, R7, R9). Ranked or truncated reply lists distort gaps.

### R7. Generic or off-topic reply share (medium)
- Signature: replies that would fit any post ("Great work!", "This is a game changer 🚀", "Impressive, looking forward to more") or that do not mention anything specific to the product.
- Statistics: share of replies classified generic by a lexicon of Pod People's "generic support" words plus an LLM or embedding check of reply-to-post relevance (cosine below a corpus percentile); topic entropy of replies (low = homogeneous). Fire when generic share exceeds the author's baseline by > 2x and > 40% of >= 20 replies. [ours]
- Evidence: Pod People 2020 (generic support topics top features; AUC 0.91-0.94; FPR 0.14), Ikram 2017 (fewer words, poorer vocabulary), Münker 2026 and Terčon 2025 (LLM text less lexically diverse, more neutral), Bier 2026 (AI thought-leadership reply accounts).
- False positives: launch posts draw honest "congrats!!" from friends, ex-colleagues and investors. That is the largest false-positive source for this rule. Down-weight congratulation replies from accounts with prior interaction with the author or follower overlap. Non-English replies fail the relevance check.

### R8. Reply author concentration (medium)
- Signature: a few non-OP accounts post many replies on the same post.
- Statistic: normalized Shannon entropy of reply counts per author (excluding the post author and accounts the post @mentions); or share of replies from the top 3 non-OP authors > 30% with >= 20 replies. [ours]
- Evidence: Ghosh 2011 (campaigns show repeated retweets by few users; "the higher the retweeting, the greater the manipulation effort").
- False positives: the founder answering every question (excluded), a genuine back-and-forth debate between two users, a support conversation.

### R9. Reply-author profile anomalies, as a distribution (weak-medium)
- Signature: unusually many reply authors are new, empty, or shaped like farm accounts.
- Statistics on reply authors (excluding OP): share joined < 30 days before the post; share with following > 5x followers and followers < 50; share with empty bio or default-looking handle (name plus 6-8 digits); likes count per day of account age in the top corpus percentile (farm accounts like everything); bio keywords ("DM for promo", "growth", "collab", "AI enthusiast" with no other content). Compare against the same author's previous posts with a two-sample test (Fisher exact on "fresh account" share, KS on log account age). Fire when the fresh-account share is > 3x baseline with >= 5 such accounts. [ours]
- Evidence: Varol 2017 (metadata features), Stringhini 2013 (follower-to-following ratio), De Cristofaro 2014 (600-1,000 page likes vs ~40), Yang 2024 (GAN-face profiles), practitioner reports on young accounts. Limits: Cresci 2017 (modern bots pass profile checks), Rauchfleisch & Kaiser 2020 (precision 0.59 or worse at standard thresholds; scores drift), Pacheco 2021 (coordinated accounts mostly human-scored).
- False positives: a new product attracts newly created accounts from people who just signed up to follow it; dev communities have many lurker accounts with empty bios; non-English users. Never label one account a bot.

### R10. Count-mix distortion vs the author's own baseline (medium-weak; the only handle on paid likes and views)
- Signatures:
  - likes/reposts far above baseline (like-only package), or reposts/likes far above (repost package);
  - replies/likes far above (reply farm or pod; Pod People's "skewed comments-to-like ratio") or far below (likes bought without replies);
  - likes/views far below the author's fitted log-likes vs log-views relation at that view count (views include promoted and repeat impressions; bought view packages exist [recalled]);
  - bookmarks/likes far below baseline (bought likes rarely include bookmarks) [ours, no evidence].
- Statistic: for each ratio, robust z-score of log ratio against the author's last 50 posts, regressed on log views because engagement rate falls as reach grows. Fire at |z| > 3 on two or more ratios. [ours]
- Evidence: Pod People (comment-to-like skew), X API docs (public counts include promoted), help.x.com (repeat views count). Bookmark rule has no literature.
- False positives: launches change the audience. HN front page or a big-account quote brings non-followers who view but like less (low likes/views). Useful dev content gets bookmarked heavily. Controversial posts get "ratioed" (high replies/likes). Video posts accrue views differently. Strong reason to treat as a supporting signal only.

### R11. Engagement vs author history (weak alone; use as a gate)
- Statistic: robust z of log1p(likes) vs the author's last 50 posts (median and MAD). z > 3 marks "outlier performance" worth running R1-R10 on. [ours]
- Evidence: Pod People recommend engagement level as a screen before a finer classifier.
- False positives: a launch is meant to be an outlier. HN front page, press, a big-account quote all do this honestly. Like baselines spanning June 2024 shift upward.

### R12. Verified low-follower reply share (weak)
- Statistic: share of reply authors that are verified with < 200 followers and account age < 1 year, vs baseline. [ours]
- Evidence: Premium reply prioritization (X); fox8 and GAN-face accounts with blue checks; practitioner reports; Bier's purges. No peer-reviewed rate.
- False positives: many real developers pay for Premium; X ranks their replies on top, so verified share in a fetched reply list is inflated for everyone.

### R13. First-hour concentration (weak)
- Statistic: share of replies (from IDs) in the first 10 and 60 minutes vs author baseline; with snapshots, share of 48-hour likes reached in hour one. [ours]
- Evidence: Pod People (1-hour rounds, hourly comment shares), De Cristofaro/Ikram (2-hour farm delivery).
- False positives: scheduled launches with an audience waiting, Product Hunt-style coordinated launches by the team, time-zone effects. Overlaps R6 and R5; do not count it twice.

## 4. Ranking, and what to report

Strong: R1 (rare), R2, R3, R4 (paid reach), R5 (if snapshots are dense). Medium: R6, R7, R8. Weak or supporting: R9, R10, R11, R12, R13.

Combine as Pacheco and Cresci 2020 suggest: require agreement between a timing rule (R5/R6/R3), a content rule (R1/R2/R7) and an actor rule (R3/R8/R9) before calling a launch "boosted". One strong rule alone (R1, R2, R3, R4) is enough to flag for human review. Report which rules fired and the evidence (example duplicate replies, the burst window, the account cluster) rather than a single score, since every rule has an honest launch-day lookalike.

Separate three labels, because they mean different things for learning from a post: "paid reach" (R4, flat pacing in R5), "coordinated but plausibly authentic" (employee or community push: R6 without R2/R7/R9), and "likely inauthentic" (R1, R2, R3, or timing + content + actor agreement).

Known blind spots: bought likes, reposts and views from stealthy farms that trickle delivery (Ikram 2017) leave only R10 and R5 traces; reciprocity markets show no synchrony (Dutta & Chakraborty); paid influencer quotes look exactly like organic big-account quotes; X's reply ranking and spam filtering hide some of the replies the rules need.

## Sources

- Pacheco et al. 2021: https://ojs.aaai.org/index.php/ICWSM/article/view/18075 ; arXiv 2001.05658
- Keller et al. 2020: https://pure.manchester.ac.uk/ws/files/128397468/Astroturfing_Kelleretal_2019.pdf
- Schoch et al. 2022: https://www.nature.com/articles/s41598-022-08404-9 (full text via Europe PMC PMC8930979)
- CooRnet estimate_coord_interval: https://rdrr.io/github/fabiogiglietto/CooRnet/man/estimate_coord_interval.html ; https://coornet.org/
- Weber & Neumann 2021: https://link.springer.com/article/10.1007/s13278-021-00815-2
- Beutel et al. 2013 CopyCatch: https://alexbeutel.com/papers/www2013_copycatch.pdf
- Ratkiewicz et al. 2011 Truthy: https://arxiv.org/abs/1011.3768
- Chavoshi et al. 2016 DeBot: https://www.cs.unm.edu/~chavoshi/debot/
- Ghosh et al. 2011: https://arxiv.org/abs/1106.0346
- Crane & Sornette 2008: https://www.pnas.org/doi/10.1073/pnas.0803685105
- Varol et al. 2017: https://arxiv.org/abs/1703.03107
- Cresci et al. 2017: https://www.semanticscholar.org/paper/bd76d414c9770a7039e9936e30a7030c127efa40
- Cresci 2020: https://arxiv.org/abs/2007.03604
- Rauchfleisch & Kaiser 2020: https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0241045
- De Cristofaro et al. 2014: https://arxiv.org/abs/1409.2097
- Ikram et al. 2017: https://arxiv.org/abs/1707.00190
- Stringhini et al. 2013: http://conferences.sigcomm.org/imc/2013/papers/imc018-stringhiniA.pdf
- Weerasinghe et al. 2020 Pod People: https://www.damonmccoy.com/papers/pod_www.pdf
- Dutta & Chakraborty survey: https://arxiv.org/html/2008.13102v1
- Yang & Menczer 2024 fox8: https://arxiv.org/abs/2307.16336
- Yang, Singh & Menczer 2024 GAN faces: https://tsjournal.org/index.php/jots/article/view/197
- Münker et al. 2026: https://arxiv.org/abs/2602.19177
- Terčon 2025 survey: https://arxiv.org/abs/2510.05136
- TechCrunch 2024 verified bots: https://techcrunch.com/2024/01/10/it-sure-looks-like-x-twitter-has-a-verified-bot-problem/
- Bier purge posts: https://x.com/nikitabier/status/1977446408136650785 ; https://x.com/nikitabier/status/2080747924380856519 ; https://x.com/nikitabier/status/2081111047340167485
- X API metrics: https://docs.x.com/x-api/fundamentals/metrics
- X view counts: https://help.x.com/en/using-x/view-counts
- X engagements campaign: https://business.x.com/en/help/campaign-setup/create-an-engagements-campaign
- X Premium reply prioritization (secondary): https://techmoran.com/x-basic-premium-and-premium-explained/
- Likes private: https://time.com/6988049/x-makes-likes-private/
- Meta adversarial threat report Q1 2025: https://transparency.meta.com/sr/Q1-2025-Adversarial-threat-report/
