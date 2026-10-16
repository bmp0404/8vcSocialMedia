# Closed ideas

Ideas the evidence says not to build, or not to build yet. Each entry gives the result, where it came from, how far it was checked, and what would reopen it. Later batches append here; add the date and keep the source path exact.

## Posting hour and day of week (closed 2026-10-16)

**Result:** Within an account, the hour block and the day of the week don't predict engagement. On the refreshed `data/context/devtools_enriched.csv` (mature, eligible, not boost-flagged posts from Mid-size and Smaller/closest accounts: 224 posts, 19 accounts), a within-account permutation test of log(engagement / author median) gives hour p = 0.412 and day p = 0.815. For engagement per view it gives p = 0.322 and p = 0.147.

**Source:** Aiden's `scripts/timing_analysis.py` on `origin/aiden/harness`. Rerun for this file with `python -I scripts/timing_analysis.py --enriched data/context/devtools_enriched.csv` from a copy of that script and `harness/evaluator/metrics.py`; no LLM calls. The B0 audit reported 0.417 and 0.805 from another draw of the 2,000 permutations. Aiden's README reports hour p = 0.045 on the older CSV (256 posts, 20 accounts), which failed his own robustness checks (p = 0.26 on ranks, 0.52 on other accounts).

**Closes:** a posting-time scheduler or "best hour" knob. Spacing between our own posts is a separate finding (`action/DESIGN.md`) and stays.

**Reopen if:** the brand's own posts, or a within-account sample several times larger, show an hour effect that survives ranks and leave-one-account-out.

## Ranking engagement per view within an account (closed 2026-10-16)

**Result:** No ranker separated a post's engagement per view from the same account's other posts. On 97 posts from 11 held-out Mid-size and Smaller accounts, same-account pair accuracy was 0.50 for the panel plus pairwise blend, 0.47 for the panel, 0.51 for the rubric, 0.51 for a no-LLM text-feature model and 0.48 for length. Random scored 0.44.

**Source:** Aiden's evaluator results, `origin/aiden/harness:README.md`, "Backtest on cleaner labels", from `scripts/backtest_eval.py --enriched`. Not rerun: it needs LLM calls. The same table shows the blend does rank engagement over the author's median (pair accuracy 0.65), so the judges track reach, not per-impression resonance.

**Closes:** optimizing drafts for engagement per view with the current judges.

**Reopen if:** a judge beats 0.5 pair accuracy on per-view engagement on a frozen within-account split, with a confidence interval that excludes 0.5.

## Persona drafting and LLM best-of-N for replies (closed 2026-10-15)

**Result:** Replies written from a persona prompt read as AI-written. In a five-reply lineup (the four most-liked real replies and one candidate) on 19 real X threads, Opus 5.5 picked out the persona prompt's reply (`naive`, today's `reply_sniper` plus the mission) 19 of 19 times. Five style-prompted samples with the least AI-sounding picked by an LLM (`bestof`) were picked out 18 of 19 times. A held-out real reply was picked out 4 of 19 times, which is chance. Every instruct-prompt arm differs from the real reply at p < 0.0001 (Fisher's exact test).

**Source:** `action/DESIGN.md`, the lineup table; raw verdicts in `action/experiments/reply_experiment.json`. Generators `claude-sonnet-5-5` and `claude-opus-4-5-20251101`, judges Opus 5.5 and two others.

**Closes:** persona prompts and LLM selection as the reply writer. The reply lane continues the real thread as a document instead (`harness/minds/writer.py`). Aiden's `harness/lanes/` (persona minds plus a pairwise tournament) is the same approach and was not ported.

**Reopen if:** a persona or selection method is picked out at no more than the real reply's rate on the same lineup protocol.

## Whole-file LLM rewrites of the lessons file (closed 2026-10-15)

**Result:** An LLM that rewrites a memory document whole tends to shrink it and lose accuracy. The ACE paper (arXiv 2510.04618) reports a Dynamic Cheatsheet memory that went from 18,282 tokens at 66.7% accuracy to 122 tokens at 57.1%, below its 63.7% no-memory baseline, after repeated rewrites. An itemized list that code updates by vote, as ExpeL does, avoids it.

**Source:** `memory/DESIGN.md`, "Three things write lessons today" and the alternatives it ruled out. This is the paper's result, not one measured here.

**Closes:** Aarav's `evolve.py` and the old `learn.py` that had an LLM rewrite `memory/lessons.md`. Lessons are written one at a time by the reflection job, each with a held-out test (`harness/learning/reflect.py`), and the lessons file is rendered from them.

**Reopen if:** a rewrite loop holds accuracy over repeated rounds on our own lesson set against the itemized store.

## Ordering reply targets by an author's openness (closed 2026-10-16)

**Result:** An author's openness on earlier threads doesn't predict how much of a later thread's top 3 small accounts take. Openness is the share of an author's top-3 replies written by accounts under 1,000 followers (`harness/rank/target.py`). Over weekly rolling cutoffs, each test thread scored against its author's threads from before that week (147 threads, 21 authors, each author with 2 or more earlier threads), the Spearman correlation between prior openness and the thread's small-account share of the top 3 is 0.09 [−0.13, 0.25]. The brief's yes-or-no question, whether any small account reached the top 3, is true for 92% of threads, so only 12 test threads were closed to small accounts. Prior openness ranks those 12 with AUC 0.75 [0.49, 0.87]. The interval clears 0.5 only when one earlier thread is enough (0.76 [0.58, 0.87] on 165 threads) and not at three (0.74 [0.44, 0.89] on 122). The single split at B2's 2026-10-06 cutoff gives 0.71 [0.37, 0.98] on 69 threads with 5 closed ones. The author's own follower count does no better (share ρ −0.19 [−0.38, 0.02]; larger authors' threads lean slightly more open). Intervals resample authors, 2,000 draws.

