# Memory implementation spec

This is the build spec for `harness/memstore/`. `memory/README.md` explains the design; this file pins every detail an implementer would otherwise guess. If the two disagree, this file wins. Ask Krish before changing anything under "Frozen contract".

## Work packages

The work splits into five packages with no shared files. P0 lands first and alone. P1 to P4 then run in parallel, each on its own branch off `krish/memory`, and merge back into `krish/memory` in the order P1, P2, P3, P4.

| Package | Branch | Owns | Depends on |
| --- | --- | --- | --- |
| P0 core | `krish/memory-core` | `harness/memstore/__init__.py`, `note.py` | nothing |
| P1 store | `krish/memory-store` | `store.py`, the `.gitignore` line | P0 |
| P2 search | `krish/memory-search` | `search.py`, `prompt.py` | P0 (reads files itself, so it doesn't wait for P1) |
| P3 seed and wiring | `krish/memory-seed` | `memory/seed/**`, `harness/config.py` (`lessons()` only) | P0 |
| P4 round end | `krish/memory-episodes` | `episodes.py`, `__main__.py` | P0, P1 (stub `write_note` until P1 merges) |

Every package must keep `uv run python -m harness run --provider mock` passing. Nobody edits `harness/models.py`, `harness/memory.py`, `harness/cli.py`, `harness/minds/` or `harness/qualify.py`. Before P3 touches `harness/config.py`, message Sam's session (Sam owns `learn.py` and `lessons.md`).

Each module exposes a `selftest_<module>()` function with plain asserts. P4's `__main__.py` calls them all. There is no pytest; it would add a dependency and `uv.lock` churn across branches.

## Frozen contract

### Types and folders

```python
TYPES = {
    "entity": "entities",       # accounts, competitors, narratives
    "fact": "facts",            # brand facts; meta.quote must appear verbatim in meta.source
    "lesson": "lessons",        # what worked or flopped; tags mark pattern / calibration
    "procedure": "procedures",  # rules: voice, safety, never-do-this
    "episode": "episodes",      # one action or dataset post
    "reflection": "reflections" # why a group of episodes shared an outcome
}
```

A note lives at `<store>/<folder>/<id>.md`. Any other type is an error.

### Note

```python
@dataclass
class Note:
    id: str
    type: str
    title: str
    aliases: list[str]
    created: str            # YYYY-MM-DD
    updated: str            # YYYY-MM-DD
    body: str
    meta: dict = field(default_factory=dict)   # every optional or unknown key
    path: Path | None = None

def parse(text: str, path: Path | None = None) -> Note
def dump(note: Note) -> str
```

### Public API (`harness/memstore/__init__.py`)

```python
load(id: str) -> Note | None
list_notes(type: str | None = None) -> list[Note]
write_note(note: Note) -> Path            # the only write path; raises MemoryValidationError
delete_note(id: str) -> bool
search(query: str, k: int = 10, types: list[str] | None = None) -> list[dict]
    # [{"id", "type", "title", "score", "snippets": [str]}]
context_for(text: str, k: int = 5, types: list[str] | None = None) -> str
prompt_block(max_chars: int = 6000) -> str
round_end(run_id: int, *, provider: str) -> list[str]   # ids written
store_root() -> Path
```

P0 ships these as stubs that raise `NotImplementedError`, except `parse`, `dump` and `store_root`. `ingest` is not public; earlier messages that named it meant `round_end`. There is no `apply_ops`.

## Format

### Frontmatter

- The file starts with a `---` line. The frontmatter ends at the next `---` line. The rest is the body.
- Each frontmatter line is `key: value`.
- Lists are inline only: `[a, b, c]`, split on commas, each item trimmed.
- A value may be quoted with `"` or `'`. Quote it if it contains a comma, `[`, `]` or `:`.
- Values are strings, except `confidence` (float) and list-valued keys.
- `dump` writes required keys first in the order `id, type, title, aliases, created, updated`, then the rest sorted by name.
- Unknown keys are kept in `meta` and written back unchanged.

Required keys: `id`, `type`, `title`, `aliases` (at least one), `created`, `updated`.
Optional keys: `evidence` (list of ids), `confidence` (0 to 1), `source`, `supersedes` (id), `valid_until` (date), `tags` (list).
Episodes must also have `source`, either `run` or `dataset`.

### Ids

Ids match `^[a-z0-9][a-z0-9-]{0,79}$`, are unique across the store, and are never renamed. Episodes use `ep-<action_id>` for harness actions and `post-<status_id>` for dataset posts.

### Body

One fact per bullet, each dated: `- **Label:** statement; stated on YYYY-MM-DD.` Links are `[[id]]`. Bodies are capped at 8,000 characters.

### Files

Read and write with `encoding="utf-8"` and `newline="\n"`. Use `pathlib` everywhere.

## P1 store

- **Store path.** `MEMSTORE_PATH` if it's set. Otherwise, if `config.PROVIDER == "mock"`, a `tempfile.mkdtemp(prefix="memstore-")` directory, created once per process. Otherwise `memory/store/`.
- **Seeding.** On first access, if the store is missing or empty, copy `memory/seed/` to `<store>.tmp`, then `os.replace` it to `<store>`, while holding the lock.
- **Lock.** Writes create `<store>/.lock` with `os.O_CREAT | os.O_EXCL`, retry every 0.1 s for 5 s, then raise `TimeoutError`. Reads take no lock.
- **`.gitignore`.** Add `memory/store/`.
- **Validation.** `write_note` raises `MemoryValidationError` with a clear message, and leaves the store unchanged, when:
  - a required key is missing, or `aliases` is empty
  - the type is unknown
  - the id is malformed, or is already used by a note at another path
  - a date is not `YYYY-MM-DD`
  - `confidence` is outside 0 to 1
  - the body is over 8,000 characters
  - an episode has no valid `source`
  - the evidence rule fails
- **Evidence rule.** A `lesson` or `reflection` must list in `evidence` at least one id that resolves to an episode with `source: dataset`, or with `source: run` and either `outcome` or `review_status` set. A lesson resting only on the panel's predicted scores is rejected. Other types are exempt.
- **Updates.** Writing an existing id at the same path replaces the note and sets `updated` to today.

## P2 search

- **Tokenizing.** Lowercase, split on `[^a-z0-9+#.]+`, drop common English stopwords. Query phrases are its 1-grams and 2-grams.
- **Scoring**, per note:
  - +10 for each phrase equal to the id or to an alias (case-insensitive)
  - +4 for each phrase found in the title
  - +1 for each body line containing a phrase as a whole word, at most 3
  - +2 for each distinct phrase matched
  - multiply by 0.5 for episodes, so evidence doesn't crowd out lessons
- Drop zero scores. Break ties by newest `updated`. There is no fuzzy matching.
- **Snippets.** Up to 3 matching body lines, each cut to 200 characters.
- **`context_for(text)`.** Runs `search(text, k, types)` and renders each hit as `### <title> (<type>, <id>)` followed by its first 5 bullets. The output is capped at 2,000 characters. Empty text gives `""`.
- **`prompt_block()`.** All procedures, then lessons with `confidence >= 0.6` and at least 3 evidence ids, sorted by confidence. Capped at `max_chars`. Returns `""` when there is nothing to show or on any error, and never raises.

## P3 seed and wiring

- About 8 notes in `memory/seed/`, covering all six types, written for dev tooling startups. Example notes must not state made-up findings as fact. Label invented examples as examples, or use `[VERIFY: ...]`.
- `harness/config.py`: `lessons()` returns the `lessons.md` text, plus `"\n\n" + prompt_block()` when that is non-empty, inside `try/except` so memory can never break a run.

## P4 round end

- `round_end(run_id, *, provider)` reads `harness.memory.run_actions(run_id)` and the `outcomes` table, and writes one `ep-<action_id>` note per action through `write_note`. It makes no LLM call.
- **Episode note.** `source: run`, `tags: [<mind>, <action type>]`, plus `review_status` and `outcome` when present. Aliases are the mind, the action type and the narrative id. Body bullets hold the mind, type, narrative, predicted score, draft text and critique. The draft text is the harness's own writing, so it is safe to store; never store raw scraped signal text.
- **Ledger.** `<store>/.ingested.json` maps `action_id` to `{"review_at", "outcome_at", "status"}`. An action is rewritten only when one of those changed, so a second call returns `[]`.
- **Mock.** When `provider == "mock"` and `MEMSTORE_PATH` is not set, write to the temp store, never `memory/store/`.
- **CLI** (`python -m harness.memstore`): `selftest`, `search <query>`, `show <id>`, `round-end <run_id> --provider <p>`, and `snapshot` (copies the live store into `memory/seed/` for a deliberate commit).

## Acceptance checks

`python -m harness.memstore selftest` sets `MEMSTORE_PATH` to a temp directory before anything else, copies the seed into it, and stops at the first failure. It never touches `memory/store/` or `data/harness.db`.

1. **Round trip.** `write_note` then `load` returns the same fields, including an unknown key and a body with non-ASCII text. The file on disk has `\n` line endings.
2. **Rejections.** A missing field, duplicate id, bad id, bad path, unknown type, and a lesson with only panel evidence each raise `MemoryValidationError`, and the store is unchanged.
3. **Accepted.** A lesson citing a `source: dataset` episode passes. Rewriting the same id updates the note instead of duplicating it.
4. **Search.** An alias match outranks a body match. `art` does not match `start`. `types` filters. Results have all five keys.
5. **Prompt text.** `prompt_block()` and `context_for("")` return `""` on an empty or missing store. On the seed, `prompt_block()` is non-empty and within its cap.
6. **Round end.** Against a fixture SQLite file with one run of two actions, the first call returns two ids, the second returns `[]`, and `memory/store/` is unchanged.
7. **Harness.** `uv run python -m harness run --provider mock` passes, and with an empty store `config.lessons()` returns exactly what it did before.

A package is done when its part of the selftest passes, the mock run passes, `memory/store/` stays out of git, and none of the shared contracts changed.
