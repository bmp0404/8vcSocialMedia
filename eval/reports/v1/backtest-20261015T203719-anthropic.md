# Backtest 2026-10-15T20:37:19

Provider `anthropic`, model `claude-sonnet-5-5`, panel `/Users/samgu/Programming Projects/8vcSocialMedia-worktrees/eval/eval/panels/devtools.json`. Holdout: posts from 2026-10-06 on; 357 sampled of 471 scorable (128 fragments and replies dropped). Sampled categories: {'Super Viral': 27, 'Medium Viral': 96, 'Deadweight': 234}. Target: local author median. Brackets are 95% intervals from resampling accounts.

| Predictor | Within-account pair accuracy | Top pick beats account median | Within-account Spearman | Cross-account Spearman (log engagement) | Super Viral AUC | P@10 Super Viral | Brier (constant) | ECE |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| random | 0.518 [0.47, 0.57] | 0.531 [0.42, 0.64] | 0.038 [-0.06, 0.13] | -0.033 [-0.14, 0.07] | 0.463 [0.37, 0.55] | 0.000 [0.00, 0.00] | n/a | n/a |
| media | 0.582 [0.52, 0.63] | 0.609 [0.53, 0.68] | 0.279 [0.15, 0.40] | 0.168 [0.01, 0.32] | 0.570 [0.45, 0.69] | 0.111 [0.04, 0.19] | n/a | n/a |
| length | 0.600 [0.55, 0.65] | 0.648 [0.54, 0.75] | 0.226 [0.10, 0.35] | 0.049 [-0.08, 0.18] | 0.461 [0.34, 0.59] | 0.100 [0.00, 0.30] | n/a | n/a |
| panel | 0.592 [0.53, 0.65] | 0.605 [0.50, 0.71] | 0.230 [0.10, 0.35] | 0.436 [0.28, 0.56] | 0.821 [0.68, 0.92] | 0.800 [0.20, 1.00] | 0.345 (0.249) | 0.329 |

Pairs compared within accounts: 834. Accounts with 2+ sampled posts: 64. Super Viral base rate in the sample: 0.076.

`random` under the category-balanced protocol (7 per category from Smaller/closest, Mid-size, 500 draws): cross-account Spearman median 0.110 (95% of draws -0.18 to 0.34); within-account pair accuracy on the same 95 posts 0.456 over 215 pairs.

`media` under the category-balanced protocol (7 per category from Smaller/closest, Mid-size, 500 draws): cross-account Spearman median 0.464 (95% of draws 0.21 to 0.64); within-account pair accuracy on the same 95 posts 0.644 over 215 pairs.

`length` under the category-balanced protocol (7 per category from Smaller/closest, Mid-size, 500 draws): cross-account Spearman median 0.340 (95% of draws 0.03 to 0.62); within-account pair accuracy on the same 95 posts 0.658 over 215 pairs.

`panel` under the category-balanced protocol (7 per category from Smaller/closest, Mid-size, 500 draws): cross-account Spearman median 0.679 (95% of draws 0.54 to 0.81); within-account pair accuracy on the same 95 posts 0.693 over 215 pairs.

`panel` within-account pair accuracy versus baselines on the same pairs: minus length: -0.008 [-0.07, 0.05]; minus media: 0.010 [-0.07, 0.08]; length+media 0.636, adding `panel` 0.634 (gain -0.002 [-0.04, 0.03]).

Retest of `panel` on 60 posts: Spearman between runs 0.956, mean absolute change 0.021.

Calibration of `panel` (engagement strictly above the account's median, base rate 0.468):

| Score bin | Posts | Mean score | Observed rate |
| --- | --- | --- | --- |
| 0.0-0.2 | 273 | 0.095 | 0.432 |
| 0.2-0.4 | 77 | 0.263 | 0.545 |
| 0.4-0.6 | 6 | 0.485 | 1.000 |
| 0.6-0.8 | 1 | 0.625 | 1.000 |
