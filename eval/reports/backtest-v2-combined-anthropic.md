# Backtest 2026-10-15T20:54:19

Provider `anthropic`, models `claude-sonnet-5-5, gpt-6.1-sol`, panel `/Users/samgu/Programming Projects/8vcSocialMedia-worktrees/eval/eval/panels/devtools.json`, judge memory `on`. v2: within-account viral vs Deadweight, one post per model call.

Eligible posts: 848 (dropped: {'unscorable_text_or_reply': 388, 'thread_continuation': 316, 'younger_than_48h': 113}). Sample: 144 posts, 120 scored by every predictor, from 26 accounts with both tiers ({'Super Viral': 9, 'Medium Viral': 52, 'Deadweight': 59}). Headline pairs: 140. Brackets are 95% intervals from resampling accounts. Every number is an upper bound for production (see eval/DESIGN.md).

| Predictor | Viral vs Deadweight, same account (headline) | Viral vs viral, same account (second headline) | Same media status | Same launch type | Deadweight vs Deadweight (noise) | Top pick is viral |
| --- | --- | --- | --- | --- | --- | --- |
| random | 0.493 [0.34, 0.63] | 0.460 [0.37, 0.56] | 0.510 [0.29, 0.69] | 0.539 [0.38, 0.69] | 0.608 [0.47, 0.74] | 0.538 |
| account | 0.500 [0.50, 0.50] | 0.500 [0.50, 0.50] | 0.500 [0.50, 0.50] | 0.500 [0.50, 0.50] | 0.500 [0.50, 0.50] | 0.520 |
| media | 0.768 [0.67, 0.86] | 0.556 [0.47, 0.65] | 0.637 [0.57, 0.73] | 0.736 [0.60, 0.85] | 0.549 [0.42, 0.67] | 0.680 |
| length | 0.689 [0.53, 0.84] | 0.460 [0.37, 0.56] | 0.686 [0.47, 0.90] | 0.702 [0.53, 0.88] | 0.569 [0.45, 0.68] | 0.663 |
| tfidf | 0.657 [0.53, 0.79] | 0.444 [0.30, 0.58] | 0.706 [0.55, 0.88] | 0.607 [0.46, 0.74] | 0.608 [0.48, 0.74] | 0.615 |
| panel@claude-sonnet-5-5 | 0.811 [0.74, 0.91] | 0.492 [0.32, 0.68] | 0.833 [0.75, 0.94] | 0.792 [0.68, 0.92] | 0.392 [0.24, 0.55] | 0.788 |
| panel@gpt-6.1-sol | 0.900 [0.84, 0.95] | 0.540 [0.41, 0.68] | 0.863 [0.78, 0.94] | 0.876 [0.79, 0.96] | 0.441 [0.26, 0.64] | 0.808 |
| reader@claude-sonnet-5-5 | 0.721 [0.64, 0.83] | 0.540 [0.38, 0.70] | 0.735 [0.62, 0.87] | 0.713 [0.60, 0.84] | 0.510 [0.44, 0.58] | 0.779 |
| reader@gpt-6.1-sol | 0.846 [0.77, 0.93] | 0.516 [0.41, 0.61] | 0.853 [0.77, 0.93] | 0.871 [0.77, 0.96] | 0.539 [0.40, 0.66] | 0.827 |
| post_judge | 0.779 [0.66, 0.90] | 0.468 [0.38, 0.57] | 0.784 [0.62, 0.93] | 0.770 [0.61, 0.93] | 0.559 [0.44, 0.67] | 0.756 |
| post_judge_rank | 0.831 [0.73, 0.95] | 0.452 [0.27, 0.61] | 0.923 [0.81, 1.00] | 0.852 [0.73, 0.97] | 0.341 [0.09, 0.61] | 0.707 |
| post_judge_memory_shuffled | 0.711 [0.60, 0.84] | 0.540 [0.44, 0.65] | 0.716 [0.61, 0.87] | 0.719 [0.57, 0.88] | 0.520 [0.37, 0.66] | 0.683 |
| panel@mean | 0.871 [0.79, 0.96] | 0.524 [0.36, 0.70] | 0.863 [0.78, 0.95] | 0.865 [0.75, 0.98] | 0.373 [0.19, 0.57] | 0.827 |
| reader@mean | 0.846 [0.76, 0.95] | 0.540 [0.37, 0.71] | 0.853 [0.77, 0.95] | 0.865 [0.75, 0.97] | 0.559 [0.35, 0.73] | 0.837 |
| post_judge_refs_only | 0.671 [0.54, 0.81] | 0.429 [0.32, 0.56] | 0.765 [0.61, 0.92] | 0.663 [0.51, 0.81] | 0.588 [0.48, 0.73] | 0.673 |

