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

An instrument is anything the loop measures with. The eight in `eval/experiments/instruments.json`:

| Instrument | Scored on `_sgu_` replies | Scored on threads | Cutoff |
| --- | --- | --- | --- |
| reply ranker (`harness/rank/reply.py`) | its score against the rank percentile | reply pairs | the frozen benchmark's newest root: it trains on every frozen thread |
| TF-IDF post ranker (`harness/rank/post.py`) | none: `_sgu_` posts no originals | within-account post pairs | its newest training post |
| Claude label model | none | within-account post pairs, labeled by Sonnet 5.5 | its newest training post |
| lineup AI detector, the action lineup's primary | 1 when it names another reply, 0 when ours | the reply it names against each other reply in a lineup of five settled replies | when the ledger first saw its prompt |
| action veto, the action lineup's veto guardrail | pass or veto | discordant reply pairs | same |
| lane veto (`harness/minds/veto.py`), the reply lane's production veto | pass or veto | discordant reply pairs | same |
| slop gate (`harness/minds/gate.py`) | pass or fail | discordant reply pairs | same |
| panel (report-only by design) | none | within-account post pairs | same |

"The gate" in the brief is read as the slop gate, the deterministic screen beside the vetoes. The paired statistical gate in `harness/iterate/stats.py` decides promotions; it isn't an instrument that measures drafts, and the confirmation rule below is what guards its errors. "The action veto" is read as the veto inside the action lineup, because that is the one that gates promotions (its pass rate is a guardrail). The reply lane's own veto is scored too, as `lane_veto`, because it decides which drafts reach Sam; nothing in the engine reads its status, so it informs a person.

**Sources.** `_sgu_` results are Spearman correlations between the instrument's score for each real reply and that reply's rank percentile in its thread, from `data/outcomes.db`. Threads are pairwise accuracy on pairs rooted after the instrument's cutoff, from the frozen corpus and the folded watcher threads. Both sources count only outcomes at or after the cutoff: threads rooted then, `_sgu_` replies posted then. The two are kept apart. Every number carries n and a 95% bootstrap interval. Pairs resample by thread (or account), because pairs that share a thread are not independent, and n counts those resampling units, so fifty pairs from one thread are one outcome.

**Screens count only discordant pairs.** For the gate and the vetoes, a pair counts only when one reply passed and the other failed, and the question is whether the passing one got more likes. The pass rate on real replies sits beside it: a screen that fails real people's replies is costing the lane good drafts.

**Cutoffs.** A fitted model's cutoff is the newest root it trained on. The reply ranker now trains on the whole frozen benchmark, so only watcher threads judge it: B2's test split was looked at once while choosing its feature blocks, and its 0.772 on that split is kept as `fit_evidence` but never decides status. A prompt or rule instrument's cutoff is the moment the ledger first saw its current version, because nothing records which data a person looked at while tuning a prompt. The version is a hash of what defines the instrument: prompt and model, rule source and limits, or training data and feature code. A new version starts a new cutoff, and the old version's numbers stay in its history. When `sam/wire` changes the lane veto, its evidence starts over.

