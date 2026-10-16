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
| `harness/llm.py` | Provider switch: `anthropic`, `xai`, `openai` (any OpenAI-compatible gateway), `mock` |

## Status / TODO

- [ ] `sources/x_search.py`: wire up live X data (Grok live search, X API, or Playwright)
- [ ] `sources/reddit.py`: needs Reddit OAuth (unauthenticated requests return 403)
- [ ] Replace `mission/mission.md` with 8VC's mission file
- [ ] Hand-off format/transport to 8VC's Grok bot
- [ ] Human approve/kill checkpoint (review UI) before hand-off
- [ ] Pull real metrics automatically instead of `record`
