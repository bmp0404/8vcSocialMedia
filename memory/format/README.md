# Memory log format

This is a proposed log format for the Python harness in `harness/`. The harness would write these logs in addition to the SQLite store in `data/harness.db`, not instead of it. Each round writes one JSON log per stage. `observe` covers `harness/sources/` and `harness/orient.py`, `decide` covers `harness/minds/`, `eval` covers `harness/qualify.py`, and `act` records what went to review. The harness only recommends actions. A separate Grok bot does the posting.

Logs embed X API v2 objects unchanged, so a predicted `public_metrics` can be diffed field by field against the actual one. Each `*.example.json` in this directory shows one log type. All ids, names and texts in them are placeholders.

## Layout

```
memory/
  inbox/<file>.jsonl          # backfill team drop: raw X API v2 GET /2/tweets/search/recent response pages (data + includes)
  loops/<loop_id>/
    observe.json  stash.json  decide.json  eval.json  act.json
  outcomes/<tweet_id>.json    # actual metrics for our own posted tweets, from the backfill team
```

## Envelope

Every log starts with these fields.

| Field | Value |
| --- | --- |
| `schema_version` | `"1"` |
| `loop_id` | The loop this log belongs to |
| `step` | `observe`, `stash`, `decide`, `eval`, `act` or `outcome` |
| `ts` | ISO 8601 timestamp |

## X API v2 objects

Tweet objects (`data[]`, `includes.tweets[]`) use the v2 fields unchanged: `id`, `text`, `author_id`, `created_at`, `conversation_id`, `lang`, `possibly_sensitive`, `referenced_tweets[{type: quoted|replied_to|retweeted, id}]`, `entities{hashtags[{start, end, tag}], mentions[{start, end, username, id}], urls[]}`, `context_annotations[{domain{id, name}, entity{id, name}}]` and `public_metrics{impression_count, like_count, retweet_count, reply_count, quote_count, bookmark_count}`.

User objects (`includes.users[]`) use the v2 fields unchanged: `id`, `username`, `name`, `verified` and `public_metrics{followers_count, following_count, tweet_count, listed_count}`.

## Step files

**`observe.json`** ([example](observe.example.json)) is a v2 search response (`data`, `includes{users[], tweets[]}`) with harness fields added beside it:

- `source{kind: "backfill_file", path}` names the inbox file it was read from, and `window{start, end}` gives the time range it covers.
- `buckets[]` are the hashtag "buckets of interest" the harness tries to go viral within, such as a rivalry bucket, a Fever bucket and a Hawkeyes bucket. Each has `bucket_id`, `hashtags[]`, `tweet_ids[]` and `stats{volume, velocity_per_hr, median_engagement_rate, p80_engagement_rate, small_account_breakout_rate}`. `small_account_breakout_rate` exists because the account is tiny, at about 2 followers. Whether small accounts can reach an audience in a bucket matters more than raw volume.

**`stash.json`** ([example](stash.example.json)) is what Decide sees each cycle:

- `buckets[{bucket_id, stats, top_k_tweet_ids[]}]`
- `own_history[{loop_id, tweet_id, bucket_id, predicted_public_metrics, actual_public_metrics}]`
- `lessons` is reserved for later and is `null` for now.

**`decide.json`** ([example](decide.example.json)) holds the candidates from every mind for one `attempt`. Each candidate has `candidate_id`, `mind` (a file name in `harness/minds/prompts/`, such as `rivalry_banterer` or `stat_nerd`), `bucket_id`, `action_type` (`post`, `reply`, `quote`, `mention` or `follow`), `request`, `source_tweet_ids[]` and `rationale`.

`request` is a literal X API v2 request, `{method, path, body}`:

- `post`, `reply`, `quote` and `mention` use `POST /2/tweets` with body `{text, reply: {in_reply_to_tweet_id}?, quote_tweet_id?}`.
- `follow` uses `POST /2/users/:id/following` with body `{target_user_id}`.

**`eval.json`** ([example](eval.example.json)) scores every candidate from the same `attempt`. Prediction combines retrieval of similar past posts (`neighbors[]`) with an LLM judge for brand fit and risk. Each entry in `results[]` has:

- `candidate_id`
- `neighbors[{tweet_id, similarity, author_followers, public_metrics}]`
- `predicted_public_metrics`, `predicted_engagement_rate` and `bucket_percentile`
- `judge{brand_fit, risk_flags[], critique}`
- `verdict` (`ship` or `veto`) and `veto_reason` (`null`, `below_threshold`, `risk`, `no_fit` or `cooldown`)

`selected_candidate_id` is `null` when every candidate is vetoed. Decide then retries with the critiques attached, up to a small maximum number of attempts. After that, Act records `none`.

**`act.json`** ([example](act.example.json)) has `decision` (`ship` or `none`), `reason_code` (`null`, `no_fit`, `cooldown`, `all_vetoed` or `risk`), `candidate_id`, `request` and `predicted_public_metrics`. `act.json` is a memory record only. The hand-off file for the Grok bot stays `out/approved-run<id>.json`, which `python -m harness review` writes after a human approves. `none` is a first-class action. A `none` decision looks like this:

```json
{"schema_version": "1", "loop_id": "loop-20261015T2200Z", "step": "act", "ts": "2026-10-15T22:01:10Z",
 "decision": "none", "reason_code": "all_vetoed", "candidate_id": null, "request": null, "predicted_public_metrics": null}
```

**`outcomes/<tweet_id>.json`** ([example](outcome.example.json)) is the actual v2 tweet object for one of our posted tweets, as collected by the backfill team. It sits under `data` with `loop_id`, `candidate_id` and `age_hours` beside it.

## Rules

- X API v2 objects are embedded unchanged. Harness-specific fields sit alongside them, never inside them.
- `predicted_public_metrics` uses exactly the v2 `public_metrics` keys, so it can be diffed against outcomes.
- An action reaches the Grok bot only after a positive eval and a human approval. The harness itself never posts.
- Whether `impression_count` is available depends on the X API access tier. Non-public and organic metrics exist only for tweets the account owns. The backfill team still has to confirm both points.

## Open questions

These are not decided yet:

- Eval ship thresholds, and the follower-size filter for neighbors.
- How buckets are discovered.
- Loop cadence and cooldown values.
- How posted tweets are matched back to candidates.
