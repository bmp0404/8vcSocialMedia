# 8VC Media Harness

An agentic harness that recommends X actions for a Caitlin Clark fan account, built for the 8VC ATX
build session (Oct 15, 2026). It recommends actions and doesn't post anything; 8VC's Grok bot decides what
gets posted.

## The agent loop

The agent's goal is to improve its recommendations over repeated rounds: start from the mission
and remembered lessons, observe what is happening, propose actions, evaluate them, and carry
what worked into the next round. Four subsystems own that loop:

```text
memory -> observe -> action -> eval -> memory -> next round
```

| Subsystem | Responsibility | Current implementation | Stage files |
| --- | --- | --- | --- |
| **Observe** | Collect signals and turn them into relevant narratives for the account. | `harness/sources/`, `harness/orient.py` | `observation/` |
| **Action** | Use the mission, narratives, and remembered lessons to draft candidate actions. | `harness/minds/` | `action/` |
| **Eval** | Score and critique candidates before review; compare predictions with recorded engagement after execution. | `harness/qualify.py`, `harness/learn.py` | `eval/` |
| **Memory** | Store run history and outcomes, retain lessons, and make them available to the next round. | `harness/memory.py`, `harness/learn.py` | `memory/`, `data/harness.db` |

Memory is shared context throughout the loop. Observe, action, and eval should contribute evidence
that helps the next round make better decisions. `data/` supports all four subsystems with datasets
and persisted records; it is not a separate decision stage. The observe subsystem currently uses
the directory name `observation/`.

### Integration layer: `engine/`

`engine/` is the integration layer for connecting observe, action, eval, and memory into one
agent loop. Its responsibility is to coordinate stage order, pass outputs between subsystems,
and feed evaluation results and remembered lessons into the next round.

The folder is scaffolded for this work. Current CLI orchestration still lives in `harness/cli.py`;
continuous orchestration in `engine/` remains to be implemented.

### Run one round, then close the loop

1. Pick a mission profile with `HARNESS_PROFILE` (`wnba` by default; see `mission/profiles/`), edit its
   `mission.md`, and read its lessons in `memory/lessons/<profile>.md`.
2. Run `python -m harness run --provider mock --sources sample` to observe sample signals,
   generate candidates, score them, and save the run and ranked recommendations.
3. Run `python -m harness review` to approve, edit, or kill candidates. Approved actions are
   exported to `out/approved-run<id>.json` for the external posting bot.
4. Once real engagement is available, run `python -m harness record <outcomes.json>` to store
   outcomes for the corresponding action IDs.
5. Run `python -m harness learn` with a configured provider to update lessons from those outcomes,
   then start another round. The action subsystem reads the updated lessons.

Today these are explicit CLI steps, not an unattended continuous agent. Automatic outcome collection,
review-feedback learning, and continuous orchestration remain work to implement. Mock runs exercise
the pipeline; they do not establish real engagement performance. The harness recommends actions;
8VC's Grok bot owns posting.

## Quickstart

```powershell
uv sync
copy .env.example .env          # then fill in a provider + key
python -m harness run --provider mock   # offline smoke test, no key needed
python -m harness run                    # real run with the provider in .env
```

## Commands

| Command | What it does |
| --- | --- |
| `run` | Observe, orient, decide, and qualify, then write ranked recommendations to `out/` |
| `record <file>` | Store real metrics: `[{"action_id": "...", "metrics": {"likes": 3}}]` |
| `learn` | Rewrite `memory/lessons/<profile>.md` from reviews and outcomes; the minds read it next run |
| `review` | Open the approve/edit/kill dashboard at `http://127.0.0.1:8765`; every verdict rewrites `out/approved-run<id>.json` |

### Review dashboard

`python -m harness review` lists each run's recommendations, best predicted first, with tabs for
To review / Approved / Killed / Critic vetoed. You can edit a draft before approving it (the original
text is kept as `original_text` in the export), override a critic veto, and leave a note.
Keyboard: `j`/`k` move, `a` approve, `x` kill, `u` reset, `e` edit.

`out/approved-run<id>.json` is the hand-off file for the posting bot.

## Layout

Python code lives in `harness/`. The four subsystems keep non-code assets in the following
directories, with `data/` shared across the loop and `engine/` reserved for integration:

```text
engine/      # Integration and orchestration connecting all four subsystems
observation/  # Inputs and observations collected by the harness
action/       # Actions and execution workflows
eval/         # Evaluations, scoring, and validation
memory/       # Persistent context and learned information
data/         # Source documents, datasets, and supporting assets
```


| Path | Stage |
| --- | --- |
| `mission/profiles/<name>/` | One brand: `mission.md`, `observe.toml`, `panel.json`, `reply_framing.json`, `prompts.toml`; `HARNESS_PROFILE` picks it |
| `harness/sources/` | **Observe.** One plugin per data source; register it in `sources/__init__.py` |
| `harness/orient.py` | **Orient.** Clusters signals into narratives scored on relevance × velocity |
| `harness/minds/prompts/*.md` | **Decide.** One persona per file; add a file to add a mind |
| `harness/qualify.py` | **Qualify.** Simulated audience panel scores drafts; a critic vetoes |
| `harness/memory.py` | SQLite log of runs, recommendations, and outcomes (`data/harness.db`) |
| `harness/learn.py` | **Learn.** Writes `memory/lessons/<profile>.md` |
| `harness/review/` | **Human checkpoint.** Stdlib HTTP server + single-page dashboard |
| `harness/llm.py` | Provider switch: `anthropic`, `xai`, `openai` (any OpenAI-compatible gateway), `mock` |

## Status / TODO

- [ ] `sources/x_search.py`: wire up live X data (Grok live search, X API, or Playwright)
- [ ] `sources/reddit.py`: needs Reddit OAuth (unauthenticated requests return 403)
- [ ] Replace `mission/profiles/wnba/mission.md` with 8VC's mission file
- [ ] Hand-off format/transport to 8VC's Grok bot
- [x] Human approve/kill checkpoint (`python -m harness review`)
- [ ] Feed review verdicts and notes into `learn`
- [ ] Pull real metrics automatically instead of `record`

## Working on the harness

Start with [CONTRIBUTING.md](CONTRIBUTING.md) for subsystem ownership, file placement, shared
contracts, and the branch/PR workflow. Coding agents must also read [AGENTS.md](AGENTS.md) and
[CLAUDE.md](CLAUDE.md). Coordinate changes to shared interfaces with the other subsystem owners
and keep the offline mock run passing.
