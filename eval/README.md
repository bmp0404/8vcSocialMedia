# Post judge

The BAML judge grades a candidate post against successful and poor-performing examples from
`data/devtools_virality_30d.csv`. It returns five rubric scores, evidence comparisons, a verdict,
confidence, and suggested revisions. Python validates the score bounds and reference IDs, then
computes `score_100` as four times the sum of the five scores.

## Setup

From the repository root, install the locked dependencies with `uv sync`. The generated Python client is committed, so a fresh checkout needs no BAML compiler.

When changing BAML source, regenerate the client:
The compiler and `baml-bridge` runtime must both be version 0.22.2.

```sh
uv sync
baml toolchain install 0.22.2
BAML_VERSION=0.22.2 baml generate --directory eval
```

The generated `baml_sdk/` Python sources are tracked. Regenerate them after changing BAML source and commit the generated diff with the source change. The generator writes an ignore-all file, so newly generated Python files must be explicitly staged with `git add -f`; never stage `__pycache__` or `.pyc` files. In PowerShell, set `$env:BAML_VERSION = "0.22.2"` before running `baml`.
If the CLI reports a stale installed agent skill, run `baml agent install` for that toolchain.
The compiler source of truth is [baml_src/main.baml](baml_src/main.baml).

## Run

Inspect the selected comparisons without making a model request:

```sh
uv run python -m harness.post_judge eval/examples/candidate.json --dry-run
```

Install Claude Code and authenticate it with `claude auth login`, then grade one candidate:

```sh
uv run python -m harness.post_judge eval/examples/candidate.json --output out/post-grade.json
```

The default provider is BAML's `claude_code.ClaudeCodeClient`, using the authenticated local
`claude` executable and the `sonnet` model alias. Set `HARNESS_JUDGE_MODEL` to choose another
Claude Code model. No separate API key is needed for this client. BAML passes the output schema
through Claude Code's native `--json-schema` option. Harness tools are disabled and the client
uses normal permission mode with a 120-second timeout.

The CLI makes a model request through your Claude Code account. There is no mock quality score.
Offline tests use explicit fixtures and do not establish that the judge predicts engagement.

Direct API clients remain optional. Set `HARNESS_JUDGE_PROVIDER=openai` to use `OPENAI_API_KEY`
and optional `OPENAI_BASE_URL`, with `gpt-4.1-mini` as the default model. Set
`HARNESS_JUDGE_PROVIDER=anthropic` and `HARNESS_JUDGE_MODEL` to your Anthropic or gateway model ID
to use `ANTHROPIC_API_KEY`, or `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`. Token authentication
sends a bearer Authorization header and the token as `x-api-key`. Verify that your gateway
accepts these headers. Direct API request construction is tested offline; real gateway
authentication remains unverified.

Judge settings are independent of `HARNESS_PROVIDER` and `HARNESS_MODEL`.

A candidate JSON object has `text`, `mission`, `context`, and `lane`. The lane is `post`, `reply`,
or `quote`. Put evidence supporting factual claims and the reply-parent text in `context`.
The sample file uses an invented release with its supporting context.

`--dataset` selects another CSV with the same columns. `--per-class` selects 1 to 10 examples per
performance class, default 3. `--before YYYY-MM-DD` uses only earlier references. Repeat
`--exclude-id <post-url>` to exclude holdout posts by ID.

## Reference selection

`Super Viral` is the successful class and `Deadweight` is the poor-performing class.
`Medium Viral` rows are excluded from the comparison pool. These labels describe observed
engagement, not human judgments about writing quality.

The selector prefers lexical overlap, breaks ties with a stable ID hash, and takes one post per
account per class. It balances and interleaves the classes. Exact duplicate URLs and normalized
post text are removed. The candidate's normalized text is excluded. A missing performance class,
invalid CSV record, or insufficient distinct accounts produces an error instead of a fabricated grade.

Lexical overlap is a first retrieval baseline. It cannot establish that two posts have comparable
audiences. Media is represented by its type, not its contents. Follower counts, impressions,
paid promotion, and bot exposure are absent from this dataset. The judge must report those limits.

## Python integration

```python
from baml_sdk import CandidatePost
from harness.post_judge.judge import grade_post

candidate = CandidatePost(
    text="Draft from another model",
    mission="The account's audience and goal",
    context="Facts and source material available to the drafting model",
    lane="post",
)
evaluation = grade_post(candidate)
print(evaluation.score_100, evaluation.grade.verdict)
```

`Evaluation` includes the candidate, selected references, dataset SHA-256, model name, rubric
version, and grade. Provider errors, invalid score ranges, and invented citations raise errors.
Callers must handle those failures explicitly. Do not interpret `InsufficientEvidence` as a usable
ranking or treat this grade as posting approval.

For backtests, split data before calling the judge. Use `grade_with_references(candidate,
references, dataset_sha256=...)` with a train-only pool. This rejects candidate-text leakage and
requires both performance classes. The caller owns the broader split boundary, including near
duplicates, events and related threads. Never retrieve references from the holdout dataset.

## Verification

```sh
BAML_VERSION=0.22.2 baml check --directory eval
BAML_VERSION=0.22.2 baml test --directory eval
uv run python -m unittest discover -s tests -p 'test_post_judge.py'
uv run python -m harness run --provider mock --sources sample
```

[The eval design](JUDGE_DESIGN.md) separates this judge from the backtests that measure it and
lists the next evaluation systems to build.

## Rank alternatives in one request

Use `rank_candidates` when choosing among 2 to 6 drafts for the same target. Every candidate must
share a mission, context, and lane. Give each draft a stable ID and pass a train-only reference pool
containing both performance classes. The result includes every candidate exactly once, ranked by
a relative 0 to 100 score, with a verdict, confidence, reason, and reference citations.

```python
from baml_sdk import RankingCandidate
from harness.post_judge.ranking import rank_candidates

# candidate_a and candidate_b are CandidatePost objects with the same context.
result = rank_candidates(
    [RankingCandidate(id="a", post=candidate_a), RankingCandidate(id="b", post=candidate_b)],
    references,
    dataset_sha256=training_dataset_hash,
)
for item in result.ranking.ranked:
    print(item.candidate_id, item.score, item.verdict, item.reason)
```

This makes one model request for the whole batch. Scores are relative within the batch and are
not interchangeable with single-post rubric scores. Ties are allowed. A batch can contain no
`Strong` drafts; do not post the top draft merely because it ranked first. Malformed rankings,
invented IDs, invalid scores, and leaked candidate text raise errors. A timeout is an error,
not an abstention or a low score. No sub-10-second latency guarantee has been established.

For offline action tests, stub the BAML `RankCandidates` boundary with an explicit
`CandidateRanking` fixture, as shown in `tests/test_post_ranking.py`. Production code never
substitutes fixture scores after a provider failure.
