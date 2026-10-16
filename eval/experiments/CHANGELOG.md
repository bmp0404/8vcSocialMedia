# Experiment changelog

Generated from the `exp-*.json` records in this folder by `harness/iterate/ledger.py`. Newest first. Every number is from the run named in its record; brackets are 95% intervals from resampling the cluster unit (accounts or threads). DESIGN.md explains the gate.

Current champion: `cfg-35c9ee1e8e13`, shaped by 2 promotion(s): `exp-20261016-action-7ea3e0`, `exp-20261016T023744-revert-7ea3e0`.

## exp-20261016-action-ba22f4: inconclusive

- **Change:** `action.reply.generation` 'persona_json' → 'styled'
- **Hypothesis:** Short style rules on Sonnet 5.5 read less like AI than the persona JSON call, and pass the veto and the GPT-family detector no worse.
- **Proposed by:** queue (research/action.md (8vc-context): forced-choice detection 81% for styled against 100% for the persona prompt)
- **ai_detection_rate** (lower is better), 20 threads: champion 0.950 [0.850, 1.000], challenger 0.800 [0.600, 0.950], difference -0.150 [-0.300, -0.000]
- **Noise floor** (champion rerun with new lineups or seeds): 0.050
- **Correction:** Benjamini-Hochberg q=0.1 over 1 comparison(s); p=0.0440, adjusted 0.0440
- **Decision:** significant after BH, but the lower bound +0.000 does not clear the noise floor 0.050 (auto, 2026-10-16T15:24:29+00:00)
- **Cost:** 95 calls on the challenger arm, plus shared champion arms (185 calls)

## exp-20261016T023744-revert-7ea3e0: promoted

- **Change:** `action.reply.generation` 'pipeline' → 'persona_json', `action.samples` 4 → 1
- **Hypothesis:** Revert exp-20261016-action-7ea3e0.
- **Proposed by:** revert (exp-20261016-action-7ea3e0)
- **Decision:** Confirmation exp-20261016-action-d71d3a didn't promote it again: improvement lower bound +0.444 at 0.975 clears the noise floor 0.000; blocked: not shown non-inferior on veto_pass_rate (95% lower bound -0.556, margin -0.1) (auto, 2026-10-16T02:37:44+00:00)
- **Cost:** 0 calls on the challenger arm

## exp-20261016-action-d71d3a: inconclusive

- **Change:** `action.reply.generation` 'persona_json' → 'pipeline', `action.samples` 1 → 4
- **Hypothesis:** The promoted reply lane stays non-inferior on the veto once the veto also checks mission fit and content-free drafts (instrument v3).
- **Proposed by:** confirmation (exp-20261016-action-7ea3e0)
- **ai_detection_rate** (lower is better), 27 threads: champion 1.000 [1.000, 1.000], challenger 0.370 [0.185, 0.556], difference -0.630 [-0.815, -0.444]
- **Noise floor** (champion rerun with new lineups or seeds): 0.000
- **Correction:** Benjamini-Hochberg q=0.1 over 1 comparison(s); p=0.0005, adjusted 0.0005
- **Decision:** improvement lower bound +0.444 at 0.975 clears the noise floor 0.000; blocked: not shown non-inferior on veto_pass_rate (95% lower bound -0.556, margin -0.1) (auto, 2026-10-16T02:37:44+00:00)
- **Cost:** 25 calls on the challenger arm, plus shared champion arms (51 calls)

## exp-20261016-action-7ea3e0: promoted

- **Change:** `action.reply.generation` 'persona_json' → 'pipeline', `action.samples` 1 → 4
- **Hypothesis:** sam/action's reply lane (4 continuations, slop gate, two-order Opus veto, random pick) reads less like AI than the persona JSON call and is no less safe, because its veto removes the invented claims and insults plain continuation shipped in exp-20261016-action-f691d2.
- **Proposed by:** manual (research/action.md, findings 2-4; exp-20261016-action-f691d2)
- **ai_detection_rate** (lower is better), 27 threads: champion 1.000 [1.000, 1.000], challenger 0.370 [0.185, 0.556], difference -0.630 [-0.815, -0.444]
- **Noise floor** (champion rerun with new lineups or seeds): 0.000
- **Correction:** Benjamini-Hochberg q=0.1 over 1 comparison(s); p=0.0005, adjusted 0.0005
- **Decision:** improvement lower bound +0.444 at 0.975 clears the noise floor 0.000 (auto, 2026-10-16T02:30:34+00:00)
- **Cost:** 237 calls on the challenger arm, plus shared champion arms (207 calls)

## exp-20261016-qualify-f89252: inconclusive

- **Change:** `qualify.batch` 0 → 1
- **Hypothesis:** Scoring each post in its own call separates viral from Deadweight better than scoring an account's posts together, because batched scores assimilate toward their neighbours (research: 0.12 AUC from order alone).
- **Proposed by:** manual (research/eval.md, finding 1)
- **within_account_auc** (higher is better), 144 posts from 27 accounts: champion 0.794 [0.713, 0.891], challenger 0.797 [0.727, 0.882], difference 0.003 [-0.065, 0.059]
- **Noise floor** (champion rerun with new lineups or seeds): 0.018
- **Correction:** Benjamini-Hochberg q=0.1 over 4 comparison(s); p=0.4498, adjusted 0.4498
- **Decision:** not significant after BH; 95% interval on the improvement [-0.065, +0.059] (auto, 2026-10-16T02:26:07+00:00)
- **Cost:** 0 calls on the challenger arm, plus shared champion arms (108 calls)

