# LLM reference labels and the label store

This is for whoever uses the reference labels as features, reconciles them by hand, or builds the labeling UI on `harness/label/store.py`. It answers four questions. Which fields can two model families label consistently? Do the labels mean anything for engagement? What did they cost? How does the store work?

Corpus and benchmarks are in `harness/corpus/DESIGN.md`.

## Short answers

- Claude Sonnet 5.5 and GPT-6.1 Sol label seven fields with Krippendorff's alpha between 0.51 and 0.86 on a 1,000-item pilot. Two fields from the starting set were dropped.
- No cheaper Claude-and-GPT pair matched that agreement, so the scale run uses Sonnet and Sol.
- Claude labeled all 14,462 corpus items. GPT-6.1 Sol labeled 5,113, including every one of the 3,580 benchmark replies, in the stretches when the Codex pool behind the gateway was serving it. Gate 3 asks for 10,000 items with both; this run has 5,113, and 9,349 remain for Sol.
- As ranker features, the labels beat the hand-built text block (0.677 against 0.577 alone, both annotators averaged) but add nothing once timing and reach are known: +0.005 [-0.041, 0.040] on the test weeks. On posts Claude's labels rank an account's better post 0.730 of the time.

## Annotators

The gateway (`ANTHROPIC_BASE_URL`, an Anthropic-format proxy) listed 42 model IDs on 2026-10-16; `gpt-6.1-sol` was not among them but answered. Every call logs the requested model and the `model` field of the response. In every logged call the two matched, which shows only what the gateway reports, not which weights served the call.

| Model | Probe | Used for |
| --- | --- | --- |
| `claude-sonnet-5-5` | answered; the cached system prompt was read from cache on almost every call | Claude annotator |
| `claude-haiku-5-5` | answered | pilot screening |
| `gpt-6.1-sol` | answered after an overloaded spell (502, 503 and 429 at 07:10 to 07:30 UTC) | GPT annotator |
| `gpt-6-luna` | answered | pilot screening |
| `gpt-6-sol`, `gpt-5.6-luna`, `gpt-5.6-terra` | answered 4 test items each | not used |
| `gpt-5.5` | refused: "not supported when using Codex with a ChatGPT account" | not used |

Calls run one item each, because batching moved this repo's earlier panel by 0.125 AUC depending on order (`research/eval.md`, finding 1). The shared instructions sit in a cached system prompt; the item, its author, posting time, minutes after the parent, media and the parent's text go in the user message. Counts are never shown. Effort is `low` (`output_config.effort`), and nothing here measured whether the gateway passes it to GPT models.

The first pilot launch ran 11 GPT requests at once, and 56% of Sol's and 40% of Luna's calls failed with 429, 502 or 503 before I stopped it. At three requests per GPT model the failure rate fell to under 1%. The runner keeps the brief's limits: at most 16 in flight, backoff on 429 and 5xx, a stop when more than 20% of a model's last 200 calls failed, and a 40,000-call budget counted from `data/labels/calls/`.

## Schema

`data/labels/schema.json` is version 2:

| Field | Kind | Values | Question |
| --- | --- | --- | --- |
| `format` | enum | announcement, tip_or_howto, opinion_or_take, question, joke_or_meme, praise_or_thanks, complaint_or_criticism, promo_or_event, link_or_share, other | What is this item mainly doing? |
| `specificity` | ordinal | 1 to 5 | How concrete is it? |
| `novelty` | ordinal | 1 to 5 | Would a typical follower of this account learn something new or see a new angle? |
| `humor` | ordinal | none, some, main_point | How much does it rely on humor? |
| `reads_as_ai` | ordinal | 1 to 5 | How likely is it that a language model wrote this text? |
| `engagement_guess` | ordinal | below, typical, above, breakout | Compared with this author's usual posts or replies, how much engagement will it get? |
| `hostility` | ordinal | none, snark, insult | Is it hostile toward a person or group? |

The anchors the annotators see are in `harness/label/schema.py`. Each field is decidable from the text and metadata, is an enum or short ordinal, and maps to a harness decision: what kind of post to write (`format`, `humor`), how to write it (`specificity`, `novelty`, `reads_as_ai`), whether a draft is worth acting on (`engagement_guess`), and what to veto (`hostility`, `reads_as_ai`).

