# 8VC Media Harness

An agentic harness that recommends X actions for a brand account, built for the 8VC ATX build session
(Oct 15, 2026). It recommends actions and doesn't post anything; 8VC's Grok bot decides what gets posted.
The brand is whatever `HARNESS_MISSION` points at (default `mission/mission.md`, a Caitlin Clark fan
account example).

```
            sources/ (observe) ──> signals
                 ┌──────────────────┴──────────────────┐
 reply lane:  pick target posts ─> minds: reply/quote/   post lane:  orient -> narratives ─> minds:
              mention/follow/skip ─> eval                            original posts ─> eval
                 │        ▲                                            ▲        │
                 │        └──── memory.recall(lane): lessons, best/worst, ─────┘│
                 │              recent texts, engaged handles/urls              │
                 └──> memory `actions` table (context + post + eval + status + actuals) <──┘
                                      │                          ▲
          out/recommendations-<run>.json -> email hand-off       │ record (X metrics) -> learn -> lessons.md
```

## Quickstart

```powershell
pip install -r requirements.txt
copy .env.example .env          # then fill in a provider + key
python -m harness run --provider mock   # offline smoke test, no key needed
python -m harness run                    # real run, both lanes, with the provider in .env
python -m harness run --lane reply       # or --lane post

# Coordigent demo on the synthetic feed, with a throwaway DB, ending in a dry-run hand-off email
$env:HARNESS_DB="data/coordigent_demo.db"; $env:HARNESS_SOURCES="feed"
$env:HARNESS_FEED_PATH="data/sample_feed_coordigent.json"
python -m harness run --lane both --email          # add --send to really email it (needs SMTP_*)
```

Every env var is listed in `.env.example`.

## Commands

| Command | What it does |
| --- | --- |
| `run [--lane reply\|post\|both] [--email [--send]]` | Run the lane(s); writes `out/recommendations-<run>.json`, a JSON list of `Action.to_dict()`, vetoed drafts and declines removed. This file is the hand-off contract. Content actions are ranked by `predicted_score` x the learned action-type weight (top `--top` per lane), then up to `HARNESS_FOLLOW_SLOTS` follows in reserved slots (`context.slot = "follow"`, ranked by watchlist priority, relationship, feed activity and mind endorsements, never re-following an account). `--email` then builds the hand-off email: a dry run (`out/handoff-<run>.eml/.txt`) unless `--send` is given and SMTP is configured. With a non-default `HARNESS_DB`, `<run>` is `<db stem>-<id>` |
| `ingest-reply FILE\|-` | Apply the posting bot's reply email (`ACTION_ID <id> \| <tweet URL>` / `\| followed` / `\| skipped \| <reason>`): marks actions posted or rejected, keeps skip reasons |
| `ingest-metrics [--from x\|FILE] [--handle H]` | Fill actual metrics for posted actions from the X API (default; `--handle` matches unlinked posts by text) or a scraper's .json/.csv |
| `calibrate` | Predicted vs. actual (bias, MAE) by lane, mind, type, watchlist relationship and panelist |
| `learn` | Rewrite the lessons file, mind/type weights (`data/weights.json`) and mission proposals (`out/mission-proposals.md`, never applied automatically) from predicted vs. actual and bot skips |
| `record <file>` | Low-level metrics import: `[{"action_id": "...", "metrics": {"likes": 3}, "post_id": "..."}]`; marks rows `posted` |
| `review` | Open the approve/edit/kill dashboard at `http://127.0.0.1:8765`; every verdict rewrites `out/approved-run<id>.json` |

`python scripts/send_handoff.py [recommendations.json] [--send]` re-sends an existing recommendations file;
like `run --email` it's a dry run unless `--send` is passed.

## Runbook: the learn loop

