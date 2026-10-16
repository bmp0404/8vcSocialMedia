# The improvement loop

This is for whoever runs `harness improve`, writes an experiment for it, or decides what to build next from its morning report. It answers four questions. What does one cycle do, and why can it run unattended? When may an instrument decide a promotion? How does an experiment get from a spec file to the champion? What did the first live cycle find?

## Why it exists

The pieces of a self-improving platform existed on `sam/platform`: benchmarks the data labels itself, learned rankers, a watcher that collects fresh threads every day, outcome sync for `_sgu_`, and an experiment engine with a paired gate. Nothing ran them on a schedule. An engine run by hand also keeps reusing one frozen test set, and anything chosen while looking at that set will overfit it. The loop runs every piece on a timer, scores each instrument only on data that arrived after the instrument was fitted, and promotes a change only after it holds on items posted after the change was proposed.

## One cycle

`python -m harness improve` runs a cycle every 6 hours until stopped. Each cycle runs five steps in order:

| Step | Runs | What it does |
| --- | --- | --- |
| facts | once a day per profile | `python -m harness.facts refresh --profile <p>` in its own process |
| fold | when `data/threads/` changes | turns the watcher's +48h thread reads into rolling benchmark pairs |
| instruments | every cycle | scores the seven instruments and updates `eval/experiments/instruments.json` |
| experiments | every cycle | runs any due stack check, then each queued spec's next phase |
| report | once a day, from 11:00 UTC | writes `out/improve/report-<date>.md` |

Outcome sync already runs inside the watcher every 30 minutes, so the loop reads `data/outcomes.db` and never syncs.

**Restarts.** Every step has a key in `out/improve/state.db`: the profile and date for facts, the size and modification time of the watcher's files for the fold, the cycle for instruments and experiments, the date for the report. A key marked done is skipped, so a restart repeats no finished step. Two things make the per-cycle steps cheap to repeat. Every LLM verdict an instrument gets is cached per instrument version in `out/improve/cache/`, so no item is judged twice. And a spec's phase is read from the experiment ledger, never from the state file, so a crash between writing a record and marking the step done can't run the phase again. A failed step is recorded with its error, retried next cycle, and listed in the report; the other steps still run.

**Process.** The loop writes its pid to `out/improve/improve.pid` and refuses to start while that pid is alive. SIGTERM or SIGINT stops it after the step in flight; an experiment run can take several minutes. `harness improve stop` sends SIGTERM and waits. On start, the next cycle is due 6 hours after the last one finished, not immediately. A mock run (`--provider mock`) keeps its state, ledger, instrument file and reports under `out/improve/mock/`, so it can never mark the live loop's facts or report done, and its numbers never reach `eval/experiments/`.

## The rolling reply benchmark

`fold.py` turns threads whose +48h read has landed into reply pairs under the frozen benchmark's rule (winner has 5 or more likes and 3 times the loser's), filed by the week the root went out, in `data/bench/rolling/`. That directory is gitignored because its pairs point at replies only `data/threads/` holds.

Two choices differ from the frozen benchmark.

- **Maturity.** The frozen rule counts a reply only when it was read 48 hours or more after the reply itself went out. A +48h read of the root would fail nearly every reply on that rule, because replies arrive minutes to hours after the root. A rolling thread counts once its root was read at 48 hours or more, and a reply counts if it came in the root's first 24 hours, so every counted reply had at least a day of likes. The 24 hours is policy, with no measurement behind it, and rolling pairs are reported apart from frozen ones.
- **No leakage.** A thread joins only if its root is later than every root in the frozen benchmark and no earlier than every fitted ranker's training cutoff. Anything else is refused with the reason, which the report shows. A folded item therefore can't land in a week a ranker trained on, and a later fit that wants rolling items has to name a cutoff, as `harness.rank.reply.fit(before=...)` already does.

## The instrument ledger

An instrument is anything the loop measures with. The seven in `eval/experiments/instruments.json`:

| Instrument | Scored on `_sgu_` replies | Scored on threads | Cutoff |
| --- | --- | --- | --- |
| reply ranker (`harness/rank/reply.py`) | its score against the rank percentile | reply pairs | its training cutoff, 2026-10-06 |
| TF-IDF post ranker (`harness/rank/post.py`) | none: `_sgu_` posts no originals | within-account post pairs | its newest training post |
| Claude label model | none | within-account post pairs, labeled by Sonnet 5.5 | its newest training post |
| lineup AI detector | 1 when it names another reply, 0 when ours | the reply it names against each other reply in a lineup of five | when the ledger first saw its prompt |
| action veto (`harness/minds/veto.py`) | pass or veto | discordant reply pairs | same |
| slop gate (`harness/minds/gate.py`) | pass or fail | discordant reply pairs | same |
| panel (report-only) | none | within-account post pairs | same |

"The gate" in the brief is read as the slop gate, the deterministic screen beside the veto. The paired statistical gate in `harness/iterate/stats.py` decides promotions; it isn't an instrument that measures drafts, and the confirmation rule below is what guards its errors.

