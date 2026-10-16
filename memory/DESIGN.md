# Memory: reflection, review learning and new ingest sources

This covers the `sam/memory` part of the memory layer. Krish's store (`harness/memstore/`) is described in [README.md](README.md) and specified in [IMPLEMENTATION.md](IMPLEMENTATION.md). Rushi's per-round log format is in [format/](format/README.md). Everything below writes into Krish's store through one adapter and never stores notes of its own.

## What memory did before this branch

- `harness/memory.py` kept one review row per action. `set_review` overwrote it with `INSERT OR REPLACE` (line 107 on `main`) and deleted it on a reset to pending (line 104). A verdict that flipped, or the draft a human edited, was gone after the next click.
- `harness/learn.py:23-29` sent `scored_history()` to an LLM and wrote whatever came back to `memory/lessons.md`. Nothing checked a lesson against data, and every lesson reached every mind, because `cli.py:25` passes `config.lessons()` (the whole file) to `decide`.
- The dataset (`data/devtools_virality_30d.csv`), the context worker's features and the brand's own pages never reached memory.

## Who else is working on memory

| Person or worker | Branch | What it covers | How this branch relates |
| --- | --- | --- | --- |
| Krish | `krish/memory` | Markdown store, keyword search, `context_for`, `prompt_block`, `round_end` episodes from SQLite | All writes here go through his `write_note`. `round_end` owns `ep-<action_id>` episodes; this branch never writes them. |
| Rushi | `main` (`memory/format/`) | JSON log per stage per round, with X API v2 objects | Episodes use the same v2 metric keys (`like_count`, `reply_count`) and ids (`tweet_id`, `loop_id`). |
| Aarav | `aarav/self-improving` (unmerged) | `reward.py`, `evolve.py`, `variants` and `kv` tables, an LLM-rewritten `lessons.md` and a playbook | Overlaps on `learn.py`, `memory.py` and `lessons.md`. See the proposal below. |
| Context worker | `sam/context-dynamics` | `boost_flags.csv`, `devtools_enriched.csv` (within-account labels, `pre_*` features) | Attached to dataset episodes by `Post URL`. |
| Eval worker | `sam/eval` | Fault verdicts, outcome bands | Episodes carry the decision fields Eval asked memory to keep. |
| Dhilan | `engine/` | The round loop | Calls `round_start` at round start and `learn` at round end. |

Aarav's `reward.py` scores reviews as approved 1.0, approved after an edit 0.7, killed 0.0. This branch labels the same verdicts as success or failure, which is his reward cut at 0.5. Binary labels are what the held-out test below needs, and the edit itself is kept separately as a preference pair instead of being folded into 0.7.

## Proposal for Sam: one writer of lessons

Three things write lessons today: this branch's reflection job, Aarav's `evolve.py` (an LLM rewrite of `lessons.md`), and Krish's store, which holds lesson notes. I propose:

1. Lesson notes have one writer, the reflection job, through Krish's `write_note`. Every lesson cites episodes and passed a held-out test.
2. `memory/lessons.md` is a view, rendered from active lessons by `learn`. Krish's `config.lessons()` change (lessons.md plus `prompt_block()`) keeps working.
3. `evolve.py` keeps prompt evolution and the playbook. When it has a lesson idea, it hands it to `reflect` as a hypothesis instead of writing `lessons.md`.

The reason is evidence about whole-document rewrites. The ACE paper (arXiv 2510.04618) reports a Dynamic Cheatsheet memory that shrank from 18,282 tokens at 66.7% accuracy to 122 tokens at 57.1%, below its 63.7% no-memory baseline, after repeated LLM rewrites. An itemized list where code applies votes, as ExpeL does, avoids that collapse. This is a proposal; nothing in this branch blocks Aarav's version from merging.

## Data model

Each kind is a dataclass in `harness/learning/kinds.py` that maps onto one of Krish's six note types.

