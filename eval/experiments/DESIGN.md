# Self-iteration: how the harness tests changes to itself

`harness/iterate/` lets the harness change its own configuration under rules. A proposer suggests a change to one or two registered knobs. The runner scores the current configuration (the champion) and the changed one (the challenger) on the same items. A paired statistical gate keeps the change only if it clearly helps, and every comparison becomes a record in this folder, kept even when it lost.

This page is for teammates deciding what to build next. It explains how the loop works, what it found on the night of 2026-10-15, how it differs from Aarav's `aarav/self-improving`, and what each stage has to expose before the loop can tune it.

## What it found tonight

Eight comparisons ran in five runs, using 1,045 metered model calls. The champion is back at the defaults, because the one promotion failed its retest.

| Record | Change | Items | Champion | Challenger | Difference, 95% CI | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| `action-fbb933` | persona JSON → continuation | 17 threads | detected 0.941 | 0.118 | −0.824 [−1.00, −0.65] | superseded |
| `action-f691d2` | persona JSON → continuation | 17 threads | detected 0.941 | 0.353 | −0.588 [−0.82, −0.29] | inconclusive |
| `action-7ea3e0` | persona JSON → sam/action reply lane | 27 threads | detected 1.000 | 0.370 | −0.630 [−0.81, −0.44] | promoted, then reverted |
| `action-d71d3a` | the same, veto v3 | 27 threads | veto pass 0.556 | 0.259 | −0.296 [−0.56, −0.04] | inconclusive, revert recorded |
| `qualify-752ef2` | persona panel → single reader | 144 posts, 27 accounts | AUC 0.794 | 0.854 | +0.060 [+0.007, +0.113] | inconclusive |
| `qualify-2d1cf7` | memory off → on | 144 posts | AUC 0.794 | 0.823 | +0.029 [+0.003, +0.056] | inconclusive |
| `qualify-f178ed` | memory off → shuffled labels | 144 posts | AUC 0.794 | 0.823 | +0.029 [−0.008, +0.064] | inconclusive |
| `qualify-f89252` | all drafts in one call → one post per call | 144 posts | AUC 0.794 | 0.797 | +0.003 [−0.065, +0.059] | inconclusive |

The action detection rate is the share of real threads where Opus 5.5 picked the generated reply out of a lineup of five. Chance is 0.2, and lower is better. The qualify metric is the within-account AUC of viral against Deadweight posts, where higher is better.

- **Continuation reads human, but plain continuation isn't safe.** The first run promoted continuation under a gate that was too loose (see "The gate" below). The record stays, marked superseded with its original decision. Under the corrected gate, continuation still cut detection from 0.94 to 0.35, but a veto guardrail blocked it. Its vetoed drafts included "I refuse to listen to anything that woman has to say".
- **The reply lane passed, then failed a stricter veto.** sam/action's reply lane (continuation, slop gate, two-order Opus veto) cut detection from 1.00 to 0.37. A GPT-family detector that never saw the Opus veto agreed (1.00 to 0.41). The veto pass rate rose from 0.56 to 0.96, so the lane was promoted. Its drafts then showed what that veto missed: "Let's go Angel Reese, another championship 🏆" on a Caitlin Clark fan account, and a bare "...". Veto v3 adds a mission-fit check and a content-free check. Re-judged on the same drafts, the lane passed 0.26 against 0.56. The confirmation failed and the loop recorded an automatic revert. Of the lane's 20 v3 vetoes, 12 were for speaking as a fan of the other team and 10 for saying nothing specific (a draft can have both reasons). Continuing another team's thread makes the model talk like that team's fans.
- **The single reader beat the persona panel, but not by more than the noise.** +0.060 AUC, BH-adjusted p 0.031, the direction the research predicted (research/eval.md, finding 8). Its lower bound (+0.007) doesn't clear the 0.018 noise floor, so the champion stands. More accounts would settle it.
- **The memory gain came from seeing older posts, not from their outcomes.** Showing the panel an account's five most recent older posts with their outcomes raised AUC by 0.029. Shuffling those outcome labels raised it by exactly the same 0.029. A memory that only helps when its labels are wrong isn't adding knowledge, so neither arm should ship as "memory helps".
- **Batching costs nothing once orders are averaged.** Production qualify scores a run's drafts in one call, in two orders, averaged. One post per call scored the same (+0.003). The 0.12 AUC order effect in research/eval.md, finding 1, came from single-order batches.
- **Viral against viral is unreadable on this sample.** It is the content question once the account prior is gone (research/context.md). Computed offline from the saved scores, its intervals span most of [0, 1], because few accounts have both Super and Medium Viral posts in the sample. It is a reported secondary from now on, not a guardrail. As a guardrail it would block every qualify change, and the champion sits near chance on it anyway.