## exp-20261016-qualify-f178ed: inconclusive

- **Change:** `qualify.memory` 'off' → 'shuffled'
- **Hypothesis:** Control for memory-on: the same older posts with their outcome labels shuffled. If this wins too, the gain comes from seeing examples, not from their outcomes.
- **Proposed by:** manual (research/memory.md, finding 11)
- **within_account_auc** (higher is better), 144 posts from 27 accounts: champion 0.794 [0.713, 0.891], challenger 0.823 [0.733, 0.913], difference 0.029 [-0.008, 0.064]
- **Noise floor** (champion rerun with new lineups or seeds): 0.018
- **Correction:** Benjamini-Hochberg q=0.1 over 4 comparison(s); p=0.0780, adjusted 0.1039
- **Decision:** not significant after BH; 95% interval on the improvement [-0.008, +0.064] (auto, 2026-10-16T02:26:07+00:00)
- **Cost:** 54 calls on the challenger arm, plus shared champion arms (108 calls)

## exp-20261016-qualify-752ef2: inconclusive

- **Change:** `qualify.panel` 'personas' → 'single_reader'
- **Hypothesis:** One no-persona reader separates viral from Deadweight posts within an account at least as well as the persona panel (Upworthy: tau 0.361 against 0.084).
- **Proposed by:** manual (research/eval.md, finding 8)
- **within_account_auc** (higher is better), 144 posts from 27 accounts: champion 0.794 [0.713, 0.891], challenger 0.854 [0.774, 0.938], difference 0.060 [0.007, 0.113]
- **Noise floor** (champion rerun with new lineups or seeds): 0.018
- **Correction:** Benjamini-Hochberg q=0.1 over 4 comparison(s); p=0.0130, adjusted 0.0310
- **Decision:** significant after BH, but the lower bound +0.007 does not clear the noise floor 0.018 (auto, 2026-10-16T02:26:07+00:00)
- **Cost:** 54 calls on the challenger arm, plus shared champion arms (108 calls)

## exp-20261016-qualify-2d1cf7: inconclusive

- **Change:** `qualify.memory` 'off' → 'on'
- **Hypothesis:** Showing the panel the account's own older posts with their outcomes raises within-account AUC.
- **Proposed by:** manual (research/memory.md, findings 8 and 11)
- **within_account_auc** (higher is better), 144 posts from 27 accounts: champion 0.794 [0.713, 0.891], challenger 0.823 [0.740, 0.915], difference 0.029 [0.003, 0.056]
- **Noise floor** (champion rerun with new lineups or seeds): 0.018
- **Correction:** Benjamini-Hochberg q=0.1 over 4 comparison(s); p=0.0155, adjusted 0.0310
- **Decision:** significant after BH, but the lower bound +0.003 does not clear the noise floor 0.018 (auto, 2026-10-16T02:26:07+00:00)
- **Cost:** 54 calls on the challenger arm, plus shared champion arms (108 calls)

## exp-20261016-action-f691d2: inconclusive

- **Change:** `action.reply.generation` 'persona_json' → 'continuation'
- **Hypothesis:** Continuing the real thread as a document on claude-opus-4-5 reads less like AI than the persona JSON call: research measured forced-choice detection of 31% against 100%.
- **Proposed by:** manual (research/action.md, finding 2)
- **ai_detection_rate** (lower is better), 17 threads: champion 0.941 [0.824, 1.000], challenger 0.353 [0.118, 0.588], difference -0.588 [-0.824, -0.294]
- **Noise floor** (champion rerun with new lineups or seeds): 0.059
- **Correction:** Benjamini-Hochberg q=0.1 over 1 comparison(s); p=0.0005, adjusted 0.0005
- **Decision:** improvement lower bound +0.294 at 0.975 clears the noise floor 0.059; blocked: not shown non-inferior on veto_pass_rate (95% lower bound -0.353, margin -0.1) (auto, 2026-10-16T02:06:00+00:00)
- **Cost:** 51 calls on the challenger arm, plus shared champion arms (102 calls)

## exp-20261016-action-fbb933: superseded

- **Change:** `action.reply.generation` 'persona_json' → 'continuation'
- **Hypothesis:** Continuing the real thread as a document on claude-opus-4-5 reads less like AI than the persona JSON call: research measured forced-choice detection of 31% against 100%.
- **Proposed by:** manual (research/action.md, finding 2)
- **ai_detection_rate** (lower is better), 17 threads: champion 0.941 [0.824, 1.000], challenger 0.118 [0.000, 0.294], difference -0.824 [-1.000, -0.647]
- **Noise floor** (champion rerun with new lineups or seeds): 0.059
- **Correction:** Benjamini-Hochberg q=0.1 over 1 comparison(s); p=0.0005, adjusted 0.0005
- **Decision:** Ran under a gate later found too loose: with a family of one, BH at q=0.1 set a one-sided 90% bound, and there was no veto guardrail. The gate was tightened after this result was seen (97.5% floor, non-inferiority on veto pass rate), which is itself a change made after looking. exp-20261016-action-f691d2 reran the same comparison under the tightened gate. (human, 2026-10-16T02:05:03+00:00)
- **Cost:** 34 calls on the challenger arm, plus shared champion arms (68 calls)

