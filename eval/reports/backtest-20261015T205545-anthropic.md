# Backtest 2026-10-15T20:55:45

Provider `anthropic`, models `claude-sonnet-5-5`, panel `/Users/samgu/Programming Projects/8vcSocialMedia-worktrees/eval-judge-scratch/eval/panels/devtools.json`, judge memory `on`. v2: within-account viral vs Deadweight, one post per model call.

Eligible posts: 848 (dropped: {'unscorable_text_or_reply': 388, 'thread_continuation': 316, 'younger_than_48h': 113}). Sample: 144 posts, 122 scored by every predictor, from 26 accounts with both tiers ({'Super Viral': 9, 'Medium Viral': 53, 'Deadweight': 60}). Headline pairs: 148. Brackets are 95% intervals from resampling accounts. Every number is an upper bound for production (see eval/DESIGN.md).

| Predictor | Viral vs Deadweight, same account (headline) | Viral vs viral, same account (second headline) | Same media status | Same launch type | Deadweight vs Deadweight (noise) | Top pick is viral |
| --- | --- | --- | --- | --- | --- | --- |
| random | 0.507 [0.38, 0.63] | 0.462 [0.38, 0.55] | 0.519 [0.33, 0.69] | 0.554 [0.43, 0.69] | 0.585 [0.45, 0.71] | 0.538 |
| account | 0.500 [0.50, 0.50] | 0.500 [0.50, 0.50] | 0.500 [0.50, 0.50] | 0.500 [0.50, 0.50] | 0.500 [0.50, 0.50] | 0.530 |
| media | 0.764 [0.66, 0.85] | 0.569 [0.48, 0.67] | 0.630 [0.56, 0.71] | 0.745 [0.61, 0.85] | 0.538 [0.40, 0.66] | 0.699 |
| length | 0.693 [0.54, 0.84] | 0.477 [0.39, 0.57] | 0.704 [0.49, 0.91] | 0.690 [0.52, 0.88] | 0.585 [0.47, 0.70] | 0.663 |
| tfidf | 0.669 [0.54, 0.80] | 0.446 [0.31, 0.58] | 0.704 [0.55, 0.87] | 0.620 [0.46, 0.76] | 0.623 [0.49, 0.75] | 0.635 |
| post_judge | 0.767 [0.63, 0.90] | 0.462 [0.37, 0.56] | 0.796 [0.63, 0.94] | 0.745 [0.57, 0.93] | 0.575 [0.45, 0.69] | 0.756 |
| post_judge_rank | 0.824 [0.72, 0.94] | 0.452 [0.27, 0.61] | 0.929 [0.82, 1.00] | 0.833 [0.69, 0.97] | 0.370 [0.11, 0.65] | 0.720 |
| post_judge_refs_only | 0.682 [0.55, 0.82] | 0.431 [0.32, 0.56] | 0.778 [0.62, 0.92] | 0.663 [0.52, 0.81] | 0.585 [0.48, 0.72] | 0.692 |

Without the 6 boost-suspect posts (116 posts, 138 pairs), headline: `random` 0.507 [0.38, 0.63]; `account` 0.500 [0.50, 0.50]; `media` 0.761 [0.66, 0.85]; `length` 0.714 [0.57, 0.84]; `tfidf` 0.645 [0.51, 0.78]; `post_judge` 0.768 [0.62, 0.90]; `post_judge_rank` 0.811 [0.71, 0.94]; `post_judge_refs_only` 0.667 [0.53, 0.80]

Paired differences in the headline AUC (positive means the row predictor wins):

- `post_judge`: minus media 0.003 [-0.16, 0.16]; minus length 0.074 [-0.02, 0.18]; minus tfidf 0.098 [-0.14, 0.35]; minus post_judge_refs_only 0.084 [-0.14, 0.31]
- `post_judge_rank`: minus media 0.085 [-0.07, 0.25]; minus length 0.063 [-0.12, 0.26]; minus tfidf 0.148 [-0.01, 0.30]
- `post_judge_refs_only`: minus media -0.081 [-0.25, 0.10]; minus length -0.010 [-0.26, 0.25]; minus tfidf 0.014 [-0.10, 0.12]

Confounded by account size (cross-account; a model that knows which brands are big scores well here without judging the writing):

| Predictor | Spearman vs log engagement | Super Viral vs rest AUC | Super vs Medium Viral AUC (author floor 0.763) | Category-balanced protocol (aarav/self-improving) |
| --- | --- | --- | --- | --- |
| random | -0.031 | 0.594 | 0.618 | 0.378 (-0.13 to 0.76) |
| account | 0.239 | 0.565 | 0.498 | 0.428 (0.09 to 0.67) |
| media | 0.419 | 0.611 | 0.476 | 0.558 (0.33 to 0.74) |
| length | 0.215 | 0.464 | 0.370 | -0.155 (-0.52 to 0.21) |
| tfidf | 0.205 | 0.538 | 0.505 | 0.189 (-0.12 to 0.41) |
| post_judge | 0.352 | 0.649 | 0.482 | 0.323 (-0.09 to 0.63) |
| post_judge_rank | 0.455 | 0.641 | 0.426 | n/a |
| post_judge_refs_only | 0.239 | 0.698 | 0.667 | 0.444 (-0.06 to 0.77) |

- `post_judge` failed on 11 posts, dropped from every predictor.
- `post_judge_rank` failed on 17 posts, dropped from every predictor.
