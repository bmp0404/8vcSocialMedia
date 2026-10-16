# Evaluating the harness

## Current division of work

Dhilan owns the reference-based BAML judge in `harness/post_judge/` and `eval/baml_src/`.
Sam's eval worker owns backtesting, calibration, and holdouts. It will compare this judge and
`harness/qualify.py` through one predictor interface on the same held-out data. Sam's action
worker plans to reuse the judge for best-of-N selection.

The judge returns an ordinal writing-quality rubric and a derived 0 to 100 score. That score is
not an engagement probability. `InsufficientEvidence` and provider failures should be reported as
abstentions or failures, with coverage measured separately. A pipeline that silently drops hard
examples can look better while serving fewer candidates.

## What ships in the first judge

Five criteria each receive an integer score from 0 to 5: relevance, clarity, specificity,
engagement potential, and grounding. The model cites successful and poor-performing historical
posts. Python checks those citations and score ranges before computing the aggregate.

The output preserves references and dataset provenance. Selection is deterministic, balanced
across labels, limited by account, and excludes the candidate text. Backtest callers can provide
an explicit train-only reference pool. The CLI supports date cutoffs and excluded post IDs.

The prompt treats candidate text and historical examples as data. It asks for uncertainty when
reference domains differ, when reply context is absent, or when media and exposure could explain
performance. These are instructions to the judge, not proven defenses against prompt injection.

## Next evaluation systems

These are designs, not implemented systems. Sam owns the first row; agree on ownership before
building the remaining rows.

| System | Inputs and procedure | Report | First acceptance check |
| --- | --- | --- | --- |
| Engagement backtest | Split historical posts by time and event before retrieval. Grade held-out text without exposing its metrics. Compare judge and existing panel on identical rows. | Rank correlation, NDCG at a chosen review budget, label discrimination, coverage, cost per grade, and bootstrap intervals grouped by account. | Beat a simple account/format baseline on a frozen holdout; show results with suspected boosts included and excluded. |
| Factual grounding | Extract atomic claims from a draft and match each to supplied source excerpts. Label supported, contradicted, or unverifiable. | Unsupported-claim rate and claim-level precision/recall against human labels. | Detect deliberately changed names, dates, and numbers without rejecting supported paraphrases. Human review settles disputed labels. |
| Pairwise preference | Compare two drafts for the same mission and observation. Hide generator identity, randomize order, and repeat with positions swapped. | Human agreement, tie rate, order-flip rate, win rate per generator, and cost. | Order reversal should preserve the winner except declared ties; investigate every systematic position preference. |
| Observation usefulness | Evaluate source freshness, relevance to mission, provenance, and coverage before generation. Preserve the source snapshot at decision time. | Stale-source rate, unsupported-narrative rate, duplicate-source rate, and human usefulness labels. | Detect stale or irrelevant fixtures and retain useful sources, including less popular sources. |
| Failure attribution | Cross the original and repaired observations with the original and repaired drafts. Keep mission and reviewer fixed. | Whether the evidence supports an observe, action, eval, mixed, or unknown failure. | A stale-source case should improve when observation is repaired; an unsupported claim added by the writer should improve when the action is repaired. Avoid causal claims from one uncontrolled comparison. |
| Memory usefulness | Run the same observation with no memory, current memory, and a shuffled-memory control. Hold retrieval budget and generator fixed. | Repeat-error rate, factuality, preference wins, stale-memory citations, and latency. | Relevant memory should reduce previously labeled errors without increasing unsupported claims. |
| Judge robustness | Apply meaning-preserving paraphrases, inject instructions into posts, swap reference order, repeat calls, and vary reference accounts. | Grade variance, verdict flips, injected-instruction compliance, and abstention rate. | Treat strong score changes from formatting or instructions inside a post as judge failures, then add regression fixtures. |

## Data and reporting contract

Freeze each benchmark with its dataset hash, split IDs, cutoff date, and deduplication rule.
Split related threads and launch events together where those identifiers exist. A post cannot be
both a reference and a holdout, even if a duplicated CSV row uses a different URL. Keep reference
selection and any score calibration inside the training split.

Store one result per candidate with candidate ID, lane, generator version, judge model, rubric
version, reference IDs, split ID, score, verdict, confidence, latency, and failure status. The judge
already emits its local inputs and provenance. The benchmark wrapper must add run metadata and
measured token cost when available. Do not store provider credentials in results.

Report original-post and reply lanes separately. The current CSV does not identify reply-parent
context, so it cannot establish reply quality. Human-labeled reply fixtures are a separate dataset.
A dev-tool benchmark also cannot establish performance for a sports fan account.

## Build order

1. Land the judge API and let Sam wire it into a frozen holdout and the action worker's selector.
2. Build small human-labeled factuality and pairwise sets, including unsupported facts and position swaps.
3. Add observation and attribution fixtures to identify which stage needs correction.
4. Evaluate memory changes with controlled ablations after the memory team exposes retrieval.
5. Run judge robustness checks on each prompt or reference-selection change.

Choose release thresholds from a labeled pilot and the cost of each error. Record them before
running the final holdout. Keep the final holdout out of prompt tuning and reference retrieval.