**The rule.** An instrument may gate promotions while its interval is above chance or it has fewer than 20 outcomes. At 20 or more, an interval that includes chance, or sits below it, makes it report-only, and the ledger appends an event with the time and the reason. Real `_sgu_` results decide first; the threads decide only while `_sgu_` has fewer than 20, or when its interval is undefined (a screen passes every `_sgu_` reply, since each one shipped through it, so `_sgu_` can't judge a screen). The status follows the evidence, so an instrument whose interval later clears chance gates again, which is also logged. The panel is report-only whatever its numbers. Twenty is the counsel's estimate of what confirms or demotes an instrument; no run here calibrated it.

**What a status does.** `RULERS` in `scheduler.py` names the instruments each fitness measures with. When the action lineup's primary, the lineup detector, is report-only, every action screen, confirmation and stack check is held. When its veto guardrail's instrument, the action veto, is report-only, the veto pass rate is still recorded but no longer blocks; the record lists it under `guardrails_dropped` with the reason. The GPT-family detector stays a guardrail whatever happens, per B3's guard table, and isn't scored here.

**Abstentions and failures.** An answer that names nothing usable is an abstention. It scores as the instrument's worst rank (for the lineup, naming the most-liked reply) and lowers coverage, which is reported beside the number. Only answers are cached. A failed call, a spent budget, or a `_sgu_` reply whose thread the watcher hasn't read yet waits for the next cycle and is counted as deferred, not as an abstention.

## From a spec to the champion

An agent or a person writes `eval/experiments/queue/<id>.toml`. `queue.py`'s docstring has the format. The parser refuses, with the reason, anything it can't run as stated:

- unknown keys, so a typo can't drop a guardrail;
- a fitness that scores a model's opinion, or doesn't declare whether it does (no LLM opinion score is ever a fitness);
- knobs the instrument can't measure, or values outside their domain;
- a guardrail margin looser than the instrument's own;
- a code change whose commit isn't in the checkout the loop runs from. A code change runs through the knob that switches it on, because the engine compares configs it can hash, diff and revert.

The spec then goes through two phases, both recorded in the ledger with the spec's id and a digest of what it tests (fitness, profile, knobs, guardrails, commit, thread files):

1. **Screen** on the fixture threads the spec names plus up to 40 more clusters rooted closest before the cutoff. A pass under the paired gate makes the record a `candidate`, which never touches the champion.
2. **Confirm** on up to the first 30 clusters rooted after the cutoff, once there are at least 20 (threads or accounts). A pass here is a promotion, subject to the weekly cap.

Each phase uses as many clusters, up to its cap, as its call budget covers, so its cost stops growing while the watcher keeps adding threads. A confirmation that can't afford 20 clusters waits.

The cutoff is the later of `proposed_at` and when the loop first saw this version of the file, so a backdated spec can't confirm on items its author had seen, and a `proposed_at` in the future is refused. Editing what a spec tests changes its digest, and the edited spec is screened from scratch rather than confirmed on the old screen. Before either phase the loop sends every model it needs (the detectors, the veto, the model knobs) a five-token request, and waits if one refuses: on 2026-10-16 the GPT detector's pool answered 429, and a screen would have spent its budget on retries. It also skips a screen the ledger already decided on the same items.

A candidate that waits more than 21 days, a phase that can't fit its budget for that long, or a phase that fails to run three times goes to "needs a person". So does every refused spec file.

**Stack checks.** Before any queued phase, the loop measures any combination of promotions that no run scored together (`campaign.unmeasured`, in `eval/experiments/DESIGN.md`), on up to 30 of the newest clusters its budget covers. When a check fails, the loop reverts the later promotion only if the loop made it (a queue confirmation or an engine confirmation) and the champion still holds its values. A promotion from a run a person started, a revert, or a value that has changed since goes to "needs a person" in that day's report. A failed check whose revert a crash lost is finished at the start of the next cycle.

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

## The first live cycles

On 2026-10-16 the loop ran from this worktree against the platform watcher's files (`--threads-dir`, `--watch-dir`, `--hot-dir`, `--outcomes-db`), on the real gateway.

| Cycle | Started (UTC) | What happened |
| --- | --- | --- |
| 1 | 14:51 | Facts: 216 for wnba from ESPN; 310 for devtools from 19 accounts, 5 left out because fxtwitter answered 404. Fold: no watcher thread has a +48h read yet. Instruments: none has an outcome after its cutoff. The `styled-reply` screen started and hung for over 20 minutes; the process was killed. |
| 2 | 15:24 | Marked cycle 1's experiments step interrupted and ran it again. The screen finished in 4 minutes on 280 calls plus 4 pre-flight probes. |
| 3 | 15:29 | Facts, fold and the decided spec were all skipped by key: 0 calls. |

The screen compared the styled prompt with the persona call on 20 WNBA threads (18 fixture threads and 2 of the watcher's):

| Metric | Persona call | Styled prompt | Difference, 95% CI |
| --- | --- | --- | --- |
| Opus 5.5 detection (primary, lower is better) | 0.95 | 0.80 | −0.15 [−0.30, −0.00] |
| GPT detector (guardrail) | 0.75 | 0.30 | −0.45 [−0.65, −0.25] |
| Veto pass rate (guardrail) | 0.65 | 0.85 | +0.20 [−0.05, +0.45] |
| Reply length, characters | 215 | 67 | −148 |

BH passed it (p = 0.044), but its lower bound, +0.000, didn't clear the noise floor of 0.05 between the champion and its repeat, so the record is inconclusive and the spec is done. More threads would decide it: the persona call is found almost every time, and the styled prompt still four times in five.

Three defects came out of the live runs, each fixed with a test. The devtools facts refresh died on the first unreadable account (13:58 run). Every GPT model on the gateway answered 429 `usage_limit_reached` in probes at 13:50, 13:58 and 14:03, and answered again by 14:52, so the loop now probes a phase's models before spending its budget; it deferred the screen twice. And cycle 1's hang came from compounding retries: `MeteredLLM` retried four times and the SDK twice more inside each attempt, with backoff sleeps, against a GPT pool still answering some calls with 429. Cycle 2 ran on the fixed client and logged 40 such retries on the GPT detector without stalling.

## Not verified

- No `_sgu_` reply has been ranked yet, so every `_sgu_` scorer has run only on synthetic outcomes in the tests.
- No watcher thread has had a +48h read yet. The fold, and every instrument scored on watcher threads, has run only on synthetic threads. Nothing has been demoted or held live.
- The label model's live labeling call (`effort: low` through `extra_body`) has not run in this loop; only its fit on stored labels has.
- The confirmation phase, the weekly cap, stack checks and instrument demotion have run only in tests: the one live screen was inconclusive, and no instrument has outcomes yet.
- The long-running mode ran live for one wait and a SIGTERM; no scheduled six-hour cycle, and no morning report at 11:00 UTC, has run unattended.
- The 24-hour reply window, the 20-outcome threshold, the 30 and 40 cluster caps, 21 days to stale and the 6-hour cycle are policy.
