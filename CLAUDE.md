# CLAUDE.md

This repo is a media harness built at the 8VC ATX build session. It recommends X actions for a brand account and writes them to `out/` for 8VC's Grok bot, which decides what to post. Tonight's brand is a Caitlin Clark fan account. Several people's agents edit this repo at the same time.

Read `CONTRIBUTING.md` before you add a stage, change a shared contract, or decide where a file goes. `README.md` lists the commands.

## Never post

Don't write code that posts, replies, likes, follows or otherwise writes to X, and don't call those endpoints while testing. We have no posting access, and 8VC's bot owns the account. Reading X is fine.

## Check your work

```sh
uv sync
uv run python -m harness run --provider mock
```

The mock run is the only check that needs no API key, so run it before you report a change as done. Keep it passing. When you add an `llm.complete_json` call, pass a `mock=` that returns the shape your caller reads.

Real runs spend the team's LLM credits. Test with `--narratives 2 --per-mind 1` unless you need the full run.

## Work alongside other agents

- Pull `main` before you start and again before you push. Teammates push often.
- Work on a branch named `<name>/<topic>` and open a pull request. Push to `main` only when the person you work for asks.
- If coordigent tools are available, register at session start and read your inbox before you edit a shared contract: `harness/models.py`, the schema in `harness/memory.py`, `harness/llm.py` or `harness/cli.py`. After you change one, message the project, because other branches depend on it.
- Leave files outside your task alone. Merge conflicts across five branches cost more than any cleanup is worth.

## Code conventions

- Put brand-specific text in `mission/mission.md` or a prompt file. The same code has to run against the data team's dev-tool dataset.
- A source that can't fetch raises an error that says why. `observe()` prints it and skips the source. Don't return an empty list to hide a failure.
- Drafts must not invent facts or stats. Prompts tell the model to write `[VERIFY: ...]` instead.
- Open files with `encoding="utf-8"` and use `pathlib`. Teammates run Windows and macOS.
- Keep keys in `.env`. Never print a key or commit one.
- Add a dependency with `uv add <package>` and commit `pyproject.toml` and `uv.lock` together. Don't use `pip install` or bring back `requirements.txt`.
