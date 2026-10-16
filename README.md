# 8VC Media Harness: Coordigent

A self-improving harness that recommends X posts for [Coordigent](https://coordigent.com), which handles
coordination for teams running parallel AI coding agents. It recommends actions and doesn't post anything. A human
approves drafts in a dashboard, and what gets approved, and what performs, feeds the next round.

## The agent loop

The goal is to improve recommendations over repeated rounds: start from the mission and remembered
lessons, observe what is happening, propose actions, evaluate them, and carry what worked into the next round.

```text
memory -> observe -> action -> eval -> memory -> next round
```

| Subsystem | Responsibility | Code | Stage files |
| --- | --- | --- | --- |
| **Observe** | Collect signals; cluster them into narratives the brand can join | `harness/sources/`, `harness/orient.py` | `observation/` |
| **Action** | Mind variants (chosen by a bandit) draft candidate actions | `harness/minds/`, `harness/bandit.py` | `action/`, `harness/minds/prompts/` |
| **Eval** | Panel + critic, pairwise tournament, hook pass; backtests against real engagement | `harness/qualify.py`, `harness/tournament.py`, `harness/hooks.py`, `harness/backtest.py` | `eval/` |
| **Memory** | Run log, reviews, outcomes, rewards, prompt variants, lessons, playbook | `harness/memory.py`, `harness/reward.py`, `harness/learn.py`, `harness/evolve.py` | `memory/`, `data/harness.db` |

`data/` holds datasets shared by every stage. `data/devtools_virality_30d.csv` (from the data team)
contains about 1.7k dev-tool company posts labelled Super Viral, Medium Viral, or Deadweight. The harness uses it three ways:
- **Observe:** the `devtools` source turns recent viral posts into reply and quote targets.
- **Action and the playbook:** winners from comparable-size accounts, and flops from those same accounts, become examples.
- **Eval:** `backtest` grades the audience panel and the tournament judge against real engagement.

`engine/` is reserved for continuous orchestration. Today `harness/cli.py` runs the stages.

### One round

```powershell
uv run python -m harness run              # observe -> orient -> decide -> qualify -> tournament -> hooks
uv run python -m harness review           # approve / edit / kill; swap hooks; Learning tab
uv run python -m harness record out.json  # real X metrics, once posted
uv run python -m harness evolve           # calibrate, lessons, playbook, mutate/retire prompt variants
```

## Quickstart

```powershell
uv sync
copy .env.example .env                              # pick a provider + key
uv run python -m harness run --provider mock        # offline check, no key needed
uv run python -m harness backtest --calibrate       # grade + calibrate the panel on real data (uses credits)
uv run python -m harness run --narratives 2 --per-mind 1   # cheap real run
```

## Commands

| Command | What it does |
| --- | --- |
| `run` | Observe, orient, decide, qualify, tournament, hooks; write ranked recommendations to `out/` |
| `review` | Dashboard at `http://127.0.0.1:8765`. **Review**: approve/edit/kill, swap hooks; each verdict rewrites `out/approved-run<id>.json`. **Learning** (`#learning`): variant leaderboard + prompt diffs, panel weights, playbook, lessons, Run evolve |
| `record <file>` | Store real metrics: `[{"action_id": "...", "metrics": {"likes": 3, "reposts": 1}}]` |
| `learn` | Rewrite `memory/lessons.md` from reviews + metrics |
| `evolve` | `learn`, plus: calibrate the panel (5+ own outcomes), rebuild `memory/playbook.md` from our winners and the dataset, mutate champion prompts into challengers, retire losers; `--spawn` invents a new mind |
| `cycle` | `evolve` then `run` |
| `backtest` | Panel rank-correlation and judge accuracy on real dataset posts; `--calibrate` applies the fitted panel weights; report goes to `eval/` |
| `stats` | Variant leaderboard |

## How it improves itself

- **Reward** (`harness/reward.py`) has two signals:
  - A human review: approve = 1, approve-after-edit = 0.7, kill = 0.
  - Real X metrics, which count 3×. Engagement is ranked against the account's own history, with reposts and quotes weighted most.
- **Bandit** (`harness/bandit.py`): each mind has prompt variants. Thompson sampling picks which variants draft each run.
- **Evolve** (`harness/evolve.py`) rewrites the champion prompt of each mind whose variants are all tested, keeps at most
  `--max-active` variants per mind, and refreshes lessons and the viral playbook that every mind reads.
- **Best-of-N:** each variant writes 4 drafts. A Swiss pairwise tournament (Elo) ranks the survivors, and the hook pass
  swaps in a stronger opening line only if it wins head-to-head *and* passes the critic.

## Layout

| Path | What |
| --- | --- |
| `mission/mission.md` | Coordigent's goal, audience, voice, and the **fact sheet**: the only product claims allowed |
| `harness/minds/prompts/*.md` | Seed minds: hook smith, pain storyteller, meme lord, demo dropper, reply sniper. Editing one seeds a new variant |
| `eval/panel.json` | Simulated audience panel |
| `data/devtools_virality_30d.csv` | Dev-tool virality dataset (data team) |
| `harness/sources/` | `sample`, `devtools` (dataset), `hn` (Hacker News, live), `reddit` (needs OAuth), `x` (stub) |
| `harness/llm.py` | Provider switch: `anthropic`, `xai`, `openai` (any OpenAI-compatible gateway), `mock` |

Set `HARNESS_DATA_DIR`, `HARNESS_MEMORY_DIR`, `HARNESS_EVAL_DIR` and `HARNESS_OUT_DIR` to experiment without touching shared files.

## Status / TODO

- [ ] `sources/x_search.py`: live X data (Grok live search, X API, or browser use)
- [ ] Pull post metrics automatically instead of `record`
- [ ] Continuous orchestration in `engine/`
- [ ] Flag suspected boosted engagement in the dataset (see CONTRIBUTING)

## Working on the harness

Start with [CONTRIBUTING.md](CONTRIBUTING.md) for subsystem ownership, file placement, shared
contracts, and the branch/PR workflow. Coding agents must also read [AGENTS.md](AGENTS.md) and
[CLAUDE.md](CLAUDE.md). Coordinate changes to shared interfaces with the other subsystem owners
and keep the offline mock run passing.