The timing half held up better than the share. An author's earlier median minutes-after of the top 3 correlates with a later thread's at ρ 0.35 [0.15, 0.47] on the rolling cutoffs, but 0.08 [−0.14, 0.27] on the single split, so no per-author freshness cutoff was derived from it.

**Source:** `uv run python -m harness.rank.target validate`, report in `eval/experiments/reports/openness-validation.json` (the one- and three-thread minimums are under `rolling_weekly_sensitivity`). Data: 242 scored threads from 45 dev-tool brand accounts in `data/context/reply_samples.jsonl` (local only), roots 2026-09-15 to 2026-10-15. The samples over-represent viral posts: every Super Viral post, 150 Medium Viral and 100 Deadweight posts with replies (`harness/context/collect.py:_targets`). I chose two earlier threads as the minimum before running the rolling check, after a first look at single splits on 2026-09-29, 10-02 and 10-06. No LLM calls.

**Closes:** ordering `lanes.plan_targets` by openness and a per-author `reply_max_age_minutes`. `openness(handle)` and the committed per-account table (`data/context/openness.json`) stay, because the watcher reads the same interface. The dev-tool watch list on `sam/watch` admits accounts at a share of 0.5 or more; this check found no evidence that the share carries over to an account's next threads.

**Reopen if:** the watcher's rolling threads (`data/threads/`) give 30 or more threads with no small account in the top 3, or the share correlation's interval clears zero on threads collected after this date.

## Watching mid-size accounts that repost the dev-tool space (closed 2026-10-16)

**Result:** The accounts that amplify the space by reposting don't start threads worth replying to. Ranked by how many different brands' posts they reposted (`data/context/reposters.jsonl`, 1,000 to 100,000 followers, self-reposts and watched brands left out), the top 75 were read through fxtwitter: one timeline page each and the replies under up to four day-old original posts. 13 profiles returned 404. Of the 62 read, 25 had no original post in their last 20 timeline items, and only 21 pages reached back into the window, by a median of half a day. In the 2026-10-02 to 10-16 window they posted 4 originals that drew 3 or more replies and 62 that drew fewer, and none of the busy ones was English with one of the profile's keywords. Only 2 had a thread with 3 or more replies old enough to score.

Replaying the window's posts through the watcher's polling (one profile read per handle in turn at 0.2 reads per second; fresh means read within 10 minutes; open means 2 or more of the top 3 replies from accounts under 1,000 followers), the current lists catch 8.2 open fresh threads a day (sam/platform's 15 handles) and 14.3 (sam/watch's 24). Adding the 62 candidates adds none that the watcher would act on. If every post counted regardless of topic or language, they would add 9.5 a day, but one account with 2 busy posts in under six hours supplies most of that, and 2 scored threads set its open rate. Per handle, the 21 candidates whose pages reach the window trail the lists by 0.55 [0.33, 0.84] and 0.60 [0.44, 0.79] open fresh threads a day; counting every post, they can't be told apart from the lists (−0.10 [−0.73, 0.90] against sam/platform). Intervals resample accounts within each group, then their scored threads, 2,000 draws. Polling never limited freshness: 86 handles are read every 430 seconds, under the 10-minute bar.

**Source:** `uv run python -m harness.rank.amplifiers build --top 75` then `replay`; counts in `eval/experiments/reports/amplifier-replay.json`. The candidate list (`data/context/amplifiers.jsonl`) names private people and stays local. No LLM calls. The wnba profile has no reposter data on disk, so it wasn't tested. A second pool, mid-size accounts that replied under 3 or more brands' threads (94 of them in the reply samples), stopped partway when the disk filled; `build --pool repliers` reruns it.

**Closes:** adding repost amplifiers to either dev-tool watch list. No additions are proposed.

**Reopen if:** the replier pool, or quote lists once a source has them, turns up mid-size accounts whose English, on-topic posts draw 3 or more replies at a rate near the brands' 0.55 to 0.60 open fresh threads per handle per day.
