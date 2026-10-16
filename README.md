# 8VC Media Harness

An agentic harness that recommends X actions for a Caitlin Clark fan account, built for the 8VC ATX
build session (Oct 15, 2026). It recommends actions and doesn't post anything; 8VC's Grok bot decides what
gets posted.

```
observe (sources/) -> orient -> decide (minds/) -> qualify -> ranked JSON in out/
      ^                                                            |
      +------------- learn: predicted vs. actual -> lessons.md <---+
```

## Quickstart

```powershell
pip install -r requirements.txt
copy .env.example .env          # then fill in a provider + key
python -m harness run --provider mock   # offline smoke test, no key needed
python -m harness run                    # real run with the provider in .env
```

## Commands

| Command | What it does |
| --- | --- |
| `run` | Observe, orient, decide, and qualify, then write ranked recommendations to `out/` |
| `record <file>` | Store real metrics: `[{"action_id": "...", "metrics": {"likes": 3}}]` |
| `learn` | Rewrite `memory/lessons.md` from predicted vs. actual; the minds read it next run |
| `review` | Open the approve/edit/kill dashboard at `http://127.0.0.1:8765`; every verdict rewrites `out/approved-run<id>.json` |

### Review dashboard

`python -m harness review` lists each run's recommendations, best predicted first, with tabs for
To review / Approved / Killed / Critic vetoed. You can edit a draft before approving it (the original
text is kept as `original_text` in the export), override a critic veto, and leave a note.
Keyboard: `j`/`k` move, `a` approve, `x` kill, `u` reset, `e` edit.

`out/approved-run<id>.json` is the hand-off file for the posting bot.

## Layout

| Path | Stage |
| --- | --- |
| `mission/mission.md` | The account's mission file (placeholder until 8VC's arrives) |
| `harness/sources/` | **Observe.** One plugin per data source; register it in `sources/__init__.py` |
| `harness/orient.py` | **Orient.** Clusters signals into narratives scored on relevance × velocity |
| `harness/minds/prompts/*.md` | **Decide.** One persona per file; add a file to add a mind |
| `harness/qualify.py` | **Qualify.** Simulated audience panel scores drafts; a critic vetoes |
| `harness/memory.py` | SQLite log of runs, recommendations, and outcomes (`data/harness.db`) |
| `harness/learn.py` | **Learn.** Writes `memory/lessons.md` |
| `harness/review/` | **Human checkpoint.** Stdlib HTTP server + single-page dashboard |
| `harness/llm.py` | Provider switch: `anthropic`, `xai`, `openai` (any OpenAI-compatible gateway), `mock` |

## Status / TODO

- [ ] `sources/x_search.py`: wire up live X data (Grok live search, X API, or Playwright)
- [ ] `sources/reddit.py`: needs Reddit OAuth (unauthenticated requests return 403)
- [ ] Replace `mission/mission.md` with 8VC's mission file
- [ ] Hand-off format/transport to 8VC's Grok bot
- [x] Human approve/kill checkpoint (`python -m harness review`)
- [ ] Feed review verdicts and notes into `learn`
- [ ] Pull real metrics automatically instead of `record`
