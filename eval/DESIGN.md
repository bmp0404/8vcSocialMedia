# Eval design

This is for teammates deciding what to build next in eval, and for anyone about to trust a number the harness prints. It covers what the backtest measures, what it found, how fault attribution decides which stage to fix, and the eval after the observation agent.

## Read this before any number

The dev-tool dataset (`data/devtools_virality_30d.csv`) holds only the two tails of engagement. No post has 11 to 99 engagements. The labels are fixed raw cut-offs: Deadweight is 0 to 10, Medium Viral 100 to 749, Super Viral 753 and up. Three things follow.

- Every pair the backtest compares is a gap of 10x or more. In production, qualify picks among one brand's drafts that differ far less. **Every number here is an upper bound** on how well a predictor would rank real drafts.
- The labels mostly encode account size. Account identity alone separates viral from Deadweight at AUC 0.906 (memory research, `8vc-context/research/memory.md`, finding 2), so any comparison across accounts measures brand recognition first.
- The dataset can't measure calibration for deployment. A predicted probability fitted here would describe a world with no middle.
- Within one account, a viral post is almost always Medium Viral. After removing thread continuations, only 1 of 38 eligible accounts posts in both the Super Viral and Deadweight tails (context worker's leakage check). So the within-account hit-vs-flop pairs are mostly Medium Viral against Deadweight.

Other facts about the file, all checked in code:

- `Engagement` is likes + replies + retweets + quotes. The context worker matched it against fxtwitter.
- The status id in `Post URL` encodes the exact post time (`harness/evals/dataset.py`, `created_at`). It agrees with the `Date` column on all 1,665 rows.
- 316 scorable posts went out less than 3 minutes after the same account's previous post. Those are thread continuations, and they are almost all Deadweight. On 796 posts that fxtwitter returned, 82 of 83 posts under 3 minutes were self-replies.
- 113 more posts were under 48 hours old when the file was scraped, and their engagement was still climbing.

## What existed, and who else works on eval

Before this branch, `harness/qualify.py` (at `ccdcf2c`) had a four-person `PANEL` of Clark fans hardcoded at lines 8 to 13 and a critic prompt for "a fan account" at line 26. Line 65 averaged the panelists into `predicted_score`, and nothing compared that number with real engagement. `harness/learn.py` asks an LLM to spot where "the audience-panel predictions were miscalibrated" (line 10) from whatever outcomes `record` stored, with no metric behind it.

Other people's pieces this branch connects to:

- **Dhilan's post judge** (PR #5, `harness/post_judge/`, design in `eval/JUDGE_DESIGN.md` on his branch). It grades a post against labelled reference posts in BAML. He owns the judge. This branch owns backtesting, calibration and holdouts, and wraps his API as two predictors.
- **Aarav's self-improving branch** (`origin/aarav/self-improving`) has its own backtest, panel file and panel-weight calibration. The comparison is below.
- **The context worker** (`sam/context-dynamics`) is building follower counts, boost flags and Snowflake timing. The backtest reads its enriched CSV when present.
- **The Memory worker** stores outcomes and verdicts. Fault verdicts are shaped for it (`FaultVerdict.to_dict()`).
- **The research audit** (`8vc-context/research/eval.md`) measured the flaws in my first protocol. Protocol v2 follows its section 13.

## Results

All predictors scored the same sample under protocol v2 (described below), and `eval/reports/backtest-v2-combined-anthropic.md` holds every number in this section. The sample has 144 posts from the 27 accounts with both tiers. The judge failed on some posts, and a post any predictor failed on drops out of every predictor's numbers, so the table covers 120 posts, 26 accounts and 140 within-account viral-vs-Deadweight pairs. Each post was scored in its own model call. Brackets are 95% intervals from resampling accounts.

There are two headlines, and they answer different questions.

1. **Viral vs Deadweight, same account.** Can the predictor tell an account's hit from its flop? Here the gap is 10x or more.
2. **Viral vs viral, same account.** Can it pick the bigger of two of the account's hits? That is the content question qualify faces in production. Within one account the author floor is 0.5 by construction. Across accounts, the context worker's past-only author floor separates Super Viral from Medium Viral at AUC 0.763 (`data/context/analysis.json` on `sam/context-dynamics`, `author_floor_timeline_only`: 58 Super against 162 Medium, medians ending 72 hours before each post).

| Predictor | Viral vs Deadweight | Viral vs viral |
| --- | --- | --- |
| random | 0.493 [0.34, 0.63] | 0.460 |
| media type | 0.768 [0.67, 0.86] | 0.556 |
| text length | 0.689 [0.53, 0.84] | 0.460 |
| TF-IDF logistic, cross-fitted by account | 0.657 [0.53, 0.79] | 0.444 |
| single reader, Claude | 0.721 [0.64, 0.83] | 0.540 |
| single reader, GPT | 0.846 [0.77, 0.93] | 0.516 |
| panel, Claude (`claude-sonnet-5-5`) | 0.811 [0.74, 0.91] | 0.492 [0.32, 0.68] |
| panel, GPT (`gpt-6.1-sol`) | 0.900 [0.84, 0.95] | 0.540 [0.41, 0.68] |
| post judge, one post per call | 0.779 [0.66, 0.90] | 0.468 |
| post judge, `rank_candidates` | 0.831 [0.73, 0.95] | 0.452 |
| post judge, reference labels shuffled | 0.711 [0.60, 0.84] | 0.540 |
| the judge's references alone (word overlap) | 0.671 [0.54, 0.81] | 0.429 |

Paired differences on the same pairs, for the first headline:

| | minus media | minus TF-IDF | minus same-model reader | other |
| --- | --- | --- | --- | --- |
| panel, Claude | +0.043 [-0.08, 0.19] | +0.154 [0.01, 0.30] | +0.089 [0.02, 0.15] | |
| panel, GPT | +0.132 [0.03, 0.24] | +0.243 [0.09, 0.40] | +0.054 [-0.03, 0.13] | |
| post judge | +0.011 [-0.12, 0.15] | +0.121 [-0.11, 0.35] | | minus its references alone +0.107 [-0.12, 0.33]; minus shuffled labels +0.068 [-0.01, 0.15] |

What I take from this:

- **On the content question, nothing shows signal.** Every predictor sits between 0.43 and 0.56 on viral-vs-viral pairs within an account. The panel can spot an account's flop, but it can't pick the bigger of two hits. Across accounts, no predictor beats the author floor on Super against Medium either: the Claude panel gets 0.737 and the GPT panel 0.637 on this sample's 9 Super Viral posts, against the floor's 0.763.
- **The panel tells a hit from a flop, at about 0.8 to 0.9.** The GPT panel cleared media type, length, TF-IDF and the Claude reader at 95%. The Claude panel cleared TF-IDF and its own reader but not media type. The report holds about 50 paired comparisons, so an interval that just excludes zero is weak evidence.
- **The within-account pairs aren't an author-floor artifact.** The context worker's past-only author median scores 0.47 within accounts on the 51 sampled pairs that have enough history. Posting volume does carry some signal: posts that came after more of the account's own posts in the previous 2 hours were more often Deadweight (within-account AUC 0.39, so 0.61 inverted).
- **Media type alone gets 0.768.** The harness drafts text, so only the panel's edge over media matters for qualify, and for the Claude panel that edge is inside the noise.
- **The judge doesn't beat media type or its own references yet.** Shuffling its reference labels costs 0.068 [-0.01, 0.15], so the labelled references help a little, at the edge of the interval. Ranking a batch with `rank_candidates` (0.831) did better than grading posts one at a time (0.779), with each batch scored in two orders.
- **Personas helped Claude** (+0.089 over a single reader, interval excludes zero) and made no difference to GPT. The literature says personas hurt (arXiv 2609.25010). I would keep them until a larger sample says otherwise, and I wouldn't pick personas by their individual scores on 26 accounts.
- **Rerunning the same posts** gave Spearman 0.93 (Claude panel), 0.99 (GPT panel) and 0.81 to 0.84 (single readers).
- **Boost suspects barely matter.** Without the 6 posts the context worker flags (its enriched CSV at `7f1c759`), 114 posts and 130 pairs remain: panel GPT 0.892, panel Claude 0.796, media type 0.765.

The 24 dropped posts skew the sample. The judge can't grade posts from the first days of the dataset, when no earlier Super Viral references exist, and `rank_candidates` then fails the whole batch. That removed CockroachDB, QodoAI, CodeRabbit, E2B and PostHog entirely. The panel-only run on all 144 posts (`eval/reports/backtest-20261015T205419-anthropic.md`) gives panel GPT 0.854, panel Claude 0.797 and media type 0.737, the same picture.

Expect much less in production. On same-author tweet pairs with the same link, wording models reach about 65% and people 61% (Tan, Lee and Pang 2014, arXiv 1405.1438), and content explains R² 0.055 of engagement against 0.317 for author features (arXiv 1812.06034). The viral-vs-viral column above is this dataset's version of that ceiling.

### The post judge

The backtest scores Dhilan's judge (`harness/post_judge`, PR #5) on the same protocol as the panel, through two predictors in `harness/evals/predictors.py`. `post_judge` grades each post alone against references that `select_references` picks with `before` set to the candidate's day. `post_judge_rank` calls `rank_candidates` on up to 4 posts from one account, in a shuffled order and its reverse, and compares only posts ranked in the same call. The batch-order finding below applies to `rank_candidates`, which scores candidates listwise. `post_judge_refs_only` scores each post by word overlap with the judge's viral references minus its Deadweight ones; the judge earns its cost only by beating that lookup. `--memory shuffled` keeps the same references but permutes their labels, which is the memory ablation.

The judge failed on 11 of 144 posts (6 had no earlier Super Viral references, 5 hit the BAML client's `max_tokens = 2500`), `rank_candidates` on 17, and the shuffled run on 8. A first judge run aborted on the `max_tokens` failure; failed posts now drop out of every predictor's numbers instead.

How the judge ran: a scratch worktree merged `dhilan/version-1` with this branch at `883f84b`. The `baml` CLI came from `brew install baml` (system-wide; the lead has since ruled out system-wide installs without asking), then `baml toolchain install 0.22.2` and `BAML_VERSION=0.22.2 baml generate --directory eval --agent-skill-check off`, which skipped installing BAML's agent skill. The judge ran as `HARNESS_JUDGE_PROVIDER=anthropic HARNESS_JUDGE_MODEL=claude-sonnet-5-5` through the gateway, not the Claude Code CLI client, which would have started a Claude Code session per post. `harness eval rescore --merge` combined the judge runs with the panel run, because all three share the same sample ids.

### Leakage

The model could have memorized these posts and their reception. The probe (`harness eval leakage`) gave `claude-sonnet-5-5` the first half of 40 Super Viral posts and asked for the rest word for word. **0 continued verbatim for 8 or more words, and 0 were claimed as recognized** (`eval/reports/v1/leakage-20261015T203320-anthropic.json`). The posts date from 2026-09-15 to 2026-10-16, after Opus 5.5's June 2026 training cutoff. I have not confirmed the Sonnet 5.5 or GPT-6.1 Sol cutoffs. The judge's leakage risk is different: its reference posts carry labels, so `PostJudgePredictor` refuses any reference dated on or after the candidate's day (`harness/evals/predictors.py`).

### Aarav's backtest, reproduced

`origin/aarav/self-improving` has its own backtest (`harness/backtest.py` there). It samples 10 posts per category from the smaller segments, correlates panel scores with log engagement across accounts, and reported Spearman 0.704 on 30 posts. It also fits panel weights on the same 30 posts it reports.

Every report here recomputes his protocol on the same predictions, 500 draws of the sample (`category_balanced` in `harness/evals/backtest.py`). In the panel-only run on all 144 posts, the Claude panel gets a median of 0.776, which reproduces his number, with 95% of single draws between 0.43 and 0.90. The same predictions score 0.797 on the within-account headline. The protocols disagree on the baselines: text length gets -0.14 under his and 0.68 under mine, and media type gets 0.58 against 0.74. His protocol measures how well a score tracks raw engagement across brands, which mostly measures brand size. Under the earlier v1 protocol, his Coordigent persona file scored like ours within accounts (0.596 against 0.592, `eval/reports/v1/`), so the persona wording mattered little.

## How the backtest works, and what I ruled out

`harness eval backtest` runs this protocol (`harness/evals/backtest.py`). The research audit (`8vc-context/research/eval.md`, section 13) proposed most of it after measuring problems in my first version.

1. **Eligible posts.** Scorable text (40+ characters after removing t.co links, not an @-reply), not a thread continuation, at least 48 hours old. That leaves 848 posts.
2. **Sample.** Every account with at least one viral and one Deadweight eligible post, up to 4 of each per account, in a seeded shuffle.
3. **No date holdout.** The panel, the reader and the judge fit nothing, so they score every sampled post. The fitted baselines cross-fit by account: TF-IDF trains on other accounts' posts only. The judge only sees references dated before the candidate's day, a rolling origin.
4. **One post per model call.** In my first version the sampler put each account's viral posts first in every batch. Batch order alone moved the panel's AUC by 0.125 (audit, finding 1), and a fake predictor that knew only batch position beat media type. When a predictor must batch, it shuffles with a logged seed and averages the forward and reverse orders. Production qualify now does the same (`panel_scores_two_orders` in `harness/qualify.py`).
5. **Headline metric.** The within-account AUC of viral against Deadweight. Viral-vs-viral, same-media-status and same-launch-type pairs are secondary, and Deadweight-vs-Deadweight pairs are a noise check. In v1, 51% of pairs were Deadweight against Deadweight, two posts with 2 and 5 likes, and they dragged every predictor toward 0.5.
6. **Comparisons.** `paired_difference` resamples accounts once per draw and takes the difference on the same pairs. Two overlapping marginal intervals can hide a real difference.

I ruled out these alternatives:

- **The continuous target** `log1p(engagement) - log1p(account median)`. With the middle of the distribution missing, an account's median lands wherever its mix of tails puts it. It survives only for the viral-vs-viral metric.
- **Cross-account correlation as the headline.** It rewards a model for knowing Cursor is big. Those numbers sit in a separate table labelled "confounded by account size", and the judge's mission string no longer names the account.
- **Calibration columns.** v1 compared the panel's mean P(engage) with "beats the account median", two different events. On this tail-sampled data, no calibration means anything for deployment anyway.
- **A single date holdout** for unfitted predictors. It threw away two thirds of the posts and gave the youngest, least mature slice as the test set.

Boost flags and eligibility come from the context worker's `data/context/devtools_enriched.csv` when it exists (`apply_enriched` in `harness/evals/dataset.py`), or from `HARNESS_ENRICHED`. The file isn't on `main` yet, so a run without it prints a warning and keeps boosted launches in. With it, the report adds a second evaluation without boost suspects, and its `label_eligible` replaces the local thread and age rules: it uses fxtwitter's `replying_to`, which catches self-replies the 3-minute rule misses. On the 1,277 posts with scorable text, the two rules disagree on 109: the context label drops 106 that the local rules keep and keeps 3 they drop, which leaves 745 eligible posts instead of 848. The headline sample above used the local rules. Read only its `boost_*` and `label_eligible` columns here, use only `pre_*` columns as features, and never its `outcome_*` columns.

## The audience panel is brand-agnostic now

`PANEL` used to hardcode Iowa and Clark fans in `harness/qualify.py`. Panelists now live in a JSON file next to the mission: the profile's `panel.json` (`mission/profiles/<name>/`), or the path in `HARNESS_PANEL`. The backtest points the same code at `eval/panels/devtools.json`. The critic prompt now reads "the account described in MISSION" instead of "a fan account".

The file shape is `{panelist name: description}`, the same as Aarav's `eval/panel.json`, so his file loads with `--panel`. The paths differ. I put the brand's panel under `mission/` because CLAUDE.md asks for brand text there. Whoever merges both branches has to pick one path.

`HARNESS_PANEL_MODEL` lets qualify score with another model on the same gateway, such as `gpt-6.1-sol`. Unset, qualify uses the run's LLM as before. A judge from another family also avoids rating its own drafts, which the backtest can't show because its posts are human-written.

## Production qualify ranks posts with the learned ranker

Qualify keeps the critic veto and no longer calls the panel by default. The ranker is the TF-IDF ridge from `harness/rank/post.py`, fitted on the post benchmark's 39 dev authors (861 posts) and saved in `eval/rankers/` with the SHA-256 of its JSON (`harness/rank/post_ranker.py`). `post_ranker.load()` refuses a model whose bytes don't match its manifest, and every scored draft carries the hash in `draft_meta["ranker"]`.

Scores on the post benchmark's 37 test authors, 1,016 pairs, from `eval/rankers/post_ridge.manifest.json`:

| Scorer | Accuracy | The saved ranker minus it, same pairs |
| --- | --- | --- |
| The saved ranker | 0.709 [0.644, 0.767] | |
| Panel, `claude-sonnet-5-5`, one post per call | 0.723 [0.670, 0.781] | -0.014 [-0.081, 0.041] |
| The same fit with every n-gram kept | | -0.005 [-0.026, 0.017] |
| The ridge cross-fitted over all accounts (phase D) | 0.711 [0.650, 0.772], 932 pairs | +0.010 [-0.015, 0.033], 932 pairs |
| Length | 0.602 | +0.107 [0.035, 0.185] |

The panel and phase D numbers are from `data/bench/results/judges-posts.json`. The saved ranker keeps only n-grams that 5 or more accounts used, because the repository is public and an n-gram one account used can be a person's handle. That filter is the third row.

What qualify does with it:

- It scores the post lane's posts and quotes, which are people's takes and captions, and reorders them among the places they already hold in decide's list.
- Replies and follows keep decide's order and get no score. On the reply benchmark, text added +0.002 [-0.034, 0.044] to timing and reach (`data/bench/results/replies.json`, `ranker_pooled_test`), so a text score has nothing to add there.
- `predicted_score` becomes the ranker's percentile among its own training posts, 0 to 1. `draft_meta["ranker"]` holds that, the raw score (the predicted within-account z-score of log engagement) and the hash. Unscored actions keep `predicted_score` at 0.0, so the old review dashboard, which sorts by it, lists them last.
- A round costs the critic's one call. The panel cost two more, one per order.

`HARNESS_QUALIFY_RANKER=panel` (the `qualify.ranker` knob in `harness/iterate/knobs.py`) restores the old path exactly: the panel scores every draft in two orders and qualify sorts all of them by its mean.

Not measured:

- The ranker learned from dev-tool brand accounts. On the wnba profile it orders a fan account's captions by what it learned there, and nothing shows that order means anything. The panel had no wnba measurement either.
- The benchmark pairs differ by 3x or more in engagement, and a round's post lane ships at most one caption or a few takes, so the ranker usually orders one to three drafts that differ less.
- No outcome yet shows whether its order predicts what the account's own posts get.

Refit with `uv run python -m harness.rank fit-post`, which needs the local corpus snapshot. A refit on the same corpus rewrites both files byte for byte.

## Fault attribution

When an action underperforms, eval decides whether the agent or its input was at fault (`harness/evals/fault.py`). Three pieces of evidence feed a decision table:

- **Outcome.** Our engagement against the account's own band. Below its 10th percentile is an underperformance. Inside the 10th to 90th band is `variance`, and nothing gets fixed. A first version flagged anything under half the median, which caught 18% of ordinary posts.
- **Story.** How other accounts' posts on the same narrative did in the 24 hours after ours, each against its own author's median. For a reply, the peers are the other replies to the same target post.
- **Draft.** The post judge's score_100 / 100, graded after the fact, or any critic flag.

| Verdict | Evidence it needs | Stage to fix |
| --- | --- | --- |
| `no_fault` | engagement above the account's 90th percentile | none |
| `variance` | engagement inside the account's 10th to 90th band, from 10+ posts | none |
| `action_fault` | underperformed, story alive (peers at 1.0x their usual or better, 3+ peers), draft bad (judge under 0.4, or a critic flag) | action |
| `observe_fault` | underperformed, story dead (peers under 0.5x), draft good (judge 0.6+) | observe |
| `both` | underperformed, story dead, draft bad | observe first; a better draft on a dead story still flops |
| `unexplained` | underperformed, story alive, draft good | eval; our quality measures missed something |
| `uncertain` | story or draft in the middle band | none |
| `insufficient_evidence` | anything above missing; the verdict lists what | none |

Every threshold is in `FaultConfig` (`harness/evals/settings.py`), and none has been tested. By construction, a tenth of ordinary posts land under the 10th percentile. So `aggregate` recommends changing a stage only after 3 matching verdicts, and qualify should be blamed per batch of 20 or more actions through the backtest metric, never per action. The two-follower fan account has no band yet, so every verdict will say `insufficient_evidence` until it has 10 posts.

**A real check, and what it found.** `harness eval fault --stories` builds evidence from the dataset itself: 72 posts about 13 model launches that several accounts covered (`eval/fixtures/devtools_stories.json`). Forty-seven were `variance`, 6 `no_fault` and 19 `insufficient_evidence`, mostly for lack of peers after the post. The observe half of the table rests on one assumption: a live story predicts a good result for a post about it. On the 14 posts with 3 or more peers, the post's own result and the story's peer ratio correlated at Spearman -0.11. That is far too few posts to conclude anything, but nothing yet supports the assumption either. Nemotron 3.5 Super on 2026-10-15 shows what a dead story looks like. Every account but Cline got 0 to 2 engagements.

**The draft half needs the judge, which is also untested as a quality signal.** An LLM naming the faulty agent from traces gets 54% of agents right and 12.5% of steps, below a random baseline (Who&When, arXiv 2505.00212). Counterfactual reruns do better. So the next step is a swap test rather than more rules:

1. For an underperformer, rerun Action only (we never post) in four arms of 5 drafts each. A0 reuses the same input, to measure noise. A1 uses a gold input rebuilt from what was visible before a strong peer post on the same story. A2 ablates the input. A3 sends the same input through the other minds.
2. Score the arms with a frozen judge from the other model family.
3. Call one arm better only when it beats the A0 noise floor and wins 4 of 5 pairwise comparisons. A1 beating A0 blames observe. A3 beating A0 blames the mind.

The swap test needs a log per action that the Memory worker owns: the input snapshot (narrative, signal ids, texts and metrics at decision time), prompt hash, model, mind, sibling drafts with their scores, which draft the Grok bot chose, outcome metrics at 24 and 48 hours, and the peer posts on the same story.

## The eval after the observation agent

The hatched Eval box on the whiteboard's second row sits after the observation agent. Its question is whether observe surfaced the story the account should join, before the story peaked.

**Time-travel recall, built** (`harness/evals/observe_eval.py`, `harness eval observe`). At a cutoff an hour after a launch's first post, orient sees only the dataset's posts from the 24 hours before, with no engagement counts, because those are final values. A story is live if at least two other accounts post about it in the next 24 hours. Recall is the share of live stories orient names in its top 5 narratives. The floor is a naive observer that takes the 5 most recent posts.

| Orient version | Report | Orient recall@5 | Most-recent floor |
| --- | --- | --- | --- |
| `main` before PR #10 | `eval/reports/observe-20261015T210330-anthropic.json` | 0.82 (9 of 11) | 0.55 (6 of 11) |
| `main` at `a9de2d5`, after the Observe PR | `eval/reports/observe-20261015T211253-anthropic.json` | 0.73 (8 of 11) | 0.55 (6 of 11) |

Both runs used `claude-sonnet-5-5` on the same 11 live stories at 9 cutoffs. Eleven stories is a smoke test, and two cutoffs share the same Opus 5.5 launch, so the two versions are indistinguishable. The old orient's two misses were failures, not judgment: on 59 and 72 signals it returned JSON cut off mid-object, because `harness/llm.py` caps output at 4,096 tokens and that orient echoed every signal URL. The merged orient had no failures; it missed GPT-6.1 Sol, the second Opus 5.5 wave and Nemotron 3.5 Super. The merged orient also drops signals older than its age limit relative to `now`, so the replay passes the cutoff as `now`.

**Not built yet:**

- Pre-action checks on each narrative: its signal URLs exist among the observed signals, its newest signal is younger than the lane needs (a reply needs a live target post), and no two narratives are duplicates.
- A velocity check: orient's velocity score against how the narrative's signals grew in the next 24 hours, which needs re-fetched metrics.
- The swap test's A1 arm against A0 measures how much a better input would have helped, which is the cost of observe.

## Lineup test (designed, not built)

Hide one harness draft among four real top-performing posts from the same account and ask a judge from the other model family which one a machine wrote. Detection at the 1-in-5 chance rate means the drafts pass as the brand's best human posts. The generation-time judge belongs to Dhilan's `harness/post_judge` and the Action worker, so this test should reuse it rather than add a third judge.

## Tunables and the fitness function

Every knob is in `harness/evals/settings.py`: `BacktestConfig` (eligibility, sample size, bootstrap draws, models, batch size, judge references, memory mode) and `FaultConfig` (bands and thresholds). `harness.evals.backtest.fitness(predictor, cfg)` runs the protocol and returns the headline AUC with its interval, for the self-iteration loop. Compare two candidates with `paired_difference`. With 27 accounts, only differences above about 0.1 AUC show, and tuning many knobs against this sample will overfit it.

## Open questions for Sam

- Production qualify scores drafts in two orders. Scoring each draft alone costs one call per draft. Is that worth it?
- Should production run the panel on `gpt-6.1-sol` (`HARNESS_PANEL_MODEL`)? It led on this sample, inside the interval.
- The judge's reference pool is labelled by raw engagement, so its "successful" references come from big accounts. Labels relative to each account would fix that. That decision is Dhilan's.
- Once followers and boost flags land, should the headline drop boosted launches by default?

## What this branch built

- `harness/evals/`: the dataset loader and eligibility rules (`dataset.py`), metrics (`metrics.py`), predictors including the judge adapters (`predictors.py`), the backtest and its reports (`backtest.py`), the leakage probe (`leakage.py`), fault attribution (`fault.py`, `stories.py`), time-travel recall (`observe_eval.py`), the knobs (`settings.py`) and the `harness eval` commands (`cli.py`).
- `harness/qualify.py`: panel from a file, mission-aware critic, two-order panel scoring and `HARNESS_PANEL_MODEL`.
- `mission/panel.json`, `eval/panels/devtools.json`, and fixtures under `eval/fixtures/`.
- Reports under `eval/reports/`. The superseded v1 runs, scored with the order bias, are in `eval/reports/v1/`.
- Tests in `tests/test_eval_*.py`.

## Run it

```sh
uv run python -m harness eval backtest --provider mock      # no key; checks the code path
uv run python -m harness eval backtest --provider anthropic --models claude-sonnet-5-5,gpt-6.1-sol
uv run python -m harness eval rescore eval/reports/<report>.json --merge eval/reports/<other>.json
uv run python -m harness eval leakage --provider anthropic --n 40
uv run python -m harness eval observe --provider anthropic
uv run python -m harness eval fault eval/fixtures/fault_evidence.json
uv run python -m harness eval fault --stories
uv run python -m pytest tests
```

A real panel run costs about 144 short calls per model and predictor, about 600 calls for both families and both arms. The post judge needs Dhilan's PR #5 merged and its BAML client generated. `eval/README.md` on his branch describes that, and the `baml` CLI is a separate install that this repo doesn't manage.
