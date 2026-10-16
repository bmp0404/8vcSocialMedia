# Backtest 2026-10-15T20:32:56

Provider `anthropic`, model `claude-sonnet-5-5`, panel `/Users/samgu/Programming Projects/8vcSocialMedia-worktrees/eval/eval/panels/devtools.json`. Holdout: posts from 2026-10-06 on; 357 sampled of 471 scorable (128 fragments and replies dropped). Sampled categories: {'Super Viral': 27, 'Medium Viral': 96, 'Deadweight': 234}. Target: local author median. Brackets are 95% intervals from resampling accounts.

| Predictor | Within-account pair accuracy | Top pick beats account median | Within-account Spearman | Cross-account Spearman (log engagement) | Super Viral AUC | P@10 Super Viral | Brier (constant) | ECE |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| random | 0.518 [0.47, 0.57] | 0.531 [0.42, 0.64] | 0.038 [-0.06, 0.13] | -0.033 [-0.14, 0.08] | 0.463 [0.37, 0.55] | 0.000 [0.00, 0.00] | n/a | n/a |
| media | 0.582 [0.53, 0.64] | 0.609 [0.54, 0.68] | 0.279 [0.16, 0.40] | 0.168 [0.01, 0.33] | 0.570 [0.45, 0.70] | 0.111 [0.04, 0.18] | n/a | n/a |
| length | 0.600 [0.55, 0.65] | 0.648 [0.54, 0.75] | 0.226 [0.10, 0.35] | 0.049 [-0.08, 0.18] | 0.461 [0.33, 0.60] | 0.100 [0.00, 0.30] | n/a | n/a |
| panel | 0.585 [0.53, 0.64] | 0.605 [0.50, 0.71] | 0.239 [0.12, 0.36] | 0.458 [0.32, 0.58] | 0.830 [0.70, 0.92] | 0.733 [0.30, 1.00] | 0.343 (0.249) | 0.327 |

Pairs compared within accounts: 834. Accounts with 2+ sampled posts: 64. Super Viral base rate in the sample: 0.076.

Retest of `panel` on 60 posts: Spearman between runs 0.972, mean absolute change 0.015.

Calibration of `panel` (engagement strictly above the account's median, base rate 0.468):

| Score bin | Posts | Mean score | Observed rate |
| --- | --- | --- | --- |
| 0.0-0.2 | 270 | 0.095 | 0.422 |
| 0.2-0.4 | 78 | 0.260 | 0.564 |
| 0.4-0.6 | 8 | 0.449 | 1.000 |
| 0.6-0.8 | 1 | 0.675 | 1.000 |