```powershell
# 1. run: draft, score and rank; write out/recommendations-<run>.json + a dry-run email
python -m harness run --lane both --email
# 2. email: when the preview in out/handoff-<run>.txt looks right, send it to the posting bot
python -m harness run --lane both --email --send   # or: python scripts/send_handoff.py --send
# 3. the bot replies one line per action; save the reply body and apply it
python -m harness ingest-reply out/bot-reply.txt   # or pipe it in: Get-Content reply.txt | python -m harness ingest-reply -
# 4. a few hours later, pull real metrics for what got posted
python -m harness ingest-metrics --from x --handle coordigent   # or --from data/scrape/metrics.csv
# 5. check how far off the audience panel was
python -m harness calibrate
# 6. learn: lessons + weights + mission proposals
python -m harness learn
# 7. run again: lanes recall the lessons, minds get quotas from the weights, ranking uses type weights
python -m harness run --lane both --email
```

Env vars for the loop (all in `.env.example`):

| Var | Used for |
| --- | --- |
| `HARNESS_PROVIDER`, `HARNESS_MODEL`, `ANTHROPIC_API_KEY` / `XAI_API_KEY` / `OPENAI_API_KEY` | LLM for minds, panel, critic and learn (`mock` = offline) |
| `HARNESS_MISSION`, `HARNESS_TARGETS` | Brand mission and watchlist (`mission/coordigent.md`, `mission/coordigent_targets.json`) |
| `HARNESS_SOURCES`, `HARNESS_FEED_PATH`, `HARNESS_FEED_SINCE_DAYS`, `HARNESS_FEED_LIMIT` | What to observe; e.g. `feed` + `data/devtools_virality_30d.csv` + `HARNESS_FEED_LIMIT=40` (newest 40) |
| `HARNESS_DB`, `HARNESS_LESSONS`, `HARNESS_WEIGHTS`, `HARNESS_PROPOSALS` | Memory, lessons, learned weights (default next to the DB) and mission proposals; point all four at a temp dir for a throwaway loop |
| `HARNESS_FOLLOW_SLOTS` | Reserved follow slots per run (default 2; 0 disables follows) |
| `HARNESS_EVALUATOR` | `on` (default) / `off`: reply triage gate + panel/pairwise ranking (see Evaluation) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `HANDOFF_EMAIL_FROM`, `HANDOFF_EMAIL_TO`, `HANDOFF_EMAIL_CC` | Hand-off email; only used with `--send` |
| `X_BEARER_TOKEN` | `ingest-metrics --from x` (and the `x_api` source) |

## Evaluation (pre-posting)

`harness/evaluator/` runs after the critic (`qualify.py`) when `HARNESS_EVALUATOR=on` (the default):
- **Reply triage**: before any mind drafts, one batched call (judge model) labels each target post engage / wait / skip. Only `engage` targets get drafts; the rest are logged as `wait` / `no_response` rows with a typed reason.
- **Ranking**: in each lane, `predicted_score` = 0.5 x panel percentile + 0.5 x Haiku pairwise-tournament percentile (Bradley-Terry). A Sonnet rubric adds per-criterion notes and guardrail flags (voice / grounding / cringe below threshold). These show as `EVAL_FLAGS` in the hand-off email and as `context.eval` in the JSON. Only a hard grounding failure (rubric grounding 1/5) vetoes; with 6+ drafts in a lane, the tournament's bottom tercile is cut. Follows skip the evaluator and keep their own ranker.
- **Backtest** (`scripts/backtest_eval.py`, 1,665 real dev-tool X posts, holdout accounts, Spearman vs within-account engagement): panel+pairwise **+0.62**, panel +0.56, length baseline +0.45, rubric +0.40.
- **A/B**: `HARNESS_EVALUATOR=off` falls back to the panel score alone (no triage, no gates).

### Review dashboard

`python -m harness review` lists each run's recommendations, best predicted first, with tabs for
To review / Approved / Killed / Critic vetoed. You can edit a draft before approving it (the original
text is kept as `original_text` in the export), override a critic veto, and leave a note.
Keyboard: `j`/`k` move, `a` approve, `x` kill, `u` reset, `e` edit.

`out/approved-run<id>.json` is the hand-off file for the posting bot.

