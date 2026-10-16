# Backtest 2026-10-15T21:12:53

Provider `anthropic`, models `claude-sonnet-5-5`, panel `/Users/samgu/Programming Projects/8vcSocialMedia-worktrees/eval-judge-scratch/eval/panels/devtools.json`, judge memory `shuffled`. v2: within-account viral vs Deadweight, one post per model call.

Eligible posts: 848 (dropped: {'unscorable_text_or_reply': 388, 'thread_continuation': 316, 'younger_than_48h': 113}). Sample: 144 posts, 136 scored by every predictor, from 27 accounts with both tiers ({'Super Viral': 9, 'Medium Viral': 60, 'Deadweight': 67}). Headline pairs: 170. Brackets are 95% intervals from resampling accounts. Every number is an upper bound for production (see eval/DESIGN.md).

| Predictor | Viral vs Deadweight, same account (headline) | Viral vs viral, same account (second headline) | Same media status | Same launch type | Deadweight vs Deadweight (noise) | Top pick is viral |
| --- | --- | --- | --- | --- | --- | --- |
| random | 0.494 [0.36, 0.61] | 0.520 [0.42, 0.64] | 0.508 [0.34, 0.66] | 0.546 [0.41, 0.67] | 0.557 [0.43, 0.67] | 0.574 |
| media | 0.750 [0.64, 0.85] | 0.547 [0.48, 0.63] | 0.627 [0.56, 0.71] | 0.713 [0.57, 0.85] | 0.541 [0.43, 0.65] | 0.698 |
| post_judge | 0.697 [0.59, 0.82] | 0.567 [0.48, 0.68] | 0.722 [0.62, 0.85] | 0.690 [0.55, 0.86] | 0.492 [0.35, 0.62] | 0.648 |
| post_judge_refs_only | 0.682 [0.57, 0.81] | 0.453 [0.35, 0.58] | 0.778 [0.65, 0.91] | 0.648 [0.52, 0.79] | 0.639 [0.52, 0.77] | 0.704 |

Without the 7 boost-suspect posts (129 posts, 154 pairs), headline: `random` 0.494 [0.36, 0.62]; `media` 0.747 [0.63, 0.85]; `post_judge` 0.688 [0.57, 0.82]; `post_judge_refs_only` 0.656 [0.54, 0.78]

Paired differences in the headline AUC (positive means the row predictor wins):

- `post_judge`: minus media -0.053 [-0.14, 0.04]; minus post_judge_refs_only 0.015 [-0.16, 0.19]
- `post_judge_refs_only`: minus media -0.068 [-0.21, 0.09]

Confounded by account size (cross-account; a model that knows which brands are big scores well here without judging the writing):

| Predictor | Spearman vs log engagement | Super Viral vs rest AUC | Super vs Medium Viral AUC (author floor 0.763) | Category-balanced protocol (aarav/self-improving) |
| --- | --- | --- | --- | --- |
| random | -0.027 | 0.589 | 0.607 | 0.399 (-0.13 to 0.78) |
| media | 0.403 | 0.611 | 0.481 | 0.580 (0.30 to 0.74) |
| post_judge | 0.306 | 0.582 | 0.447 | 0.213 (-0.24 to 0.55) |
| post_judge_refs_only | 0.303 | 0.707 | 0.659 | 0.462 (-0.06 to 0.80) |

- `post_judge` failed on 8 posts, dropped from every predictor.