Without the 6 boost-suspect posts (114 posts, 130 pairs), headline: `random` 0.492 [0.34, 0.64]; `account` 0.500 [0.50, 0.50]; `media` 0.765 [0.66, 0.86]; `length` 0.712 [0.56, 0.84]; `tfidf` 0.631 [0.50, 0.77]; `panel@claude-sonnet-5-5` 0.796 [0.71, 0.90]; `panel@gpt-6.1-sol` 0.892 [0.83, 0.95]; `reader@claude-sonnet-5-5` 0.700 [0.61, 0.81]; `reader@gpt-6.1-sol` 0.835 [0.76, 0.93]; `post_judge` 0.781 [0.65, 0.90]; `post_judge_rank` 0.817 [0.72, 0.94]; `post_judge_memory_shuffled` 0.708 [0.58, 0.84]; `panel@mean` 0.862 [0.77, 0.95]; `reader@mean` 0.835 [0.75, 0.94]; `post_judge_refs_only` 0.654 [0.52, 0.79]

Paired differences in the headline AUC (positive means the row predictor wins):

- `panel@claude-sonnet-5-5`: minus media 0.043 [-0.08, 0.19]; minus length 0.121 [-0.04, 0.29]; minus tfidf 0.154 [0.01, 0.30]; minus reader@claude-sonnet-5-5 0.089 [0.02, 0.15]; minus reader@gpt-6.1-sol -0.036 [-0.11, 0.04]; minus reader@mean -0.036 [-0.10, 0.04]
- `panel@gpt-6.1-sol`: minus media 0.132 [0.03, 0.24]; minus length 0.211 [0.07, 0.36]; minus tfidf 0.243 [0.09, 0.40]; minus reader@claude-sonnet-5-5 0.179 [0.07, 0.27]; minus reader@gpt-6.1-sol 0.054 [-0.03, 0.13]; minus reader@mean 0.054 [-0.04, 0.14]
- `post_judge`: minus media 0.011 [-0.12, 0.15]; minus length 0.089 [0.00, 0.18]; minus tfidf 0.121 [-0.11, 0.35]; minus reader@claude-sonnet-5-5 0.057 [-0.10, 0.21]; minus reader@gpt-6.1-sol -0.068 [-0.23, 0.09]; minus reader@mean -0.068 [-0.23, 0.09]; minus post_judge_refs_only 0.107 [-0.12, 0.33]; minus post_judge_memory_shuffled 0.068 [-0.01, 0.15]
- `post_judge_rank`: minus media 0.088 [-0.06, 0.25]; minus length 0.081 [-0.10, 0.27]; minus tfidf 0.154 [0.01, 0.30]; minus reader@claude-sonnet-5-5 0.074 [-0.03, 0.19]; minus reader@gpt-6.1-sol -0.044 [-0.12, 0.04]; minus reader@mean -0.037 [-0.11, 0.05]
- `panel@mean`: minus media 0.104 [-0.02, 0.24]; minus length 0.182 [0.02, 0.34]; minus tfidf 0.214 [0.05, 0.36]; minus reader@claude-sonnet-5-5 0.150 [0.05, 0.23]; minus reader@gpt-6.1-sol 0.025 [-0.06, 0.10]; minus reader@mean 0.025 [-0.06, 0.11]
- `post_judge_refs_only`: minus media -0.096 [-0.26, 0.09]; minus length -0.018 [-0.28, 0.25]; minus tfidf 0.014 [-0.10, 0.12]; minus reader@claude-sonnet-5-5 -0.050 [-0.19, 0.10]; minus reader@gpt-6.1-sol -0.175 [-0.31, -0.06]; minus reader@mean -0.175 [-0.30, -0.06]
- `reader@claude-sonnet-5-5`: minus media -0.046 [-0.17, 0.12]; minus length 0.032 [-0.15, 0.22]; minus tfidf 0.064 [-0.05, 0.18]
- `reader@gpt-6.1-sol`: minus media 0.079 [-0.05, 0.22]; minus length 0.157 [-0.04, 0.36]; minus tfidf 0.189 [0.08, 0.31]
- `reader@mean`: minus media 0.079 [-0.05, 0.23]; minus length 0.157 [-0.04, 0.36]; minus tfidf 0.189 [0.09, 0.31]