## How the loop works

1. **Propose.** `RuleProposer` or `LLMProposer` (`harness/iterate/propose.py`) suggests changes. Either way, `validate` drops anything outside the registry.
2. **Run arms.** `run_batch` (`harness/iterate/runner.py`) scores the champion, a repeat of the champion, and each challenger on the same items, under a hard call cap.
3. **Gate.** `harness/iterate/stats.py` decides each comparison. All comparisons in a run form one family for multiple-comparison correction.
4. **Apply.** The champion is the registry defaults plus every promoted change, replayed in order from the records.
5. **Record.** Each comparison, pending or decided, becomes `exp-*.json` in the shape agreed with the UI worker. `CHANGELOG.md` and `champion.json` are regenerated after every write.

### Knobs and configs

`REGISTRY` in `harness/iterate/knobs.py` lists 34 knobs. Each knob has a type, a default taken from today's code, its allowed values, the stage that owns it, and `reader`, the file that reads it. Three knobs have no reader yet, so the loop can test them but a promotion changes nothing live. Several readers exist only on worker branches, and `reader` names the branch.

A `Config` assigns a value to every knob. Its identity is the SHA-256 of its canonical JSON (sorted keys, no whitespace, sets stored sorted), and its ref is `cfg-` plus the first 12 hex digits. Adding a knob changes every hash. Records keep the refs they were written with. The champion is rebuilt from knob changes rather than refs, so it survives registry growth. For example, the first action runs name the default config `cfg-17e3b75195e4`, and later runs name it `cfg-35c9ee1e8e13`.

### Fitness instruments

An instrument is the measuring side of an experiment. Its settings aren't knobs. Proposers can't touch them, and each record stores them in `eval.instrument`, so a promotion can never come from changing the ruler.

- `action_lineup` (`harness/iterate/fitness/action.py`). For each real thread, the config's generator writes one reply from the root and 12 real replies (ranks 7 to 18). Opus 5.5 then sees four real top replies plus the generated one, in a seeded order, and names the AI reply. A GPT-family model judges the same lineup as an independent check. A frozen veto judges each draft, and its pass rate is a guardrail: a challenger must be shown no more than 10 points worse. The 10 points is policy, not measurement. An arm that ships nothing for a thread counts as detected and as passing the veto. `persona_json`, the current default, is frozen in `fixtures/persona_json-main.json`, so the knob value keeps one meaning after sam/action replaces main's prompt.
- `qualify_auc` (`harness/iterate/fitness/qualify.py`). The eval worker's protocol sample and headline metric, imported from `harness/evals/`. Arms the eval worker already scored on the same sample are imported from its report, and the record says so. The champion repeat always runs live.

Two instruments the brief asked for aren't built:

- Observe time-travel recall. `harness eval observe` on sam/eval replays orient at cutoffs, but it scores one config per call and reports totals. The loop needs per-story hits for two configs on the same cutoffs.
- A memory ablation through the memory worker's `harness.learning.ranking.round_start(mode=...)`. `qualify.memory` is a stand-in. It retrieves the account's most recent older posts by date, not through the memory store.

### The gate