Version 1 had two more fields, both dropped after the pilot:

- `adds_to_parent` (replies, 1 to 5) agreed well (alpha 0.75) but did not predict which reply got more likes: 0.53 [0.47, 0.59] on 438 dev pairs. A model's sense of a reply's contribution is not what readers like.
- `engagement_bait` (yes or no) was "yes" on 5 to 12 of 1,000 items per model, too rare to measure agreement or validity. A bait veto needs targeted examples, not this corpus.

I did not rewrite either: a rewrite would need a fresh pilot, and neither failure looked like wording.

## Pilot

1,000 items, all from the dev split so the schema never saw test: 500 replies from dev reply pairs (207 with 0 likes, 119 with 1 to 4, 75 with 5 to 9, 60 with 10 to 29, 39 with 30 or more) and 500 posts from dev-author pairs plus other posts filled by engagement band (116 at 0 to 10, 171 at 11 to 99, 126 at 100 to 749, 87 at 750 or more). Four models labeled every item under prompt `490e2e305638af44`. `data/labels/pilot.json` lists the items; `data/labels/reports/pilot-v1.json` has every number below.

**Agreement** (Krippendorff's alpha, ordinal for ordinal fields, nominal for enums; the gate is 0.4):

| Field | Sonnet / Sol | Sonnet / Luna | Haiku / Sol | Haiku / Luna | Haiku / Sonnet | Luna / Sol |
| --- | --- | --- | --- | --- | --- | --- |
| format | 0.79 | 0.77 | 0.77 | 0.75 | 0.78 | 0.82 |
| specificity | 0.68 | 0.78 | 0.74 | 0.83 | 0.89 | 0.84 |
| novelty | 0.79 | 0.79 | 0.78 | 0.82 | 0.82 | 0.79 |
| humor | 0.86 | 0.80 | 0.72 | 0.73 | 0.77 | 0.83 |
| reads_as_ai | 0.74 | 0.70 | 0.57 | 0.58 | 0.64 | 0.78 |
| engagement_guess | 0.51 | 0.32 | 0.31 | 0.21 | 0.59 | 0.57 |
| hostility | 0.61 | 0.65 | 0.58 | 0.69 | 0.66 | 0.73 |
| adds_to_parent (v1, 500 replies) | 0.75 | 0.60 | 0.53 | 0.38 | 0.67 | 0.80 |
| engagement_bait (v1) | 0.58 | 0.58 | 0.60 | 0.54 | 0.38 | 0.58 |

Every Claude-and-GPT pair that swaps in a cheaper model drops `engagement_guess` below the gate (0.21 to 0.32) and loses 0.05 to 0.18 on `reads_as_ai`, so no cheaper model matched the pilot agreement. The two families also lean in opposite directions on `engagement_guess`: Sonnet said "below" for 352 items, Sol for 161.

**Validity on dev pairs.** Each field scores both items of a dev benchmark pair, averaged over Sonnet and Sol. "Pre-registered" uses a direction fixed in advance (higher specificity, novelty and engagement_guess win; more AI-sounding and more hostile lose). "Cross-fitted" scores each item by its value's win rate in pairs from other threads or authors.

| Benchmark | Field | Pairs (groups) | Pre-registered | Cross-fitted |
| --- | --- | --- | --- | --- |
| replies | engagement_guess | 438 (87) | 0.70 [0.65, 0.75] | 0.70 [0.65, 0.75] |
| replies | reads_as_ai | 438 (87) | 0.60 [0.54, 0.66] | 0.60 [0.54, 0.66] |
| replies | humor | 438 (87) | | 0.59 [0.55, 0.64] |
| replies | format | 438 (87) | | 0.57 [0.51, 0.62] |
| replies | hostility | 438 (87) | 0.46 [0.42, 0.50] | 0.55 [0.51, 0.59] |
| replies | specificity | 438 (87) | 0.52 [0.46, 0.58] | 0.52 [0.46, 0.59] |
| replies | novelty | 438 (87) | 0.52 [0.45, 0.59] | 0.50 [0.44, 0.56] |
| posts | engagement_guess | 236 (43) | 0.77 [0.69, 0.84] | 0.72 [0.64, 0.80] |
| posts | novelty | 236 (43) | 0.73 [0.66, 0.79] | 0.72 [0.65, 0.78] |
| posts | format | 236 (43) | | 0.65 [0.56, 0.74] |
| posts | specificity | 236 (43) | 0.61 [0.52, 0.70] | 0.58 [0.50, 0.67] |
| posts | reads_as_ai | 236 (43) | 0.53 [0.47, 0.60] | 0.42 [0.36, 0.49] |
| posts | humor | 236 (43) | | 0.48 [0.43, 0.53] |
| posts | hostility | 236 (43) | 0.50 [0.47, 0.53] | 0.48 [0.45, 0.50] |

Every kept field clears 0.5 on at least one benchmark. On replies, snark wins slightly (the pre-registered "hostility loses" direction scores 0.46). `engagement_guess` sees the minutes after the parent and the author's followers in its prompt, so it can restate timing: on the 72 dev pairs whose delays are within 2x of each other it still scores 0.65 [0.56, 0.74], so it is not delay alone. The ablation below asks whether any of it is new.

None of these clear delay on replies (0.755 on test); the ranker ablation is the test that matters.

## Labels as ranker features

The question for the labels: added to the reply ranker (`harness/corpus/DESIGN.md`, "Reply ranker"), do they beat the text block? Label features are each ordinal field's mean rank over the annotators and each enum value's share. The table uses Claude Sonnet 5.5's labels alone (`data/bench/results/replies-labels-claude-sonnet-5-5.json`, `python -m harness.rank replies --labels --label-models claude-sonnet-5-5`). Once Sol had labeled all 3,580 benchmark replies too, the same run with both annotators averaged (`replies-labels.json`, `python -m harness.rank replies --labels`) gave the same answer on the test weeks: labels alone 0.677 [0.612, 0.740], +0.100 [0.006, 0.181] over the text block; added to timing and text, +0.005 [-0.041, 0.040]; added to timing, -0.007 [-0.056, 0.035].

| Cutoff (eval threads) | text alone | labels alone | timing + text | timing + text + labels | labels added to timing + text | labels added to timing |
| --- | --- | --- | --- | --- | --- | --- |
| Sep 29, dev (27) | 0.631 | 0.714 | 0.865 | 0.877 | +0.012 [-0.007, 0.036] | +0.017 [-0.007, 0.044] |
| Oct 6, test (32) | 0.580 | 0.688 | 0.776 | 0.785 | +0.009 [-0.041, 0.046] | -0.012 [-0.070, 0.036] |
| Oct 13, test (8) | 0.564 | 0.658 | 0.751 | 0.751 | +0.000 [-0.047, 0.042] | +0.004 [-0.054, 0.056] |
| Both test weeks (40) | 0.577 | 0.683 | 0.772 | 0.779 | **+0.007 [-0.036, 0.040]** | -0.009 [-0.060, 0.033] |

Labels alone beat the text block alone by 0.106 [0.001, 0.195] on the test weeks: as a description of a reply's content, seven LLM fields carry more than the hand-built text features. Neither the labels nor the text block adds anything measurable once timing and reach are in the model. Next to timing, the largest label weight at the Oct 6 cutoff is `reads_as_ai` (-0.60): replies that read as machine-written lose, which is the one label result that points at how to write.

`engagement_guess` sees minutes after the parent and followers in its prompt, so its weight partly restates timing. A label set that hides those would answer "do the words matter" more cleanly; this one answers "does the LLM add to timing", and the answer is no.

On posts the labels do carry signal. A pairwise logistic on Claude's labels, fitted on dev authors, ranks test-author pairs at 0.730 [0.667, 0.783], 0.128 [0.059, 0.203] above length and about level with the TF-IDF ridge (0.711 on the 932 pairs it can score). `engagement_guess` and `novelty` lift and `promo_or_event` sinks (`data/bench/results/posts-labels-claude-sonnet-5-5.json`).

**Stability.** Sonnet relabeled the 1,000 pilot items under the version 2 prompt (`data/labels/screening/retest-v2-*`). Against its own version 1 answers, alpha is 0.90 to 0.95 per field and 0.75 for `engagement_guess`. Claude is consistent with itself, so most of the disagreement in the pilot table is between the families. I did not retest Sol.

## Scale

The surviving schema went to every corpus item outside the pilot, ordered so the benchmark items came first: 3,080 reply-benchmark items, 1,055 post-benchmark items, then 9,327 others, listed in `data/labels/scale-order.txt` (run prefixes `scale-v2` and, after a restart, `scale-v2b`; prompt `eb8acc6a017cdcc1`).

| | Claude Sonnet 5.5 | GPT-6.1 Sol | Both |
| --- | --- | --- | --- |
| Items with a label (pilot and scale) | 14,462 (8,716 replies, 5,746 posts) | 5,113 | 5,113 (3,580 replies, 1,533 posts) |
| Benchmark items covered | all | all 3,580 replies; 1,289 of 1,311 posts | same as Sol |

Sol's throughput followed the Codex pool behind the gateway, not the request rate:

- 08:14 to 08:30 UTC, about 60 calls a minute.
- 08:40 to 09:37, under 10 a minute, then 85 of its last 200 calls failed with 502 ("servers are currently overloaded") and 503 ("auth_unavailable: no auth available ... model=gpt-6.1-sol"). The per-model guard stopped it while Sonnet finished.
- 10:05, every call returned 503. 10:37, it answered again and I resumed it.
- 10:37 to 12:51, between 10 and 60 calls a minute. Eight requests at once drew 17% failures for no gain; four did best. Twice, at 12:14 and 12:44, the gateway answered 429 "All credentials for model gpt-6.1-sol are cooling down (last error: usage_limit_reached)" and the run sat idle for about 15 minutes. I stopped it at 12:51, in the second cooldown.

To finish the GPT side:

```sh
uv run python -m harness.label run --set data/labels/scale-order.txt --models gpt-6.1-sol \
  --run-prefix scale-v2b --concurrency gpt-6.1-sol=4 --skip-labeled
```

`--skip-labeled` skips the 4,113 scale items Sol already answered in either run. At the 30 to 60 calls a minute it managed when the pool served it, the other 9,349 take three to five hours, and they draw on the same Codex usage limit the cooldowns came from, which other work on this machine shares. Switching them to GPT-6 Luna would put `engagement_guess` below the agreement gate (0.32 with Sonnet). Both are the lead's call.

Of the 5,113 items both models labeled, 411 agree on all seven fields and 4,702 differ on at least one (1,209 on one field, 1,566 on two, 1,232 on three, 695 on four or more). Of the ordinal disagreements, 94% are one step apart (9,469 of 10,080); the other 936 are `format`. That is why the queue meant for a person is the audit set, not the disagreement list.

**Calls, tokens and errors per model**, every logged HTTP attempt in `data/labels/calls/` (pilot, scale, retest, probes and the 49 calls of an aborted first scale launch whose prompt still mentioned a dropped field):

| Model | Calls | Answered | Input tokens (uncached) | Input tokens read from cache | Output tokens | Errors |
| --- | --- | --- | --- | --- | --- | --- |
| claude-sonnet-5-5 | 15,497 | 15,492 | 4,577,418 | 21,198,599 | 1,991,897 | timeout 4, invalid answer 1 |
| claude-haiku-5-5 | 1,012 | 999 | 301,701 | 1,698,820 | 169,074 | invalid answer 13 |
| gpt-6.1-sol | 5,520 | 5,136 | 7,498,638 | 118,400 | 586,238 | 429: 139, 502: 121, 503: 112, timeout 8, 408: 4 |
| gpt-6-luna | 1,029 | 1,004 | 1,673,903 | 4,352 | 116,747 | 503: 13, 502: 10, 429: 1, timeout 1 |
| gpt-6-sol, gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna | 39 | 30 | 43,576 | 1,280 | 5,172 | 502 and 503: 9 |

That is 23,097 logged label calls. Phase D's judges made 1,191 more (`harness/corpus/DESIGN.md`), probe scripts that did not log to that folder about 60, and each of about ten restarts dropped at most 16 requests in flight that no log counts. The total stays under 24,600 of the 40,000 budget. "Invalid answer" means a reply that failed schema validation (Haiku invented formats such as "wish_or_opinion") and was retried. The gateway reported cache reads for GPT models only rarely, so their input tokens are mostly uncached.

## The label store

Files under `data/labels/`, the contract the UI batch builds against:

- `schema.json`: `{"version", "fields": [{"name", "kind", "values", "question", "applies_to"}]}`. The UI renders its controls from this.
- `items.jsonl`: one item per line with the contract keys `item_id` (`x:<status_id>`), `kind`, `status_id`, `url`, `author`, `author_followers`, `text`, `created_at`, `parent_id`, `metrics`, `source`, plus one extra key, `context`, holding what the annotator prompt needs (bio, media, minutes after the parent, flags). Gitignored, because it holds individuals' replies; `python -m harness.corpus rebuild` writes it from the local snapshot (`harness/corpus/DESIGN.md`, "Rebuilding the local files").
- `llm/<run_id>.jsonl`: the reference annotators' answers, `item_id`, `run_id`, `annotator` (`model`, `response_model`, `prompt_sha`, and `effort`), `labels`, `rationale`, `usage`, `at`, `error`. Every model with a file here votes. The committed `rationale` is always empty.
- `rationales/<run_id>.jsonl`: gitignored `item_id`, `run_id`, `at`, `rationale`. The runner writes the rationale here because it can quote the item, and `llm_labels` joins it back by item, run and time.
- `screening/`: the pilot runs of models that were compared but not chosen. They never vote.
- `human.jsonl`: append-only `item_id`, `labeler`, `labels` (partial is fine), `note`, `at`.
- `audit.json`: the frozen audit set behind `queue("audit")`.
- `calls/<run_id>.jsonl`: one row per HTTP attempt, for the budget and the cost table.

API in `harness/label/store.py` (module functions use `data/labels/`; `Store(root)` takes another directory):

| Function | Returns |
| --- | --- |
| `load_schema()` | schema.json as a dict, re-read when the file changes |
| `items(ids=None)` | items in file order, or the given IDs in that order; an unknown ID raises `KeyError` |
| `llm_labels(item_id)` | every LLM answer for the item, oldest first, errors included |
| `consensus(item_id)` | per field: `value` (the strict plurality of the latest error-free vote per model, None on a tie), `agreement` (share of votes equal to it), `votes` (model to value), `human` (the latest human value with labeler and time, or None) |
| `queue(mode, limit, offset)` | entries with the item, `n_disagree`, `disagree_fields`, `llm_labeled`, `human_labeled` |
| `append_human(item_id, labeler, labels, note)` | the appended row; unknown fields, values outside the schema, fields that don't apply to the item's kind and blank labelers raise `ValueError` |

Queue modes: `disagree` (LLM votes differ on a field no human has settled, most disagreeing fields first), `unlabeled` (no human label yet, disagreements first), `all` (everything, disagreements first) and `audit`. One developer can't reconcile thousands of disagreements, so `audit` is the queue meant for a person: 200 items frozen by `python -m harness.label audit` from the 5,113 items both models labeled, an equal share from each of six strata (post or reply, times 0, 1 to 2, or 3 or more disagreeing fields), so 200 human labels estimate how often the LLM labels are right both where the models agree and where they don't. Labeled items drop out of it.

A human label never replaces an LLM label: `consensus` reports it under `human`, and the LLM files are never rewritten. The store loads `llm/` once per process, so a server must restart to see a new run. `tests/test_label_store.py` has 17 tests on fixtures, covering every function, mode and validation error.

## Not verified

- That the gateway's model names are the models that served the calls; only the response's own `model` field was recorded.
- That `effort: low` reached the GPT models.
- Human agreement with any field. The audit set exists so this can be measured.
- Test-split validity of single fields; the pilot measured dev only.
