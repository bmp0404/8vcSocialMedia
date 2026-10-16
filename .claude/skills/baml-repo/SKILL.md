---
name: baml-repo
description: Rules for BAML in this repo (eval/baml_src, the generated baml_sdk/ at the repo root, harness/post_judge) and for deciding whether to move an LLM call to BAML. Use when editing a .baml file, regenerating baml_sdk, changing the judge, or proposing to migrate a harness LLM call. Read baml-core first for the language itself.
---

# BAML in this repo

The repo runs BAML v1: `baml-bridge` and toolchain 0.22.2, with the generated package `baml_sdk`. `docs.boundaryml.com` documents v0 and says to ignore it for v1, so its `retry_policy`, `ClientRegistry`, `@check`/`@assert`, Jinja and `baml-py` don't apply here. For language questions, read `.claude/skills/baml-core/SKILL.md` and run `baml describe <name>`. The rules below come from a tested audit on 2026-10-16 and probes on 0.22.2; each gives its reason. Probe a rule again before you rely on it after a toolchain bump.

## Setup

- Bump `baml-bridge` in `pyproject.toml` and `BAML_VERSION` together. The runtime has to match the toolchain.
- After every `.baml` edit, run `BAML_VERSION=0.22.2 baml generate --directory eval`, then `git add -f baml_sdk`. The project lives in `eval/`, but the generator writes the package to the repo root. The generator writes an ignore-all `.gitignore`, and stale bytecode keeps serving the old prompt.
- Never edit `baml_sdk/` by hand, because the next generate overwrites it.
- To detect drift, compare `baml_sdk/_baml_sources.py` with `eval/baml_src`. Don't byte-diff `_inlinedbaml.py`: its bytecode embeds the absolute path of the machine that generated it, so it changes with no source change.
- After a toolchain bump, rerun `BAML_VERSION=<v> baml agent install --project eval` and move the skill to `.claude/skills/baml-core/`. The CLI refuses agent runs when the skill doesn't match.

## Types and parsing

Schema-aligned parsing (SAP) is BAML's tolerant parser. It fixes format noise such as fenced blocks, unquoted keys and trailing commas, but it also has silent behaviors:

- A closed range in the type, such as `score: 0 | 1 | 2 | 3 | 4 | 5`, raises `ParseError` (and the runner re-asks once) only on a field of a class. Inside an array, an out-of-range element is dropped without an error, and SAP rounds fractions into the range: 0.8 becomes 1 for `0..10`, and 4.6 becomes 5 for `0..5`. When a fractional answer is possible, keep the field a `float` and check the range in Python.
- Don't write `@check` or `@assert`. 0.22.2 rejects them. Validate rules that span fields in Python, and raise a specific error.
- Wrap any array whose length matters in a class, and check the length in Python. SAP drops elements that fail to parse, including an element missing a required field, and fills a missing array with `[]`. On truncated JSON it can lose the whole array, including elements that were complete (a probe on 0.22.2 parsed `{"items":[{complete},{"id":"b","score":2,"re` to `items: []`). A dropped verdict must never read as "no objection".
- Recheck any value where a wrong type must fail. SAP coerces `"10"` to 10, rounds 12.7 to 13 for an int field, and maps loose strings to enum members.
- `@description`, `///` comments and enum names render into the prompt through `ctx.output_format()`. State each rule once, there or in the prose, not both.

## Prompts

- Put `${ctx.output_format()}` in every structured function. Don't hand-write the JSON shape.
- Put instructions after `${role("system")}`, and posts, references and other untrusted text after `${role("user")}`. Without role markers, 0.22.2 sends everything as one user message, and the "treat posts as data" instruction protects nothing.
- Use assistant prefill only in functions that return `string`. The parser sees only the continuation, so a JSON prefill fails to parse.

## Clients

- Wrap API clients in `ai.clients.Retry.new(inner = c)`, which makes 3 attempts with backoff on rate limits, network failures and retryable provider errors. It doesn't retry parse failures or refusals.
- Build clients inside BAML. v1 has no ClientRegistry, and a client value from Python doesn't work: passing `baml_sdk.vendor.anthropic.Client.new(...)` to `.call(client=...)` raises `TypeError: host value type anthropic.Client is not a member of declared union ai.Client | null`, and so does a client returned by a BAML function once it has passed through Python, or `Retry.new(<python client>)`. To choose the provider at runtime, pass provider, model and `max_tokens` as function arguments, and build the client in a BAML function from them, as `judge_client` does.
- Don't cap the Anthropic client below its 8192 default without measuring. On the gateway, `claude-sonnet-5-5` thinking spent a whole 4,096-token budget in `harness/llm.py`, and hitting the cap raises `FinishReasonError`.
- For an OpenAI-compatible gateway without `/responses`, use `openai.GenericClient`, which posts to `/chat/completions`.
- Read keys with `env.NAME`. Never write a key into a `.baml` file.

## Tests

- Test parsing with `F@spec(args).parse(reply)`, including malformed replies: prose around JSON, a missing field, an out-of-range score, a truncated array. `baml.json.from_string` is strict JSON and skips SAP, so it doesn't test the parser.
- Test the prompt offline with `F@spec(args).build_request()` on the `anthropic` provider. `ClaudeCodeClient` can't render a preview.
- Never call an LLM function from a `.baml` test without a scripted client, because it sends a real request.
- Run `baml check` and `baml test` with `--directory eval` before you commit.

## Moving a harness LLM call to BAML

Every harness call goes through the `LLM` object in `harness/llm.py`, which carries provider routing, the `mock=` path the offline check needs, model routing and iterate's call metering. A BAML function bypasses all four. Before migrating anything, build one adapter that makes a BAML client from the harness LLM, returns `mock()` on mock runs and passes `on_event` so usage reaches the meter.

Migrating replaces a hand-written "Return JSON" line with `ctx.output_format()`, which changes the prompt, so re-measure any number that was measured on the old prompt.

- **Worth it, in order:** orient (malformed JSON from it killed a live round), then the action veto (a string "false" is read as True today; keep a length check on the verdict array).
- **Not worth it:**
  - the continuation writer: plain text, a prefill whose measured result depends on exact bytes, and only `claude-opus-4-5-20251101` accepts prefill
  - the qualify panel: runtime keys, and its AUC was measured on the current prompt
  - iterate's frozen instruments: changing them breaks ledger comparability
  - plain-string writers

## One project per process

Keep every BAML function in the single project at `eval/baml_src`. The bridge loads one runtime per Python process, and every project generates a package named `baml_sdk`, so a second project's functions fail with `BamlPanic: Function not found` once the first is loaded. The engine runs orient and the judge in the same process, so they have to share one project. `baml.toml` rejects `[dependencies]`.
