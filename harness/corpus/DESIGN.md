# Corpus, benchmarks and rankers

This is for whoever scores a judge, a selector or a change to the loop. It answers three questions. What data do we have? Which comparisons can the data grade by itself, and how well do cheap scorers already do on them? Does a reply's wording predict its likes once timing and the author are known?

Code is in `harness/corpus/` (loader, benchmarks, statistics) and `harness/rank/` (rankers, baselines, openness). Data is in `data/corpus/raw/` and `data/bench/`. LLM labels are in `harness/label/DESIGN.md`.

## Short answers

- The corpus has 14,462 posts and replies from the dev-tool dataset's accounts and the people who reply to them.
- Within a thread, how soon a reply came is most of the signal. Delay alone ranks the more-liked reply 0.755 of the time on test threads. A model on delay, followers and verified status reaches 0.769.
- Adding the reply's text features to that model changes accuracy by +0.002 (95% interval -0.034 to +0.044) over both test weeks. Hashed word n-grams make it worse by 0.071, and LLM label fields add +0.007 (-0.036 to +0.040). On this data the words of a reply add nothing measurable to timing and reach.
- Within an account, the words of a post do carry signal. A TF-IDF ridge model, scored only on accounts it never saw, ranks the better post 0.712 of the time on test authors, 0.120 above length alone. Most of it is purpose: launches beat event promos.

The reply result is a product decision, not just a number. If a reply's words don't move its likes, the harness earns its keep by choosing the thread and the moment and by vetoing bad drafts. The words can come from a person.

## Corpus

`python -m harness.corpus snapshot --src <sam/context-dynamics checkout>/data/context` copies the context worker's fxtwitter reads into `data/corpus/raw/` as gzip (2.6 MB) and writes `MANIFEST.json` with each file's sha256 and line count. `python -m harness.corpus verify` checks the gzips against the manifest. `load()` reads only the snapshot.

| Source file | Rows read | Rows skipped (fetch errors, empty threads) |
| --- | --- | --- |
| `x_metrics.jsonl` (dataset posts) | 1,664 | 1 |
| `author_timelines.jsonl` | 7,480 | 4 |
| `reply_samples.jsonl` | 7,206 | 15 |

`load()` merges rows by status ID and keeps the counts from the most recent read. It yields 14,462 items:

| Kind | Items | Notes |
| --- | --- | --- |
| post | 5,746 | 927 are dataset rows; 1,866 reached the corpus only as a repost on a dataset account's timeline |
| reply | 8,716 | 7,206 from reply samples; 1,789 are thread continuations (a reply to the author's own post) |

Every item has its text, author, follower count at the read, creation time decoded from the Snowflake ID (`(id >> 22) + 1288834974657` ms), parent link, counts, and flags: `in_dataset`, `is_thread_continuation` (from `boost_flags.csv`, `timeline_posts.csv`, or a reply to its own author), `boost_suspect` (None where no boost check ran), and `mature` (read 48 hours or more after posting).

`reposters.jsonl` is left out of the snapshot. Nothing here reads it, and it holds 16,000 strangers' bios.

### Rebuilding the local files

The repository is public, and the snapshot holds about 7,000 replies by individual X users with their handles and bios. So only `MANIFEST.json` is committed. These files stay local and gitignored:

| Path | Holds | Rebuilt from |
| --- | --- | --- |
| `data/corpus/raw/*.gz` | the snapshot | the context worker's raw reads |
| `data/labels/items.jsonl` | every item's text, author, bio and counts | the snapshot |
| `data/labels/rationales/*.jsonl` | the annotators' one-sentence reasons, which can quote an item | the local branch `sam/corpus` |

One command rebuilds all three and checks the snapshot against the committed hashes:

```sh
uv run python -m harness.corpus rebuild \
  --src "/Users/samgu/Programming Projects/8vcSocialMedia-worktrees/context-dynamics/data/context" \
  --rationales-from sam/corpus
```

The raw reads live in that `context-dynamics` worktree's `data/context/` (gitignored there too; the tracker that wrote beside them has finished). A copy of the snapshot, `items.jsonl` and the unfiltered LLM rows is in `~/.long-run/8vc-harness/evidence/b2/local-data/`. Omit `--src` to keep the snapshot already in place. Omit `--rationales-from` on a machine without `sam/corpus`; the store then shows empty rationales.

`sam/corpus` is the unfiltered local history: it commits the snapshot, `items.jsonl` and the rationales. Never push it. `sam/corpus-clean` is the same work with those files removed from every commit, the rationales blanked, and the TF-IDF word lists dropped from the old results files.

## Reply benchmark

**Rule.** A pair is two direct replies to the same dataset post (the root), where the winner has at least 5 likes and at least 3 times the loser's likes. Both replies must be mature, by someone other than the root's author, and have words after the leading @handles. The scorer has to say which reply got more likes.

**Why that margin.** If two replies were equally likeable and each like landed on either with probability 1/2, a 5 to 0 split happens by chance with p = 0.031 and 12 to 4 with p = 0.038, but 6 to 2 with p = 0.14. At larger counts the ratio rule is much stricter than any p-value cut, which matters because likes pile onto early replies. The baselines tell the same story at other margins:

| Margin | Test pairs | Test threads | delay | followers | length |
| --- | --- | --- | --- | --- | --- |
| 3+ likes, 2x | 1,575 | 43 | 0.734 | 0.595 | 0.539 |
| 5+ likes, 3x (used) | 1,332 | 40 | 0.755 | 0.607 | 0.550 |
| 10+ likes, 5x | 1,023 | 29 | 0.810 | 0.665 | 0.575 |

**Size.** 336 roots have a reply sample; 127 have at least one pair. A thread can supply over 1,000 pairs, so at most 50 per thread are kept, drawn with seed 20261016. That leaves 4,725 pairs over 3,580 replies.

**Splits.** Threads split by the root's posting date, and a thread never spans two splits. Weeks start at 00:00 UTC on 2026-09-15.

| Split | Weeks | Root dates | Threads | Pairs |
| --- | --- | --- | --- | --- |
| dev | 0 to 2 | Sep 15 to Oct 5 | 87 | 3,393 |
| test | 3 to 4 | Oct 6 to Oct 16 | 40 | 1,332 |

Rolling cutoffs fit on every thread before the cutoff and score the week after it: Sep 29 (dev, 27 threads), Oct 6 (test, 32 threads) and Oct 13 (test, 8 threads, because roots from the last two days are not mature). Every threshold and feature choice here was made on dev, with one exception noted under the ranker. Later batches can confirm a change only on threads newer than it. Week 4 is thin; once the tracker and reply samples cover later posts, rebuild with `python -m harness.corpus bench` and add cutoffs.

The splits keep threads apart, not accounts: 13 root accounts have threads in both dev and test. The rankers use no account identity, so this matters less than it would for a model with per-account terms.

`data/bench/manifest.json` records the seed, the rule, each thread's week, split and coverage, and the snapshot hashes the pairs came from.

**Confounds.**

- *Delay.* Replies that arrive early get seen first and liked more. Delay is a baseline, and the "delay within 2x" subset holds it roughly fixed.
- *Reply author's followers.* Larger accounts' replies get more likes. Followers is a baseline, and "followers within 3x" holds it roughly fixed.
- *Sampling.* fxtwitter returned the first page of replies sorted by likes and the first page sorted by recency, about 35 each, so in large threads the middle is missing and losers skew late. Coverage (sampled replies over the root's reply count) is at least 0.8 in 27 of the 40 test threads, reported separately.

**Baselines on test**, fitted on dev (direction only; time of day is a table of dev win rates per 4-hour UTC block). Pair accuracy with a 95% interval from resampling threads:

| Baseline | All (1,332 pairs, 40 threads) | Coverage at least 0.8 (760, 27) | Followers within 3x (342, 37) | Delay within 2x (278, 35) | Both matched (73, 22) |
| --- | --- | --- | --- | --- | --- |
| constant | 0.500 | 0.500 | 0.500 | 0.500 | 0.500 |
| delay | **0.755** [0.690, 0.815] | 0.766 [0.668, 0.841] | 0.763 [0.686, 0.837] | 0.518 [0.440, 0.602] | 0.534 [0.372, 0.745] |
| followers | 0.607 [0.536, 0.677] | 0.566 [0.473, 0.670] | 0.551 [0.480, 0.623] | 0.583 [0.500, 0.671] | 0.507 [0.421, 0.602] |
| media or link | 0.574 [0.539, 0.617] | 0.563 [0.517, 0.622] | 0.580 [0.537, 0.627] | 0.577 [0.527, 0.640] | 0.575 [0.518, 0.643] |
| verified | 0.560 [0.510, 0.601] | 0.533 [0.463, 0.597] | 0.534 [0.483, 0.578] | 0.583 [0.540, 0.625] | 0.596 [0.538, 0.667] |
| length | 0.549 [0.486, 0.611] | 0.552 [0.462, 0.642] | 0.507 [0.427, 0.584] | 0.504 [0.427, 0.577] | 0.390 [0.282, 0.512] |
| time of day | 0.529 [0.471, 0.593] | 0.529 [0.445, 0.614] | 0.529 [0.458, 0.606] | 0.500 [0.440, 0.560] | 0.569 [0.494, 0.660] |

A scorer beats the benchmark only if it beats delay on test, by a paired thread bootstrap. Once delay and followers are both matched, 73 pairs remain and nothing clears 0.6.

## Reply ranker

`harness/rank/reply.py` fits a pairwise logistic model within threads: P(a beats b) = sigmoid(w · (x_a - x_b)), no intercept, L2 strength chosen by 4-fold cross-validation over training threads. Feature blocks:

- **timing**: log minutes after the root, log1p followers, verified.
- **text**: length, emoji, question mark, extra @mentions, caps ratio, first person, all lowercase, word overlap with the root, link, media.
- **ngrams**: hashed word unigrams and bigrams.
- **labels**: LLM label fields, in `harness/label/DESIGN.md`.

`fit(before)` trains on threads rooted before a date; `benchmark(model, cutoff)` scores the week after it against a timing model trained on the same threads and refuses a model trained past the cutoff. `score(parent, text, minutes_after, followers)` ranks a draft.

Accuracy by cutoff:

| Cutoff (eval threads) | timing | text alone | timing + text | timing + text + ngrams | text added to timing |
| --- | --- | --- | --- | --- | --- |
| Sep 29, dev (27) | 0.843 | 0.631 | 0.865 | 0.780 | +0.023 [0.004, 0.045] |
| Oct 6, test (32) | 0.784 | 0.580 | 0.776 | 0.704 | -0.008 [-0.048, 0.038] |
| Oct 13, test (8) | 0.698 | 0.564 | 0.751 | 0.676 | +0.053 [0.000, 0.117] |
| Both test weeks (40) | 0.769 [0.717, 0.822] | 0.577 [0.514, 0.643] | 0.772 [0.720, 0.824] | 0.699 [0.637, 0.758] | **+0.002 [-0.034, 0.044]** |

The text block alone does better than a coin (0.577), but what it knows overlaps with timing: added to timing, it moves nothing. The largest text weights at the Oct 6 cutoff are media (+0.44) and overlap with the root (-0.30, replies that repeat the post lose).

The one choice not made on dev alone: the brief put hashed n-grams in the text block. That version lost to timing on all three cutoffs (pooled test -0.071 [-0.137, -0.001]). I then compared it on the dev cutoff with the n-grams removed (+0.023 against timing) and moved them to their own block. The test numbers above for the dense text block were computed after I had seen the n-gram version's test result, so treat its +0.002 as one look at test, not a clean held-out number. Both versions are reported. With 60 to 120 training threads, the n-grams learn thread topics.

`data/bench/results/replies.json` holds every number in this section. Rerun with `python -m harness.rank replies` (about 90 seconds).

## Post benchmark

**Rule.** Two of an account's own original posts in the window (Sep 15 to Oct 16), both mature, neither a thread continuation nor `boost_suspect`, where the winner has at least 10 engagements (likes, reposts, replies and quotes) and 3 times the loser's. Unlike the CSV, the timeline posts restore the 11 to 99 band. At most 50 pairs per account. Accounts split 50/50 by a seeded hash: 43 dev accounts (1,157 pairs) and 37 test accounts (1,016 pairs).

This side was meant to be a quick null check, because the repo's earlier work found no content feature beyond |rho| 0.14 within an account. It is not null.

| Scorer on test-author pairs | Accuracy |
| --- | --- |
| length | 0.602 [0.548, 0.661] |
| format logistic (media, quote, link, all lowercase, length, gap since the previous status), fitted on dev authors | 0.592 [0.526, 0.654] |
| TF-IDF ridge, cross-fitted by account | **0.712** [0.648, 0.779] |

TF-IDF minus length on the same test pairs: +0.120 [0.040, 0.205]. A logistic model on Claude's LLM labels does about as well, 0.730 [0.667, 0.783] on all 1,016 test pairs (`harness/label/DESIGN.md`). The ridge model mirrors `harness/virality.py` on `origin/aarav/self-improving` in pure Python (word 1 to 2-grams, character 3 to 5-grams, shape features, target log engagement z-scored within account, alpha 10, 5 folds by account). Its heaviest words: "introducing", "is out", "is now", "free" and "love" lift; "join", "rsvp", "talk", "week" and "in sf" sink. That is launches beating event promos, which matches the context worker's within-account finding for announcements.

Aarav reports 0.756 on 844 viral-against-deadweight pairs. On clean pairs of that kind (100+ against 10 or fewer, same filters as above) only 129 pairs from 12 accounts remain, and the ridge scores 0.581 [0.495, 0.773]. With thread continuations and immature posts put back, 4,785 pairs from 38 accounts score 0.664 [0.545, 0.771]. So I could not reproduce 0.756 on these files, and the version with easy pairs scores higher than the clean one, but both intervals are wide. On the broader 3x pairs the content signal holds at about 0.71.

`data/bench/results/posts.json` holds these numbers. Rerun with `python -m harness.rank posts`.

## Openness

`harness/rank/target.py` gives `openness(handle) -> {share_small_top3, median_top3_minutes, n}` for an account whose threads were sampled: across its threads, the share of the three most-liked replies written by accounts under 1,000 followers, and the median minutes after the root they arrived. Over 45 accounts the median share is 0.667. Cognition's top replies come from small accounts less often (0.42) and fast (median 14 minutes); Expo's arrive late (119 minutes). It has no validation yet.

## Scoring a new scorer

```python
from harness.corpus import load
from harness.corpus.bench import load_pairs
from harness.corpus.stats import accuracy, credits_for, paired_difference

corpus = load()
test = [p for p in load_pairs("replies") if p.split == "test"]
mine = credits_for(test, lambda sid: my_score(corpus.get(sid)))
delay = credits_for(test, lambda sid: -(corpus.get(sid).created_at - corpus.get(corpus.get(sid).thread_root).created_at).total_seconds())
print(accuracy(mine, [p.group for p in test]))
print(paired_difference(mine, delay, [p.group for p in test]))  # must clear 0 to count
```

## Not verified

- Like counts were read once, on Oct 16. Replies to roots from the last week had less time to settle; the 48-hour maturity rule is the context worker's, not measured here.
- The coverage split assumes fxtwitter's two pages are the top and the newest replies. I did not re-fetch any thread to check.
- Week 4 has 8 test threads, so its cutoff alone decides nothing.
- The post side splits by account but not by time.
- No scorer here has been checked on the fan account's own threads, which the tracker has not yet sampled.