Each step exists because something measured tonight, or in the research, showed it was needed.

1. **Paired cluster bootstrap.** Both metrics are recomputed on the same resampled set of whole accounts or threads. Pairing cancels item difficulty, and resampling whole clusters respects the fact that one account's posts aren't independent.
2. **Benjamini-Hochberg at q = 0.1 across the run** (research/memory.md, finding 1). Every extra comparison raises the bar for all of them, so runs should test few, research-motivated changes.
3. **A one-sided lower bound at level 1 − min(Rq/m, 0.025),** where R is the number of BH rejections and m the family size. This is the Benjamini-Yekutieli false-coverage adjustment, floored so it is never looser than the paired 95% interval the eval worker requires to exclude zero. With m = 1, Rq/m alone gives a 90% bound, and that is what promoted continuation in the superseded first run.
4. **A noise floor.** The bound must exceed |repeat − champion| on the same items. The repeat draws new lineups and new seeds, because a judge's verdict moves with lineup composition (research/action.md, finding 3).
5. **Guardrails need non-inferiority.** "Not significantly worse" passes any challenger on a small sample. A guardrail that can't be computed blocks promotion. So does a challenger that failed on noticeably more items than the champion.
6. **At most one promotion per fitness per run.** Two winners were never measured together. Ties break on the bound, then the p-value, then the config hash, so proposal order can't decide.
7. **Confirmations.** A retest against the champion a promotion replaced reverts that promotion automatically if it fails.

An independent reviewer in a fresh context checked these functions against brute-force versions and seeded broken variants. Items 5 and 6, a float tolerance in BH, and five stronger tests came from that review. `tests/test_iterate_stats.py` and `tests/test_iterate_loop.py` hold the hand-worked cases.

### Proposers

`RuleProposer` tries research priors first (`harness/iterate/plans.py`, each with its source), then one-knob neighbours of the champion. `LLMProposer` shows a model the registry, the ledger summary and those findings, and asks for at most three experiments as JSON. Both reject proposals that:

- name an unknown fitness or knob, or a value outside the domain,
- change a knob the fitness can't measure,
- equal the champion,
- or repeat a change the ledger already decided against an equivalent champion on the same dataset.

A grown dataset reopens a settled experiment, because more items is how an inconclusive result gets the power to decide. Growing an instrument is a person's call, since it changes the ruler.

The one live LLM call proposed the GPT panel (research/eval.md, finding 14), and a stricter reply-lane veto with eight samples. Both sit in `proposals/20261016T023414-llm.json`. The second targeted the promotion that was later reverted.

### The ledger

| Path | What it holds |
| --- | --- |
| `exp-*.json` | One experiment: change, hypothesis, proposer, both config hashes, metrics with intervals, noise floor, correction, failures, decision, cost |
| `CHANGELOG.md`, `champion.json` | Regenerated from the records; don't edit them |
| `configs/cfg-*.json` | Every config a run used, by ref |
| `runs/run-*.json` | One run: call cap, calls by arm, the BH family |
| `reports/run-*-<fitness>.json` | Every draft, lineup pick, veto reason and panel score behind a run's numbers |
| `proposals/*.json` | What each proposer suggested and why each rejection happened |
| `fixtures/` | The persona prompt snapshot and 18 threads fetched read-only through fxtwitter |

Mock runs write to `out/iterate/`, which git ignores, and never import real cached scores.

## How this differs from aarav/self-improving

Aarav's branch improves the harness from online rewards. Reviews count with weight 1 and real outcomes with weight 3 (`harness/reward.py:16-18`). `bandit.py:37` gives more drafting turns to prompt variants with a higher posterior mean. `evolve.py:95` has an LLM rewrite the best prompt into a challenger and retires the weakest. `evolve.py:41` reweights the panel by correlation once five outcomes exist. `evolve.py:159` has an LLM rewrite `memory/lessons.md`.

