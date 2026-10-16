# Backtest 2026-10-15T20:54:19

Provider `anthropic`, models `claude-sonnet-5-5, gpt-6.1-sol`, panel `/Users/samgu/Programming Projects/8vcSocialMedia-worktrees/eval/eval/panels/devtools.json`, judge memory `on`. v2: within-account viral vs Deadweight, one post per model call.

Eligible posts: 848 (dropped: {'unscorable_text_or_reply': 388, 'thread_continuation': 316, 'younger_than_48h': 113}). Sample: 144 posts, 144 scored by every predictor, from 27 accounts with both tiers ({'Super Viral': 9, 'Medium Viral': 64, 'Deadweight': 71}). Headline pairs: 192. Brackets are 95% intervals from resampling accounts. Every number is an upper bound for production (see eval/DESIGN.md).

| Predictor | Viral vs Deadweight, same account (headline) | Same media status | Same launch type | Viral vs viral | Deadweight vs Deadweight (noise) | Top pick is viral |
| --- | --- | --- | --- | --- | --- | --- |
| random | 0.495 [0.39, 0.60] | 0.507 [0.36, 0.65] | 0.533 [0.43, 0.64] | 0.537 [0.44, 0.65] | 0.543 [0.43, 0.66] | 0.537 |
| account | 0.500 [0.50, 0.50] | 0.500 [0.50, 0.50] | 0.500 [0.50, 0.50] | 0.500 [0.50, 0.50] | 0.500 [0.50, 0.50] | 0.506 |
| media | 0.737 [0.63, 0.84] | 0.633 [0.57, 0.70] | 0.709 [0.57, 0.86] | 0.573 [0.50, 0.65] | 0.521 [0.40, 0.65] | 0.707 |
| length | 0.680 [0.54, 0.82] | 0.667 [0.49, 0.85] | 0.652 [0.49, 0.82] | 0.476 [0.38, 0.57] | 0.600 [0.51, 0.69] | 0.639 |
| tfidf | 0.672 [0.55, 0.80] | 0.707 [0.57, 0.85] | 0.623 [0.46, 0.78] | 0.451 [0.33, 0.56] | 0.643 [0.54, 0.75] | 0.630 |
| panel@claude-sonnet-5-5 | 0.797 [0.72, 0.89] | 0.813 [0.72, 0.91] | 0.754 [0.65, 0.88] | 0.506 [0.35, 0.66] | 0.471 [0.34, 0.60] | 0.759 |
| panel@gpt-6.1-sol | 0.854 [0.76, 0.95] | 0.813 [0.69, 0.93] | 0.795 [0.67, 0.95] | 0.500 [0.39, 0.61] | 0.529 [0.37, 0.69] | 0.796 |
| reader@claude-sonnet-5-5 | 0.719 [0.64, 0.81] | 0.720 [0.62, 0.83] | 0.697 [0.59, 0.81] | 0.555 [0.41, 0.69] | 0.557 [0.48, 0.64] | 0.786 |
| reader@gpt-6.1-sol | 0.823 [0.75, 0.90] | 0.807 [0.72, 0.89] | 0.811 [0.71, 0.91] | 0.524 [0.43, 0.61] | 0.621 [0.49, 0.73] | 0.833 |
| panel@mean | 0.836 [0.75, 0.93] | 0.820 [0.70, 0.93] | 0.791 [0.68, 0.93] | 0.530 [0.39, 0.67] | 0.471 [0.31, 0.63] | 0.796 |
| reader@mean | 0.828 [0.75, 0.92] | 0.820 [0.72, 0.92] | 0.807 [0.70, 0.92] | 0.549 [0.40, 0.69] | 0.621 [0.46, 0.76] | 0.843 |

Without the 9 boost-suspect posts (135 posts, 170 pairs), headline: `random` 0.494 [0.39, 0.61]; `account` 0.500 [0.50, 0.50]; `media` 0.732 [0.61, 0.84]; `length` 0.715 [0.58, 0.83]; `tfidf` 0.629 [0.49, 0.76]; `panel@claude-sonnet-5-5` 0.788 [0.71, 0.89]; `panel@gpt-6.1-sol` 0.865 [0.78, 0.95]; `reader@claude-sonnet-5-5` 0.694 [0.61, 0.79]; `reader@gpt-6.1-sol` 0.812 [0.73, 0.90]; `panel@mean` 0.841 [0.76, 0.93]; `reader@mean` 0.818 [0.73, 0.92]

