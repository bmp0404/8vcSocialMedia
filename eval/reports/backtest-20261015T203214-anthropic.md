# Backtest 2026-10-15T20:32:14

Provider `anthropic`, model `claude-sonnet-5-5`, panel `/Users/samgu/Programming Projects/8vcSocialMedia-worktrees/eval/eval/panels/devtools.json`. Holdout: posts from 2026-10-06 on; 83 sampled of 471 scorable (128 fragments and replies dropped). Sampled categories: {'Super Viral': 6, 'Medium Viral': 20, 'Deadweight': 57}. Target: local author median. Brackets are 95% intervals from resampling accounts.

| Predictor | Within-account pair accuracy | Top pick beats account median | Within-account Spearman | Cross-account Spearman (log engagement) | Super Viral AUC | P@10 Super Viral | Brier (constant) | ECE |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| random | 0.479 [0.41, 0.56] | 0.575 [0.38, 0.78] | -0.088 [-0.24, 0.10] | 0.037 [-0.20, 0.28] | 0.476 [0.10, 0.76] | 0.100 [0.00, 0.30] | n/a | n/a |
| media | 0.521 [0.46, 0.60] | 0.568 [0.44, 0.69] | 0.150 [-0.02, 0.34] | 0.011 [-0.23, 0.25] | 0.448 [0.19, 0.69] | 0.000 [0.00, 0.00] | n/a | n/a |
| length | 0.623 [0.49, 0.72] | 0.625 [0.42, 0.82] | 0.177 [-0.03, 0.41] | -0.128 [-0.35, 0.19] | 0.345 [0.21, 0.49] | 0.000 [0.00, 0.00] | n/a | n/a |
| panel | 0.489 [0.35, 0.64] | 0.533 [0.34, 0.73] | 0.043 [-0.23, 0.32] | 0.333 [-0.09, 0.66] | 0.966 [0.92, 1.00] | 0.500 [0.00, 0.90] | 0.390 (0.250) | 0.369 |

Pairs compared within accounts: 142. Accounts with 2+ sampled posts: 20. Super Viral base rate in the sample: 0.072.

Retest of `panel` on 40 posts: Spearman between runs 0.931, mean absolute change 0.027.

Calibration of `panel` (engagement strictly above the account's median, base rate 0.518):

| Score bin | Posts | Mean score | Observed rate |
| --- | --- | --- | --- |
| 0.0-0.2 | 66 | 0.100 | 0.515 |
| 0.2-0.4 | 13 | 0.274 | 0.385 |
| 0.4-0.6 | 3 | 0.513 | 1.000 |
| 0.6-0.8 | 1 | 0.637 | 1.000 |
