# Post judge

The BAML judge grades a candidate post against successful and poor-performing examples from
`data/devtools_virality_30d.csv`. It returns five rubric scores, evidence comparisons, a verdict,
confidence, and suggested revisions. Python validates the score bounds and reference IDs, then
computes `score_100` as four times the sum of the five scores.

## Setup

From the repository root, install the locked dependencies and generate the Python client.
The compiler and `baml-bridge` runtime must both be version 0.22.2.

```sh
uv sync
baml toolchain install 0.22.2
BAML_VERSION=0.22.2 baml generate --directory eval
```

The generated `baml_sdk/` package is ignored by Git. Regenerate it after changing the BAML source
or on a fresh checkout. In PowerShell, set `$env:BAML_VERSION = "0.22.2"` before running `baml`.
If the CLI reports a stale installed agent skill, run `baml agent install` for that toolchain.
The compiler source of truth is [baml_src/main.baml](baml_src/main.baml).

## Run

Inspect the selected comparisons without making a model request:

```sh
uv run python -m harness.post_judge eval/examples/candidate.json --dry-run
```

Set `OPENAI_API_KEY` in your environment or `.env`, then grade one candidate:

```sh
uv run python -m harness.post_judge eval/examples/candidate.json --output out/post-grade.json
```

The judge defaults to OpenAI `gpt-4.1-mini`. Set `HARNESS_JUDGE_PROVIDER=anthropic` and
`HARNESS_JUDGE_MODEL` to your Anthropic or gateway model ID to use Anthropic instead.
Use `ANTHROPIC_API_KEY`, or the team's `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` gateway
configuration. Token authentication sends an Authorization bearer header and the token as
`x-api-key` for compatibility with the BAML Anthropic client. Verify that your gateway accepts
these headers. `OPENAI_BASE_URL` is also supported. Request construction is tested offline;
real provider and gateway authentication still needs a configured key.

`HARNESS_JUDGE_MODEL` can override the OpenAI model too. Judge settings are independent of
`HARNESS_PROVIDER` and `HARNESS_MODEL`.
This command makes one paid model request. There is no mock quality score. The offline tests use
explicit fixtures and do not establish that the judge predicts engagement.

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