| Kind | Note type and id | What it holds |
| --- | --- | --- |
| `Episode` | episode, `post-<tweet id>` (dataset) or `ep-<action id>` (ours) | The post or draft, what the agent saw (`observation`), how the draft was made and picked (`decision`: mind, provider, siblings, `draft_meta`, `chosen_by_bot`), peer posts on the same story (`peers`), and one `Outcome` per source |
| `Reflection` | reflection, `refl-<hash>` | A claim about why cited episodes share an outcome, the contrasting episodes, the predicate and optional rule that tested it, the test result, and caveats |
| `Lesson` | lesson, `lesson-<hash>` | Advice, its predicate or rule, the effect from the latest supporting test, evidence ids, vote counts (support, contradict, inconclusive), `valid_until`, scope, caveats |
| `Entity` | entity, `entity-<slug>` | An account, team or brand profile with stats |
| `Fact` | fact, `fact-<hash of subject and question>` | One answer about the brand, its verbatim quote and source URL, `as_of`, `valid_until`, `supersedes` |
| `Procedure` | procedure, `procedure-review-edits-<mind>` | Every edit reviewers made to one mind's drafts: the draft, the kept text, the verdict, the note and a word diff |

An episode keeps outcomes from different sources apart because they disagree and have different owners. `Episode.label()` picks the highest-priority labeled one: real metrics, then the human verdict, then the dataset category, then the model's own prediction.

Krish's frontmatter parser returns every value as a string except `confidence`, and splits lists on commas. The adapter (`harness/learning/memstore_adapter.py`) therefore writes the keys his store reads in plain form (`evidence`, `confidence`, `source`, `valid_until`, `tags`, and `quote` on facts) and the whole record as one base64url JSON value under `record`. Reading decodes only `record`. `tests/test_learning_kinds.py` round-trips every kind through the adapter and fails on any lost or changed field. It also runs against `harness.memstore` itself once that package is on the branch.

## Reflection

`harness/learning/reflect.py` asks why posts that share an outcome share it, and keeps an answer only if it holds on posts it never saw.