## Layout

The central filesystem is organized into five directories:

```text
observation/  # Inputs and observations collected by the harness
action/       # Actions and execution workflows
eval/         # Evaluations, scoring, and validation
memory/       # Persistent context and learned information
data/         # Source documents, datasets, and supporting assets
```


| Path | Stage |
| --- | --- |
| `mission/*.md` | Mission files; `HARNESS_MISSION` picks one. An `**Audience panel:**` list of `- name: description` bullets sets the eval panel. Product facts in the mission are the only brand facts drafts may state (grounding) |
| `mission/*_targets.json` | Watchlist (`accounts`: handle, priority, relationship, notes) + search `keywords`; `HARNESS_TARGETS`, default `<mission stem>_targets.json` |
| `harness/sources/` | **Observe.** One plugin per data source, registered in `sources/__init__.py`: `sample`, `feed` (teammate scraper output, `HARNESS_FEED_PATH`; flexible field names, including the `data/devtools_virality_30d.csv` export; trim with `HARNESS_FEED_LIMIT` / `HARNESS_FEED_SINCE_DAYS`), `x_api` (query built from the targets file), `reddit` |
| `harness/lanes/reply.py` | **Reply lane.** Hottest not-yet-engaged posts become targets, watchlist authors boosted (priority more); each mind owns a few focus targets (draft cap = `learn.quota`, scaled by learned mind weights) and replies / quotes / mentions (watchlist only) or declines with `wait` / `no_response` + a reason. A priority watchlist post the assigned mind passes on gets a second look from another mind before it's declined. Follows bypass the panel: `rank_follows` picks up to `HARNESS_FOLLOW_SLOTS`, skipping accounts already followed. Declines are logged to memory, never emailed |
| `harness/lanes/post.py` | **Post lane.** `orient.py` clusters signals into distinct narratives, which are dealt round-robin so each mind writes on its own ones (one post per narrative); minds write original posts. Drafts the reply lane kept earlier in the run count as already said |
| `harness/lanes/common.py` | Shared lane context + the Eval step (score, critique, write to memory) |
| `harness/minds/` | Persona prompts, one per file; `MIND_LANES` maps each to the reply and/or post lane: `reply_sniper`, `friendly_rival` (reply), `agent_wrangler`, `technical_explainer` (both), `build_in_public` (post) |
| `harness/qualify.py` | **Eval.** Simulated audience panel scores drafts and predicts a positive/neutral/negative sentiment split; hard rules + a critic veto. Vetoes carry a `reason`: off_tone, stale, wrong_target, nothing_to_add, factual_risk (invented brand facts), repetitive, other |
| `harness/memory.py` | **Memory layer.** SQLite `actions` table (`data/harness.db`, now with `sentiment` and `reason` columns; status recommended / posted / rejected / declined) + `recall(lane)`. A target counts as engaged if posted to in 7 days or recommended in the last 24h |
| `harness/learn.py` | **Learn.** Writes `memory/lessons.md` (`## General` / `## Reply lane` / `## Post lane`) |
| `harness/review/` | **Human checkpoint.** Stdlib HTTP server + single-page dashboard |
| `harness/llm.py` | Provider switch: `anthropic`, `xai`, `openai` (any OpenAI-compatible gateway), `mock` |

## Status / TODO

- [x] Live X data: `sources/x_api.py` (needs a paid tier for recent search) and `sources/feed.py` (teammate scraper)
- [ ] `sources/reddit.py`: needs Reddit OAuth (unauthenticated requests return 403)
- [ ] Point `HARNESS_MISSION` at 8VC's mission file
- [x] Hand-off transport to 8VC's Grok bot (email, `harness/handoff/`; `run --email [--send]`)
- [x] Human approve/kill checkpoint (`python -m harness review`)
- [ ] Feed review verdicts and notes into `learn`
- [x] Pull real metrics (`ingest-metrics --from x`) and the bot's reply (`ingest-reply`) instead of `record`
