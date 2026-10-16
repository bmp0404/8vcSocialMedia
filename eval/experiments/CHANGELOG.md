# Experiment changelog

Generated from the `exp-*.json` records in this folder by `harness/iterate/ledger.py`. Newest first. Every number is from the run named in its record; brackets are 95% intervals from resampling the cluster unit (accounts or threads). DESIGN.md explains the gate.

Current champion: `cfg-17e3b75195e4`, shaped by 0 promotion(s); all knobs are at their defaults.

## exp-20261016-action-f691d2: inconclusive

- **Change:** `action.reply.generation` 'persona_json' → 'continuation'
- **Hypothesis:** Continuing the real thread as a document on claude-opus-4-5 reads less like AI than the persona JSON call: research measured forced-choice detection of 31% against 100%.
- **Proposed by:** manual (research/action.md, finding 2)
- **ai_detection_rate** (lower is better), 17 threads in 17 threads: champion 0.941 [0.824, 1.000], challenger 0.353 [0.118, 0.588], difference -0.588 [-0.824, -0.294]
- **Noise floor** (champion rerun with new lineups or seeds): 0.059
- **Correction:** Benjamini-Hochberg q=0.1 over 1 comparison(s); p=0.0005, adjusted 0.0005
- **Decision:** improvement lower bound +0.294 at 0.975 clears the noise floor 0.059; blocked: not shown non-inferior on veto_pass_rate (95% lower bound -0.353, margin -0.1) (auto, 2026-10-16T02:06:00+00:00)
- **Cost:** 51 calls on the challenger arm, plus shared champion arms (102 calls)

