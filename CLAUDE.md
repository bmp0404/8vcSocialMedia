# CLAUDE.md

This repo is a media harness built at the 8VC ATX build session. It recommends X actions for a brand account and writes them to `out/`; a person decides what gets posted. Each brand is a mission profile under `mission/profiles/<name>/`, and `HARNESS_PROFILE` picks one (`wnba` by default, or `devtools`). Several people's agents may edit this repo at the same time.

Read `CONTRIBUTING.md` before you add a stage, change a shared contract, or decide where a file goes. `README.md` lists the commands.

## Never post

Don't write code that posts, replies, likes, follows or otherwise writes to X, and don't call those endpoints while testing. We have no posting access, and the harness only recommends. Reading X is fine.

## Check your work

```sh
uv sync
uv run pre-commit install    # once per clone; installs the commitlint commit-msg hook
uv run python -m harness run --provider mock
```

The mock run is the only check that needs no API key, so run it before you report a change as done. Keep it passing. When you add an `llm.complete_json` call, pass a `mock=` that returns the shape your caller reads.

Real runs spend the team's LLM credits. Test with `--narratives 2 --per-mind 1` unless you need the full run.

## Work alongside other agents

- Pull `main` before you start and again before you push. Teammates push often.
- Write commit messages and PR titles as Conventional Commits, such as `feat(observe): add ESPN source` or `fix(memory): keep review notes`. Use the loop stage as the scope. The commit-msg hook and the `commitlint` PR check reject anything else.
- Work on a branch named `<name>/<topic>` and open a pull request. Push to `main` only when the person you work for asks.
- Register with coordigent at session start, and read your inbox before each new task. Messages are how five people's agents learn what changed without reading every commit.
- Before you start work that could collide with someone else's, message the person who owns that area and say what you plan to touch. Collisions include editing a file another session is working in, changing a shared contract (`harness/models.py`, the schema in `harness/memory.py`, `harness/llm.py`, `harness/cli.py`), or building something another team already claimed. Use `coordigent_session_list` to find their session, and ask the project if you can't tell who owns it.
- Message the whole project when you change something every agent needs to know: a shared contract, a command, a file location, a new dependency, or a decision about how the team works. Say what changed, the commit or branch, and what others have to do about it.
- Leave files outside your task alone. Merge conflicts across five branches cost more than any cleanup is worth.

## Code conventions

- Put brand-specific text in the profile's files (`mission.md`, `prompts.toml`) and read them through `config.profile_file()`. The same code runs for every profile.
- A source that can't fetch raises an error that says why. `observe()` prints it and skips the source. Don't return an empty list to hide a failure.
- Drafts must not invent facts or stats. Prompts tell the model to write `[VERIFY: ...]` instead.
- Open files with `encoding="utf-8"` and use `pathlib`. Teammates run Windows and macOS.
- Keep keys in `.env`. Never print a key or commit one.
- Add a dependency with `uv add <package>` and commit `pyproject.toml` and `uv.lock` together. Don't use `pip install` or bring back `requirements.txt`.

## Skills

The repo ships these skills in `.claude/skills/`. Read the matching `SKILL.md` before the task.

- `unslop`: any prose, and every draft post or reply the harness produces.
- `prompt-writing`: editing a mind in `harness/minds/prompts/` or any prompt string in `harness/`.
- `technical-writing`: docs, including each stage's `DESIGN.md`.
- `pr-writing`: pull request titles and descriptions.
- `deslop`: reviewing a diff before you commit it.
- `baml-core`: the BAML v1 language, from the 0.22.2 toolchain (`baml agent install`). `docs.boundaryml.com` documents v0, so don't follow it here.
- `baml-repo`: editing `eval/baml_src`, regenerating `eval/baml_sdk`, or moving a harness LLM call to BAML.