| | `aarav/self-improving` | `harness/iterate` |
| --- | --- | --- |
| Signal | Online rewards: reviews and real metrics | Offline paired tests on fixed items |
| What changes | Prompt text, panel weights, the lessons document | Registered knobs only |
| When a change is kept | Highest posterior mean or mean reward | Paired gate, BH, noise floor, guardrails |
| Record | Variants table and leaderboard | One JSON record per comparison, losers included |

Two things make the online route weak tonight. The account has two followers, so real outcomes barely exist, and a review reward measures the reviewer's taste. And ACE (arXiv 2510.04618) found that letting an LLM rewrite a whole lesson document can collapse it below the no-memory baseline (research/memory.md, finding 4).

His pieces fit this loop as proposers or allocators:

- **`mutate_and_retire`** (`evolve.py:95`) becomes a prompt-variant proposer. The rewritten prompt becomes a new, content-hashed value of a mind-prompt knob, and `action_lineup` tests it before it ever drafts live. Keep his instruction to change one or two specific things.
- **`spawn_mind`** (`evolve.py:130`) works the same way, for membership in `action.minds`.
- **`calibrate_panel`** (`evolve.py:41`) becomes a fitted knob. Fit weights on half the accounts and test on the rest with `qualify_auc`.
- **`bandit.choose`** (`bandit.py:37`) isn't a proposer. It belongs after the gate, to split live traffic among configs that already passed offline, once real outcomes exist.
- **`write_lessons`** goes. The memory worker's tested lessons replace it.

## What I ruled out

- **An online bandit as the acceptance test.** There is no traffic to learn from.
- **Free-form code edits from the proposer.** A knob change can be validated, diffed, hashed and reverted. A code edit can't be checked the same way.
- **Comparing marginal intervals.** Records show each arm's interval for reading only. Decisions use the paired difference.
- **Skipping the champion repeat to save calls.** It is the only measure of run-to-run noise, and it set the floor that held back the single reader.

## Gaps and open questions

- **No instrument measures whether a reply is worth posting.** v3 catches side-taking and empty lines, not whether anyone would care.
- **The v3 retest re-judged saved drafts.** It isolates the veto change, but it doesn't re-measure drafting variance.
- **Promotions on different fitnesses are applied together without being measured together.** The next run's champion arm is the first measurement of the combination.
- **q = 0.1, the 10-point veto margin and the failure slack are policy, with no calibration behind them.**
- For Sam: should a confirmation under a stricter instrument be required before any action promotion counts? Tonight it was required by hand.
- For Sam: should the qualify instrument grow past 27 accounts so the single reader can be decided? That needs the data team's backfill.

## What other stages need to expose

- **Action.** Build `ActionConfig` from the champion: `action.samples`, `action.page_replies`, `action.veto.*` and the generator choice. Add a mind-prompt knob so prompt variants can be tested. On the v3 finding, frame the continuation page from the mission's side rather than as one more reply from the other team's fans.
- **Qualify and eval.** Read `qualify.panel`, `qualify.model` and `qualify.memory` from the champion instead of `HARNESS_PANEL` and `HARNESS_PANEL_MODEL`. Return per-story hits from `harness eval observe` so recall can be paired.
- **Memory.** Accept `memory.recall` in decide (`round_start(mode=...)`), so `action_lineup` can run the off, on and shuffled ablation through the real store.
- **Observe.** Read the reply-target size band and maximum age from a config object.

## Run it

```sh
uv run python -m harness iterate knobs                       # every knob, its default and its reader
uv run python -m harness iterate run --plan qualify          # mock by default; --provider anthropic for real
uv run python -m harness iterate propose --llm               # next experiments; add --run to run them
uv run python -m harness iterate champion
uv run python -m harness iterate revert <experiment id> --reason "..."
uv run python -m pytest tests/test_iterate_*.py
```

`action_lineup` needs `action/fixtures/x_threads.json`, which lands with sam/action. Until then, pass `--threads`. The reply-lane arm needs sam/action's code merged.
