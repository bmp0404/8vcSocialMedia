# Memory

Memory is how the harness gets better from one round to the next. At the start of a round it reads what it has learned. At the end of the round it writes down what happened. There is one store for the one harness, aimed at dev tooling startups. There are no per-user or per-brand stores.

The design follows the memory system behind Instinct, as reverse-engineered by supermemory: plain markdown files, keyword search instead of embeddings, and a strict split between the stages that read memory and the one step that writes it.

Status: this describes the plan. The code is being built on the `krish/memory` branch in `harness/memstore/`. [IMPLEMENTATION.md](IMPLEMENTATION.md) has the exact spec and the work packages. Krish owns the store; ask before changing its format.

## Where memory sits in the loop

```text
round start   memory -> prompt_block() + context_for(narrative)  (read)
              observe -> orient -> decide -> qualify -> review -> record
round end     round_end(run_id) -> episode notes                 (write)
```

The drafting stages (observe, orient, the minds, qualify) only read memory. They never write it, so a bad draft mid-round can't corrupt what the harness knows. Writes happen at the end of the round through one validated path.

## The store

Memory is a folder of markdown notes, one folder per type:

```text
memory/
  seed/          # tracked in git: starter notes, and good state promoted by hand
  store/         # gitignored: the live store, copied from seed/ on first use
    entities/    # accounts, competitors, narratives the harness tracks
    facts/       # facts about the brand, each with a verbatim source quote
    lessons/     # what worked and what flopped; tags mark patterns and calibration
    procedures/  # rules: voice, safety, things review taught us never to do
    episodes/    # one note per action or dataset post: the raw evidence
    reflections/ # why a group of episodes shared an outcome
  format/        # JSON log format (raw input for ingest, not memory itself)
```

The live store is gitignored so that five branches don't fight over generated files, and so that mock runs never pollute it. Each mission profile has its own store, `memory/store/<profile>/`, seeded from `memory/seed/<profile>/`; the current seed is dev-tools, so it lives in `memory/seed/devtools/`. Set `MEMSTORE_PATH` to put the stores in a different folder; each profile's store is a subfolder of it, as under `memory/store/`. To share good state, promote it into the profile's seed folder and commit that. For a demo, run the whole loop on one machine.

The SQLite database (`data/harness.db`) and the JSON logs in `memory/format/` are the raw record of every run. The markdown store is the distilled version that the harness actually reads.

## Note format

Each note is a markdown file with frontmatter. The body is one fact per bullet, each with a date, and `[[id]]` links to related notes. This example is made up to show the shape; it is not a finding from the data.

```markdown
---
id: launch-posts-with-demo-video
type: lesson
title: Launch posts with a demo video outperform text-only launches
aliases: [launch, ship, release, GA, demo, video]
created: 2026-10-15
updated: 2026-10-15
evidence: [post-1845532211, post-1846010042]
confidence: 0.6
tags: [pattern]
---
- **Finding:** Super Viral launch posts in the dataset mostly carry a short demo video; stated on 2026-10-15.
- Related: [[ai-coding-agents]]
```

Required keys: `id`, `type`, `title`, `aliases`, `created`, `updated`.
Optional keys: `evidence`, `confidence`, `source`, `supersedes`, `valid_until`, `tags`. The parser keeps keys it doesn't know, so other branches can add fields.

Ids are kebab-case (`[a-z0-9-]`, at most 80 characters), unique across the store, and never renamed. Episodes use `ep-<action_id>` for the harness's own actions and `post-<status_id>` for dataset posts, and must set `source` to `run` or `dataset`.

Aliases matter more than anything else in the note. Search only finds words that are spelled out, so a note about launches should list `ship`, `release` and `GA` too.

## API

All of this lives in `harness/memstore/`.

| Function | What it does |
| --- | --- |
| `load(id)` | Returns one note, or `None` |
| `list_notes(type=None)` | Lists notes, optionally of one type |
| `search(query, k=10, types=None)` | Ranked keyword search; returns id, type, title, score and matching lines |
| `context_for(text, k=5, types=None)` | The top matching notes for a narrative, formatted for a prompt |
| `prompt_block()` | All procedures plus confident lessons, capped; returns `""` if the store is empty |
| `write_note(note)` / `delete_note(id)` | The only way to change memory; validates every write |
| `round_end(run_id, *, provider)` | Turns a run's actions into episode notes; no LLM call, safe to rerun |

`write_note` rejects a note with a missing field, a duplicate id, or a path outside the type folders. It also rejects a lesson unless its evidence cites an episode backed by something real: a dataset post, a recorded outcome, or a human review verdict. A lesson resting only on the panel's own predicted scores is refused, so the harness can't learn from itself.

Each note loads as a `Note(id, type, title, aliases, created, updated, body, meta, path)`, where `meta` holds every optional key.

The minds get memory through `config.lessons()`, which returns the profile's `memory/lessons/<profile>.md` plus `prompt_block()`. That keeps `decide()` unchanged.

Raw scraped text is never stored. Notes about narratives hold a title, a summary the harness wrote, and URLs.

## Who builds what

| Area | Owner | Where |
| --- | --- | --- |
| Store, format, search, `round_end` | Krish | `harness/memstore/`, `memory/seed/` |
| Dataset posts, review verdicts and edits, brand facts, reflections, lessons, `learn.py` | Sam (`sam/memory`) | `harness/learning/`, `harness/learn.py` |
| Panel calibration | Sam (`sam/eval`) | `harness/evals/` |
| Calling memory at round start and end | Dhilan (`engine/`) | `engine/` |
| JSON log format | Rushi | `memory/format/` |

Everyone writes memory through `write_note`. Nobody commits files under `memory/store/`.

## Not built yet

These are deliberately left out of the first version: a generated one-pager summary, fuzzy matching, a link-graph tool, git history inside the store, and a shared synonyms file.