Paired differences in the headline AUC (positive means the row predictor wins):

- `panel@claude-sonnet-5-5`: minus media 0.060 [-0.05, 0.18]; minus length 0.117 [-0.01, 0.26]; minus tfidf 0.125 [-0.01, 0.25]; minus reader@claude-sonnet-5-5 0.078 [0.02, 0.13]; minus reader@gpt-6.1-sol -0.026 [-0.10, 0.04]; minus reader@mean -0.031 [-0.09, 0.03]
- `panel@gpt-6.1-sol`: minus media 0.117 [0.01, 0.22]; minus length 0.174 [0.07, 0.30]; minus tfidf 0.182 [0.02, 0.33]; minus reader@claude-sonnet-5-5 0.135 [0.03, 0.24]; minus reader@gpt-6.1-sol 0.031 [-0.06, 0.12]; minus reader@mean 0.026 [-0.06, 0.12]
- `panel@mean`: minus media 0.099 [-0.01, 0.20]; minus length 0.156 [0.04, 0.29]; minus tfidf 0.164 [0.01, 0.30]; minus reader@claude-sonnet-5-5 0.117 [0.03, 0.21]; minus reader@gpt-6.1-sol 0.013 [-0.07, 0.09]; minus reader@mean 0.008 [-0.07, 0.09]
- `reader@claude-sonnet-5-5`: minus media -0.018 [-0.14, 0.10]; minus length 0.039 [-0.11, 0.19]; minus tfidf 0.047 [-0.06, 0.15]
- `reader@gpt-6.1-sol`: minus media 0.086 [-0.03, 0.20]; minus length 0.143 [-0.03, 0.32]; minus tfidf 0.151 [0.05, 0.25]
- `reader@mean`: minus media 0.091 [-0.03, 0.20]; minus length 0.148 [-0.02, 0.32]; minus tfidf 0.156 [0.07, 0.25]

Confounded by account size (cross-account; a model that knows which brands are big scores well here without judging the writing):

| Predictor | Spearman vs log engagement | Super Viral vs rest AUC | Category-balanced protocol (aarav/self-improving) |
| --- | --- | --- | --- |
| random | -0.023 | 0.590 | 0.427 (-0.14 to 0.83) |
| account | 0.201 | 0.595 | 0.408 (0.07 to 0.67) |
| media | 0.412 | 0.614 | 0.577 (0.32 to 0.75) |
| length | 0.243 | 0.460 | -0.140 (-0.50 to 0.21) |
| tfidf | 0.218 | 0.542 | 0.175 (-0.07 to 0.41) |
| panel@claude-sonnet-5-5 | 0.557 | 0.847 | 0.776 (0.43 to 0.90) |
| panel@gpt-6.1-sol | 0.507 | 0.767 | 0.607 (0.37 to 0.83) |
| reader@claude-sonnet-5-5 | 0.498 | 0.742 | 0.472 (0.15 to 0.68) |
| reader@gpt-6.1-sol | 0.609 | 0.809 | 0.623 (0.36 to 0.81) |
| panel@mean | 0.558 | 0.819 | 0.720 (0.49 to 0.87) |
| reader@mean | 0.609 | 0.798 | 0.576 (0.31 to 0.74) |

- Retest of `panel@claude-sonnet-5-5` on 40 posts: Spearman between runs 0.932.
- `panel@claude-sonnet-5-5`: 144 model calls (claude-sonnet-5-5).
- Retest of `panel@gpt-6.1-sol` on 40 posts: Spearman between runs 0.986.
- `panel@gpt-6.1-sol`: 144 model calls (gpt-6.1-sol).
- Retest of `reader@claude-sonnet-5-5` on 40 posts: Spearman between runs 0.837.
- `reader@claude-sonnet-5-5`: 144 model calls (claude-sonnet-5-5).
- Retest of `reader@gpt-6.1-sol` on 40 posts: Spearman between runs 0.813.
- `reader@gpt-6.1-sol`: 144 model calls (gpt-6.1-sol).
