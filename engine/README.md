# Run the engine

The engine connects memory, observation, narrative selection, action generation, and evaluation in bounded rounds. It writes recommendations for review and never posts to X.

From the repository root, run two offline rounds:

```sh
uv sync
uv run python -m engine --provider mock --rounds 2
```

The command prints the output directory. Mock mode uses sample observations and a separate temporary database, lessons file, and memory-store path. Files remain available after the command exits. Use `--state-dir out/my-run` to choose that directory.

For real drafts with the reference judge, first complete the [judge setup](../eval/README.md), then run:

```sh
uv run python -m engine --provider anthropic --sources sample --mission mission/mission.md --eval reference --narratives 2 --per-mind 1 --rounds 1
```

The draft provider and judge provider are separate. `HARNESS_JUDGE_PROVIDER=claude_code` uses the authenticated local Claude Code client through BAML. Select a mission file that describes your account. The repository's default mission may describe a different audience.

Each round reads lessons, observes sources, selects narratives, drafts actions, and evaluates them. When every draft is vetoed, the engine passes the critiques back to action generation, up to `--retries` times. Reference evaluation admits only `Strong` posts, replies, or quotes. An empty result is an abstention. A stage error stops the loop and records the failed stage.

Inspect `out/engine/<round-id>/` for the round journal, observations, narratives, drafts, and evaluation evidence. `out/recommendations-engine-<round-id>.json` contains the selected actions and SQLite run ID. The database also retains vetoed drafts. Real runs use the configured shared storage unless you provide `--state-dir`.

Memory currently uses `config.lessons()` and `learn()`. Learning requires recorded outcomes; predicted scores do not become successful-outcome lessons. Krish's `prompt_block`, `context_for`, and `round_end` integration and Will's trace integration are pending their component code. The engine scopes legacy global configuration, so run it serially within a process.

To evaluate posts you write yourself, use the [post lab demo](demo/README.md).
