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
| `run [--lane reply\|post\|both] [--email [--send]]` | Run the lane(s); writes `out/recommendations-<run>.json`, a JSON list of `Action.to_dict()`, best first, vetoed drafts and declines removed. This file is the hand-off contract. `--email` then builds the hand-off email: a dry run (`out/handoff-<run>.eml/.txt`) unless `--send` is given and SMTP is configured. With a non-default `HARNESS_DB`, `<run>` is `<db stem>-<id>` |
| `record <file>` | Store real metrics: `[{"action_id": "...", "metrics": {"likes": 3}, "post_id": "..."}]` (X `public_metrics` names work too); marks rows `posted` |
| `learn` | Rewrite the lessons file from predicted vs. actual; both lanes recall it next run |
| `review` | Open the approve/edit/kill dashboard at `http://127.0.0.1:8765`; every verdict rewrites `out/approved-run<id>.json` |

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
| `harness/sources/` | **Observe.** One plugin per data source, registered in `sources/__init__.py`: `sample`, `feed` (teammate scraper output, `HARNESS_FEED_PATH`; flexible field names), `x_api` (query built from the targets file), `reddit` |
| `harness/lanes/reply.py` | **Reply lane.** Hottest not-yet-engaged posts become targets, watchlist authors boosted (priority more); each mind owns a few focus targets and replies / quotes / mentions / follows (watchlist only) or declines with `wait` / `no_response` + a reason. Declines are logged to memory, never emailed |
| `harness/lanes/post.py` | **Post lane.** `orient.py` clusters signals into narratives; minds write original posts |
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
- [ ] Pull real metrics automatically instead of `record`
