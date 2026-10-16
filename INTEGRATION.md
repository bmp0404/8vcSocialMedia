# Integrating Sam's stages

Readers: Sam's lead and the stage owners, deciding what merges next and which stage to fix next. This file records how `sam/integration` was assembled, how the other teams' branches would merge into it, and what tonight's live rounds measured.

## Merge order

`sam/integration` is `main` plus the branches below, merged in this order with `git merge --no-ff`. Observe (PR #10) and the shared pytest setup (PR #8) are already on `main`, so they aren't merged separately.

| # | Branch | Head | Why this position | Conflicts and resolution |
| --- | --- | --- | --- | --- |
| 1 | `sam/context-dynamics` | `7f1c759` | Observe's `x_targets` and `attention` sources import `harness.context.xreader` and `harness.context.collectors.attention`. Without this branch both raise and `observe()` skips them. | `pyproject.toml` and `uv.lock`: took `main`'s side, then `uv add "tzdata>=2026.5"`. |
| 2 | `sam/eval` | `b156368` | Changes `qualify.py`, which every run calls. Its `cli.py` block is additive. | None. |
| 3 | `sam/action` | `8045583` | Replaces decide's lanes. Reads Observe's `Narrative.reply_targets` and `Signal.thread`. | `models.py`: both sides add optional fields (`Signal.kind`, `thread`, `Narrative.velocity_basis`, `reply_targets` from Observe; `Action.lane`, `target_text`, `draft_meta` from Action). Git merged them cleanly. |
| 4 | `sam/memory` | `0144360` | Adds `review_events` to the `memory.py` schema and memory subcommands. | `cli.py`: kept both additive blocks, eval's `register_evals(sub)` and memory's `add_commands(sub)`. |
| 5 | `sam/iterate` | see `git log` | Knob registry and promotion gate; merges `sam/eval` itself. | `cli.py`: another additive block, kept with the others. |
| 6 | `sam/ui` | local, unpushed | Console and trace views in `harness/review/`. Additive. | None. |

Rules used for every merge:

- `pyproject.toml` and `uv.lock`: take one side, then re-run `uv add` for each dependency the other side added. `uv.lock` is never merged by hand or by git's text merge, even when git reports no conflict.
- `models.py` and `cli.py`: every change is additive, so both sides are kept.
- The integration branch carries seam fixes as its own commits, listed under "Seam fixes" below. Each one also went to the owner.

## How the other teams' branches would merge

Checked with `git merge-tree` against `sam/integration`.

| Branch | Textual result | What actually collides |
| --- | --- | --- |
| `origin/aarav/self-improving` (`910c98b`) | 12 conflicting files: `cli.py`, `learn.py`, `llm.py`, `memory.py`, `minds/__init__.py`, `minds/prompts/reply_sniper.md`, `orient.py`, `qualify.py`, `sources/__init__.py`, `memory/lessons.md`, `pyproject.toml`, `uv.lock` | Aarav retargets the harness to Coordigent and rewrites decide around an action catalog and tournament. Action's lanes replace the same `decide()` internals, so `minds/__init__.py` is a design conflict, not a text conflict: one decide has to win. `learn.py` and `memory/lessons.md` conflict the way the memory research predicted: Aarav's `learn.py` has an LLM rewrite the lessons file, Memory's renders it from tested lessons only. The memory research recommends the tested version (ACE's collapse result). `orient.py`: Observe's budgeted payload supersedes Aarav's truncation fix for the same bug. `memory.py`: both append tables to `SCHEMA`; keep both. `sources/__init__.py`: both add sources to `REGISTRY`; keep both. `llm.py`: keep `HARNESS_MAX_TOKENS` from this branch, and take Aarav's `TruncatedError` and one JSON retry, which this branch lacks. `pyproject.toml`: he adds `scikit-learn`. |
| `origin/dhilan/version-1` (`bf26dce`, judge PR #5) | Clean | Adds `baml-py`, so regenerate `uv.lock` with `uv add`. Once `harness/post_judge` is present and `baml generate --directory eval` has run, Action's `veto.grounding_checker()` turns on Dhilan's grounding score as a second veto. Without it the run prints "post_judge unavailable" and continues. |
| `origin/dhilan/engine` (`eed0bc0`, PR #9) | Clean | See "Plugging into the engine". |
| `origin/krish/memory` (`2e427e8`) | Clean | Only the memory spec, already on `main`. No store code is pushed, so nothing calls `context_for` or `round_end` yet. |

### Plugging into the engine

Dhilan's `engine/runner.py` takes each stage as a field of `Components` (`observe`, `orient`, `decide`, `evaluate`, `save_run`, `read_memory`, `finish_memory`). The `sam/*` stages fit those signatures unchanged: `observe(names, mission)`, `orient(llm, mission, signals, top)`, `decide(llm, mission, lessons, narratives, per_mind, vetoed)` returning `list[Action]`, and `qualify` through `engine.evaluation.panel_evaluate`. Three seams need code in `engine/`:

1. **Decision log.** Action's `decide` holds every decision in `lanes.PENDING` until `write_decision_log(run_id)` writes `out/decisions-run<id>.json`. `cli.run` calls it after `memory.save_run`; `run_round` doesn't. Without the call the engine loses every abstention and rejected draft, and `Budget.from_logs` never sees engine rounds, so the daily caps don't hold across engine rounds.
2. **Memory at round start.** Memory's hook is `harness.learning.ranking.round_start(narratives, mode=None)`. `Components.read_memory` currently returns `config.lessons()`, which skips it.
3. **Reference evaluation vetoes follows.** `reference_evaluate` vetoes every action that isn't a post, reply or quote, so the follows Action's post lane ships never survive it.

## Hand-off formats

There are three, and none is agreed with 8VC yet.

- `out/approved-run<id>.json`: the dashboard's export, `{run_id, actions: [...]}`, approved actions only, with human edits applied. `harness/review/__init__.py:export_approved`.
- `out/grok-handoff-run<id>.json` and `.md`: written by `harness/integration/handoff.py`. Each recommendation carries the target URL, action type, text, rationale, a `send_before` time for replies, and its evidence (narrative, sources, the target post's live counts, decide's veto evidence, qualify's verdict, and the integration checks). Actions that fail a check are listed under `held` with the reason.
- Aarav's CSV (`harness/export.py` on `aarav/self-improving`): `out/coordigent-suggested-posts.csv`, one row per public draft with run, rank, status, type, text, reply target, panel score and critic note. His `ideas` command appends de-duplicated posts to `out/ideas.csv`.

## Integration tooling

`harness/integration/` is new and owned by this branch.

- `live.py` runs `harness run` unchanged and records the signals, narratives, decide and qualify calls, and decision logs that `cli.run` keeps only in memory, in `out/integration/run<id>.json`.
- `measure.py` audits a recorded round. It re-reads every reply target through fxtwitter, runs the Opus 5.5 AI-register lineup from `research/action.md`, asks Opus 5.5 per draft (one draft per call) for P(AI), a targeted insult (X's `FosnrAbuseInsults`), engagement bait (`SpamHighRecall`) and which claims tonight's sources support, and reruns the gate's grounding rules with the round's evidence as context.
- `handoff.py` writes the hand-off files above and approves the ready actions in the review table with the note "approved by the integration checks, not yet by a person".

## Operating notes

- Poll `x_targets` no more often than every 5 minutes. fxtwitter's 1 request per second is shared across every process on this machine through `~/.cache/media-harness/throttle/`, so one `x_targets` run took 66 seconds while Context's tracker was polling.
- `harness/llm.py` now caps output at `HARNESS_MAX_TOKENS`, default 16,000 (see "Seam fixes"). This is a shared-contract change.
- Keep real runs small; every reply target costs one planning call, six continuation samples on `claude-opus-4-5-20251101` and two Opus 5.5 veto passes.

<!-- LIVE-RESULTS -->