1. It drops thread continuations (a post within 3 minutes of the same account's previous one), posts under 48 hours old, posts the context worker marks not `label_eligible`, and successes flagged `boost_suspect`. It refuses to run without boost flags unless told `--allow-unfiltered`.
2. It groups posts into strata by account and by launch versus non-launch. Launch means the keyword rule in `dataset.py` or the context worker's `pre_hook_announce`. Each stratum with at least 2 failures and 2 successes gives half of each side to a held-out set.
3. From the other half it builds batches of strata and asks the proposer (Sonnet) what the failures share that the successes from the same account lack, and the reverse. The model may answer that nothing is shared. Each hypothesis cites post ids, gives a yes/no predicate, and gives a rule when the trait is a computed feature (`media`, `n_chars`, `has_link`, text keywords, any `pre_*` context feature).
4. Citations that don't point at a post with the claimed outcome in that batch are dropped. A hypothesis left with fewer than 2 evidence ids or no contrast id is discarded.
5. Rules are checked by code. Other predicates go to a different model (Haiku), which sees held-out posts without their outcomes.
6. `stats.py` runs a one-sided Cochran-Mantel-Haenszel test across strata, and Benjamini-Hochberg at q=0.1 across every test in the run. A supported hypothesis becomes a lesson. A hypothesis that restates an existing lesson votes on it, and the vote tests the lesson's own predicate. A retest that misses significance is inconclusive and changes nothing; a contradiction needs the opposite direction to be significant.

Why this shape:

- Labels in the CSV are raw engagement cutoffs (Deadweight is 0 to 10, Medium Viral 100 to 749, Super Viral 753 and up, nothing in between). Account identity alone separates viral from Deadweight at AUC 0.906 (research report, `8vc-context/research/memory.md`). Without strata every reflection concludes "small account". HypoGeniC (Zhou et al. 2024) uses tweet pairs matched on author for the same reason.
- My first version held out one pair per account and used a sign test. The red-team simulation showed real keyword effects passed only 12% to 41% of draws at that size, and that counting non-significant retests as contradictions retired real lessons. The stratified test uses every held-out post.
- Reflexion (Shinn et al. 2023) works because an outside signal grades the attempt; Huang et al. (ICLR 2024) found self-correction without one makes reasoning worse. Here the outside signal is engagement or a human verdict, and the critic's own veto never labels an episode.
- D5 (Zhong et al. 2023) proposes on one split and judges on another. ExpeL (Zhao et al. 2024) contrasts failed with successful attempts and votes on insights. Generative Agents (Park et al. 2023) stores reflections with pointers to the memories they cite.
- An LLM labeler can make weak hypotheses look significant (Baumann et al., arXiv 2509.08825), so code-checkable rules are preferred, and the labeler is a different model from the proposer.

### What it found on the dev-tool dataset

All runs used Sonnet 5.5 to propose and Haiku 5.5 to label, two batches of four strata, three posts per side.

| Run | Filters and label | Held-out posts (strata) | Supported | Inconclusive |
| --- | --- | --- | --- | --- |
| Seed 0 | No boost flags, Category label | 144 (18) | 2 of 8 | 6 |
| Seed 1, same store | No boost flags, Category label | 141 (18) | 4 of 9, plus 2 votes | 5 |
| Seed 0 | Boost flags and `label_eligible`, Category label | 43 (6) | 0 of 8 | 8 |
| Seed 0 | Boost flags, within-account label (`label_above_author_prior`) | 196 (35) | 0 of 6 | 6 |

After the two unfiltered runs the store held four lessons. Retesting all four with boost flags on:

| Lesson | Category label, 42 posts in 6 strata | Within-account label, 192 posts in 34 strata |
| --- | --- | --- |
| Lead with the concrete news and its specific detail | supported, 13 of 21 successes vs 6 of 21 failures | inconclusive, p=0.56 |
| State the news in the text instead of only pointing to a link | supported, 13 of 21 failures vs 6 of 21 successes | inconclusive, p=0.40 |
| Attach a video or image to link posts | supported, 14 of 21 failures vs 8 of 21 successes (p=0.051) | inconclusive, p=0.071 |
| Attach a short demo video | inconclusive, p=0.28 | inconclusive, p=0.043 |

My read: media and "say the news in the post" have the most consistent support, but nothing here is established. Once account size, threads, young labels and boosts are controlled, the held-out sets are small, and the within-account label (which compares a post with its author's earlier median) supports none of them under the false discovery rate. That matches Tan, Lee and Pang (2014), where people picked the more-retweeted of two same-author posts 61.3% of the time. "Lead with the concrete news" carries a launch-confound caveat, because it describes announcements. Expect few real lessons from this dataset; the job is built to say so rather than produce some anyway.

I hand-checked the labeler on 20 held-out posts for two predicates ("bare link with no explanation" and "announces a new product or feature"). Haiku agreed with my labels on 19 of 20 for each; both misses were posts I had marked as borderline.

## Review verdicts as learning signal

`set_review` now appends every verdict to `review_events`, in the same transaction as the old `reviews` write, so the dashboard keeps working:

```sql
CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY, action_id TEXT, run_id INTEGER, status TEXT,
    original_text TEXT, edited_text TEXT, note TEXT, reviewed_at TEXT, reasons TEXT
);
```

`set_review(action_id, status, text=None, note="", reasons=None)` takes kill reason codes from `memory.REVIEW_REASONS` (`off_message`, `factually_shaky`, `stale_story`, `wrong_target`, `slop`, `tone`, `other`) and stores them as a JSON list; an unknown code raises `ValueError`. The table is new, so existing `data/harness.db` files get it on the next connect.

`harness/learning/reviews.py` folds the events into one summary per action: the latest verdict, how many times it flipped, the notes, the reasons, and each distinct edit. `learn` then writes one procedure note per mind with every edit pair, labels approved and killed drafts as episodes for reflection (grouped by narrative, so drafts are compared on the same story), and renders `memory/lessons.md`. `review_events` stays the source of truth; procedure notes are rebuilt from it with stable ids on every run.

## New ingest sources

`python -m harness ingest-dataset` writes the 5 highest- and lowest-engagement posts of each Category as episodes (Krish asked for exemplars, not 1,665 notes) and one entity profile per account. It joins the context worker's CSVs by `Post URL`. Post times come from the snowflake id, which is how thread continuations and label age are computed. The rest of the table reaches memory through reflection, and reflection writes every post it cites as an episode so `[[links]]` resolve and the store's lesson-evidence check passes.

`python -m harness ingest-brand <url or file> --name <brand>` fetches the brand's pages, honoring `robots.txt` and reading `llms.txt` first when it exists. It asks eight fixed questions plus follow-ups the model writes for that brand, and keeps an answer only if its quote appears verbatim on the cited page and every number, date and name in the answer appears in the quote. Facts are keyed by subject and question, so a changed answer supersedes the old fact. On resend.com it kept 14 facts from 3 pages and dropped 2 whose answers claimed more than their quotes.

## Ranking at round start

`harness/learning/ranking.py` supplies the ranking Krish's `context_for` can call, and `round_start(narratives, mode=None)` is the entry point for `engine/`. For each narrative it takes the store's search hits plus every active lesson, then scores each memory as relevance plus trust plus recency, each scaled to [0, 1], as in Generative Agents. Trust is a lesson's effect, or a fixed value per kind. Lessons past `valid_until`, retired lessons, rejected reflections, superseded facts and other domains' episodes are left out. Episodes are capped at one success and one failure, so a mind sees a contrast. Lessons are few and abstract, so every active one is a candidate and keyword relevance only reorders them; ExpeL likewise puts its whole insight list in the prompt.

`mode` is the ablation switch: `off` returns nothing, and `shuffled` returns the same number of picks per kind drawn at random from memories that pass the filters. The `observe` part of the result lists lessons and facts due for a recheck.

I kept keyword search (BM25 in Krish's store) and ruled out a vector database at this size. BEIR (Thakur et al. 2021) found BM25 a robust zero-shot baseline, and on its one tweet task it beat every 2021 dense model, though that task's labels were gathered with keyword systems. The one ranking factor with local evidence is `other_author_factor`. `python -m harness exemplar-check` predicts a held-out post's outcome from its 10 keyword neighbors. Across 5 random 400-post splits, neighbors from the same account were right 85.3% of the time against 79.8% for the account's own majority label on the same posts, while neighbors from other accounts were right 64.9% against 78.7%. So the brand's own past posts are useful examples and other accounts' posts much less so. The other weights are unvalidated starting points.

## Tunables

Every knob lives in `harness/learning/settings.py` as `MemorySettings` with sections `reflect`, `recall` and `ingest`. A `memory/settings.json` file overrides any field, and an unknown key raises. `python -m harness memory-settings` prints the values in effect. The settings that matter most for a tuning loop are `recall.mode`, `recall.budget_chars`, `recall.home_domains`, `reflect.fdr_q`, `reflect.label_source` and `reflect.min_new_labeled`.

## Ruled out

- Writing all 1,665 dataset rows as notes. They would flood keyword search, and Krish's store is meant for distilled material.
- An LLM that rewrites the lesson file whole, for the ACE result above.
- Pooled tests across accounts. The research report's shuffle test found about 80% of what a pooled test accepted was account size.
- Critic vetoes as labels. That is the model grading itself.
- A sixth store or index. Retrieval is Krish's search plus this branch's ranking.

## Open questions

1. Which account is tonight's brand? `recall.home_domains` defaults to `["brand"]`, so dev-tool lessons stay out of the fan account's prompts. A dev-tool brand should set `["brand", "devtools"]`.
2. Who owns lessons (proposal above)?
3. Does Krish's `dump` and `parse` keep `record` byte for byte? The adapter test will say once `harness.memstore` lands.
4. Which label should reflection default to? The Category label gives cleaner contrasts but few comparable posts; the within-account label gives four times as many and supports nothing yet.
5. `reflect.min_new_labeled` (50) and `valid_days` (30) are guesses with no evidence behind them.
6. For our own posts, what counts as success? `run_episodes` labels real metrics only when Eval records `"outcome": "success"` or `"failure"` with them.
7. Krish assigned the Action worker's abstain logs (`out/decisions-*.json`) to this branch. They aren't ingested yet.
8. A revalidation records its verdict but not which label source it used, so votes under different labels mix in one lesson's counts.

## What this branch adds

| Path | Purpose |
| --- | --- |
| `harness/learning/kinds.py` | The data model and its mapping to note types |
| `harness/learning/store.py`, `memstore_adapter.py` | The store interface, a test fake, and the adapter to Krish's store |
| `harness/learning/dataset.py` | Dataset rows to episodes, context joins, exemplars, account profiles |
| `harness/learning/brand.py` | Brand page ingest with quote and claim checks |
| `harness/learning/reflect.py`, `stats.py` | Reflection, the held-out test, votes and revalidation |
| `harness/learning/reviews.py`, `harness/learn.py` | Review events to labels, procedures and `lessons.md` |
| `harness/learning/ranking.py`, `views.py`, `settings.py` | Round-start ranking, read views for the UI, and every knob |
| `harness/learning/commands.py` | `ingest-dataset`, `ingest-brand`, `reflect`, `revalidate`, `exemplar-check`, `memory-settings` |
| `harness/memory.py` | The `review_events` table, `review_history()` and `run_log()` |