Confounded by account size (cross-account; a model that knows which brands are big scores well here without judging the writing):

| Predictor | Spearman vs log engagement | Super Viral vs rest AUC | Super vs Medium Viral AUC (author floor 0.763) | Category-balanced protocol (aarav/self-improving) |
| --- | --- | --- | --- | --- |
| random | -0.050 | 0.588 | 0.613 | 0.361 (-0.13 to 0.80) |
| account | 0.233 | 0.560 | 0.490 | 0.407 (0.05 to 0.65) |
| media | 0.414 | 0.606 | 0.468 | 0.555 (0.32 to 0.71) |
| length | 0.217 | 0.467 | 0.373 | -0.147 (-0.50 to 0.23) |
| tfidf | 0.206 | 0.538 | 0.504 | 0.172 (-0.09 to 0.41) |
| panel@claude-sonnet-5-5 | 0.572 | 0.842 | 0.737 | 0.762 (0.40 to 0.91) |
| panel@gpt-6.1-sol | 0.534 | 0.769 | 0.637 | 0.627 (0.37 to 0.82) |
| reader@claude-sonnet-5-5 | 0.532 | 0.728 | 0.626 | 0.446 (0.15 to 0.70) |
| reader@gpt-6.1-sol | 0.646 | 0.797 | 0.678 | 0.611 (0.34 to 0.81) |
| post_judge | 0.361 | 0.655 | 0.489 | 0.351 (-0.07 to 0.67) |
| post_judge_rank | 0.465 | 0.648 | 0.429 | n/a |
| post_judge_memory_shuffled | 0.282 | 0.588 | 0.451 | 0.217 (-0.30 to 0.55) |
| panel@mean | 0.583 | 0.817 | 0.694 | 0.732 (0.47 to 0.88) |
| reader@mean | 0.652 | 0.789 | 0.669 | 0.555 (0.29 to 0.75) |
| post_judge_refs_only | 0.228 | 0.698 | 0.671 | 0.420 (-0.05 to 0.75) |

- Retest of `panel@claude-sonnet-5-5` on 40 posts: Spearman between runs 0.932.
- `panel@claude-sonnet-5-5`: 144 model calls (claude-sonnet-5-5).
- Retest of `panel@gpt-6.1-sol` on 40 posts: Spearman between runs 0.986.
- `panel@gpt-6.1-sol`: 144 model calls (gpt-6.1-sol).
- Retest of `reader@claude-sonnet-5-5` on 40 posts: Spearman between runs 0.837.
- `reader@claude-sonnet-5-5`: 144 model calls (claude-sonnet-5-5).
- Retest of `reader@gpt-6.1-sol` on 40 posts: Spearman between runs 0.813.
- `reader@gpt-6.1-sol`: 144 model calls (gpt-6.1-sol).
- `post_judge` failed on 11 posts, dropped from every predictor.
- `post_judge_rank` failed on 17 posts, dropped from every predictor.
- `post_judge_memory_shuffled` failed on 8 posts, dropped from every predictor.
