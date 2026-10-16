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