**Sources.** `_sgu_` results are Spearman correlations between the instrument's score for each real reply and that reply's rank percentile in its thread, from `data/outcomes.db`. Threads are pairwise accuracy on pairs rooted after the instrument's cutoff, from the frozen corpus and the folded watcher threads. The two are kept apart. Every number carries n and a 95% bootstrap interval. Pairs resample by thread (or account), because pairs that share a thread are not independent, and n counts those resampling units, so fifty pairs from one thread are one outcome.

**Screens count only discordant pairs.** For the gate and the veto, a pair counts only when one reply passed and the other failed, and the question is whether the passing one got more likes. The pass rate on real replies sits beside it: a screen that fails real people's replies is costing the lane good drafts.

**Cutoffs.** A fitted model's cutoff is the newest root it trained on. A prompt or rule instrument's cutoff is the moment the ledger first saw its current version, because nothing records which data a person looked at while tuning a prompt. The version is a hash of what defines the instrument: prompt and model, rule source and limits, or training cutoff and feature code. A new version starts a new cutoff, and the old version's numbers stay in its history. When `sam/wire` changes the veto, the veto's evidence starts over.

**The rule.** An instrument may gate promotions while its interval is above chance or it has fewer than 20 outcomes. At 20 or more, an interval that includes chance, or sits below it, makes it report-only, and the ledger appends an event with the time and the reason. Real `_sgu_` results decide first; the threads decide only while `_sgu_` has fewer than 20. The status follows the evidence, so an instrument whose interval later clears chance gates again, which is also logged. The panel is report-only whatever its numbers. Twenty is the counsel's estimate of what confirms or demotes an instrument; no run here calibrated it.

**Abstentions** score as the instrument's worst rank and lower its coverage, which is reported beside the number. When a cycle's call budget runs out, unjudged items wait for the next cycle instead of counting as abstentions.

## From a spec to the champion

An agent or a person writes `eval/experiments/queue/<id>.toml`. `queue.py`'s docstring has the format. The parser refuses, with the reason, anything it can't run as stated:

- unknown keys, so a typo can't drop a guardrail;
- a fitness that scores a model's opinion, or doesn't declare whether it does (no LLM opinion score is ever a fitness);
- knobs the instrument can't measure, or values outside their domain;
- a guardrail margin looser than the instrument's own;
- a code change whose commit isn't in the checkout the loop runs from. A code change runs through the knob that switches it on, because the engine compares configs it can hash, diff and revert.

The spec then goes through two phases, both recorded in the ledger with the spec's id:

1. **Screen** on the instrument's items rooted at or before the cutoff. A pass under the paired gate makes the record a `candidate`, which never touches the champion.
2. **Confirm** on items rooted after the cutoff, once there are 20 clusters of them (threads or accounts). The cutoff is the later of `proposed_at` and when the loop first saw the file, so a backdated spec can't confirm on items its author had seen. A pass here is a promotion, subject to the weekly cap.

A candidate that waits more than 21 days for confirmation items, or a phase that fails to run three times, goes to "needs a person". So does every refused spec file.

The action lineup instrument confirms on the watcher's threads, which `ActionLineup(extra_threads=...)` now accepts beside fixture files. The qualify instrument's items are the dataset's posts, all from before any proposal, so a qualify candidate can't be confirmed until fresh posts get tier labels. That gap is listed below.

The engine changes behind this (the weekly cap, the confirmation filter, stack checks) are in `eval/experiments/DESIGN.md`.

## Guards

B3's guard table, and where each guard lives:

| Failure | Guard |
| --- | --- |
| The same fixture is tested again and again | screens never promote; confirmation only on later items; one promotion per rolling 7 days |
| The writer drifts toward the judge's register | the queue refuses LLM-opinion fitnesses; the GPT-family lineup detector is a guardrail on every action promotion (10-point margin, policy) |
| A config wins by abstaining | the lineup counts an abstention as detected; abstain rate and coverage are reported beside detection |
| An instrument is wrong and nobody notices | the instrument ledger and its demotion rule |

## Commands

```sh
HARNESS_PROVIDER=anthropic uv run python -m harness improve       # every 6 h until stopped
uv run python -m harness improve --once --report --provider anthropic  # one cycle now, with today's report
uv run python -m harness improve status                           # running or not, the latest steps
uv run python -m harness improve stop                             # SIGTERM, then wait for the step in flight
```

`--threads-dir`, `--watch-dir`, `--hot-dir` and `--outcomes-db` point the loop at another checkout's watcher. `--instrument-calls` and `--experiment-calls` cap LLM calls per cycle (300 and 800 by default).

## Not verified

- No `_sgu_` reply has been ranked yet, so every `_sgu_` scorer has run only on synthetic outcomes in the tests.
- No watcher thread has had a +48h read yet. The fold, and every instrument scored on watcher threads, has run only on synthetic threads.
- The label model's live labeling call (`effort: low` through `extra_body`) has not run in this loop; only its fit on stored labels has.
- The 24-hour reply window, the 20-outcome threshold, 21 days to stale and the 6-hour cycle are policy.
