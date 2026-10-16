# Integrating Sam's stages

Readers: Sam's lead and the stage owners, deciding what merges next and which stage to fix next. This file records how `sam/integration` was assembled, how the other teams' branches merge into it, and what tonight's live rounds measured.

## Merge order

`sam/integration` is `main` plus the branches below, merged in this order with `git merge --no-ff`. Observe (PRs #10, #20, #25), the pytest setup (#8), the LLM fixes (#24), Dhilan's engine (#9, which carries the judge from #5) and Aarav's post-ideas CSV (#19) are already on `main`.

| # | Branch | Head merged | Why this position | Conflicts and resolution |
| --- | --- | --- | --- | --- |
| 1 | `sam/context-dynamics` | `b96a8f6` | Observe's `x_targets` and `attention` sources import `harness.context.xreader` and `harness.context.collectors.attention`. Without this branch both raise and `observe()` skips them. | `pyproject.toml` and `uv.lock`: took `main`'s side, then `uv add "tzdata>=2026.5"`. |
| 2 | `sam/eval` (PR #13) | `77ca08d` | Changes `qualify.py`, which every run calls. | Eval rebased twice, so its files came back as add/add conflicts against its own earlier commits. Took eval's side for every file eval owns. |
| 3 | `sam/action` (PR #14) | `1364b14` | Replaces decide's lanes and reads Observe's `Narrative.reply_targets` and `Signal.thread`. | `models.py`: both sides add optional fields; git merged them. `writer.py`: took Action's digit-free mock captions over the integration copy. |
| 4 | `sam/memory` | `43eb33a` | Adds `review_events` to the `memory.py` schema and memory subcommands. | Memory rebased, so `harness/learning/` came back add/add; took memory's side. `cli.py`: kept every additive block. |
| 5 | `sam/iterate` | `36ce6fa` | Knob registry and promotion gate. | `cli.py`: another additive block, kept. |
| 6 | `sam/ui` | `7a124a5`, local and unpushed | Console and trace views in `harness/review/`, plus `harness/trace.py`. | None. Its `trace.py` matches the integration fix below. |

Rules used for every merge:

- `pyproject.toml` and `uv.lock`: take one side, then re-run `uv add` for each dependency the other side added. `uv.lock` is never merged by hand or by git's text merge, even when git reports no conflict.
- `models.py` and `cli.py`: every change is additive, so both sides are kept.
- A rebased branch comes back as add/add conflicts on its own files. Take its side for the files it owns.

## Seam fixes

Each is a commit on this branch, and each went to its owner through the lead.

| Fix | Owner | Status |
| --- | --- | --- |
| `harness/trace.py` wrapped the LLM in a plain `TracingLLM`. Through `python -m harness run`, the writer skipped thread continuation, the veto ran on `claude-sonnet-5-5` instead of `claude-opus-5-5`, qualify ignored `PANEL_MODEL`, and mock runs read fxtwitter and spent the real daily budget. The wrapper is now a subclass of the inner LLM's class. | UI | On `sam/ui` (`7a124a5`). Action also routes on capability now (`harness/minds/routing.py`), so each fix covers the other. |
| `harness/llm.py`: `HARNESS_MAX_TOKENS` (thinking emptied answers at 4,096), `parse_json` decodes the first JSON value (a greedy `{.*}` spanned two objects and killed a round in orient), one retry on malformed JSON, `MockLLM.is_mock`. | Shared | On `main` (PR #24). |
| `tests/test_observe_reply_targets.py` reached a live fxtwitter client once `harness.context` was present. | Observe | On `main` (PR #20). |
| `tests/conftest.py` refused loopback, so Context's cache tests that connect to `127.0.0.1:9` failed. It also lets `HARNESS_GROUNDING` default to off in unit tests, because post_judge's `claude_code` provider runs the Claude Code client in a subprocess the socket guard can't see, and a lanes test hung there. | Observe (conftest), Action | Integration only. Action has since made the grounding veto opt-in. |
| `tests/test_dashboard.py`: the engine dashboard's mock round signal had no link, so Action's post lane had nothing to caption and the round shipped nothing. | Dhilan | Integration only. |
| Action's mock captions carried digits, which the gate rejects as ungrounded. | Action | On `sam/action` (`MOCK_WORDS`). |

Still open, sent to owners:

- **Engine** (`engine/runner.py`): `run_round` never calls `harness.minds.write_decision_log(run_id)`, so engine rounds lose every abstention and `Budget.from_logs` never counts them. `reference_evaluate` vetoes follows. `read_memory` skips memory's `round_start`.
- **Observe**: `x_targets` raises "every read failed" when the timelines succeed with nothing fresh and search is unavailable (run 39). An empty, successful read should return no targets, not an error. `x_fx.thread_replies` catches only `RuntimeError`, so any other exception drops the whole source.
- **Action and Observe**: captions of a Google News story link a `news.google.com/rss/articles/...` redirect and credit "news.google.com". Observe is resolving the redirects; Action should credit the publisher.
- **Action and `cli.run`**: the veto retry only re-rolls captions now. Chorus text never ships, so "revising" vetoed chorus drafts does nothing, and the post lane captions the same source again (run 36 shipped two captions of one story).

## How the other teams' branches merge

| Branch | Result against `sam/integration` | What collides |
| --- | --- | --- |
| `origin/aarav/self-improving` (`910c98b`) | 12 conflicting files, including `cli.py`, `learn.py`, `llm.py`, `memory.py`, `minds/__init__.py`, `orient.py`, `qualify.py`, `sources/__init__.py`, `memory/lessons.md` | Aarav retargets the harness to Coordigent and rewrites decide around an action catalog and tournament. Action's lanes replace the same `decide()` internals, so `minds/__init__.py` is a design conflict: one decide has to win. `learn.py` and `memory/lessons.md`: his `learn.py` has an LLM rewrite the lessons file, Memory's renders it from tested lessons only; the memory research recommends the tested version (ACE's collapse result). `orient.py`: Observe's budgeted payload supersedes his truncation fix. `memory.py` and `sources/__init__.py`: both append; keep both. `llm.py`: `main` now has the same JSON retry he added; keep `main`'s and add his `TruncatedError`. He adds `scikit-learn`. His post-ideas CSV is already on `main` (PR #19). |
| `dhilan/engine`, `dhilan/version-1` | On `main` | The engine runs the `sam/*` stages through `Components` unchanged; `python -m engine --provider mock --rounds 1` completes on this branch. The gaps are listed under "Still open". |
| `origin/krish/memory` | On `main` | Only the memory spec. No store code is pushed, so nothing calls `context_for` or `round_end` yet. |

## Hand-off formats

None is agreed with 8VC yet.

- `out/approved-run<id>.json`: the dashboard's export, approved actions only, human edits applied (`harness/review/__init__.py:export_approved`).
- `out/grok-handoff-run<id>.{json,md}` and `out/grok-handoff-tonight.{json,md}`: written by `harness/integration/handoff.py`. Each recommendation carries the target URL, action type, text, rationale, a `send_before` time for replies, and its evidence: narrative, sources, the target post's live counts, the veto's verdicts, qualify's verdict, the integration checks and the hand screen.
- Aarav's CSVs: `action/coordigent-post-ideas.csv` on `main`, and on his branch `out/coordigent-suggested-posts.csv` (one row per public draft) and `out/ideas.csv` (de-duplicated across runs).

## Integration tooling

`harness/integration/` is new and owned by this branch.

- `live.py` runs `harness run` unchanged and records the signals, narratives, decide and qualify calls, and the run's decision log, which `cli.run` keeps only in memory, in `out/integration/run<id>.json`. It picks files by run id, because other runs write to `out/` at the same time.
- `measure.py` audits a recorded round: what survives each gate, every abstention, whether each reply target resolves on fxtwitter by the author in its URL, the Opus 5.5 AI-register lineup from `research/action.md` (four real texts plus the draft, two orders, chance 20% per order), and one Opus 5.5 call per draft for P(AI), a targeted insult (X's `FosnrAbuseInsults`), engagement bait (`SpamHighRecall`) and which claims tonight's sources support. It also reruns the gate's grounding rules with the whole round as context.
- `handoff.py` writes the hand-off files, applies a hand screen against `mission/mission.md`, and approves only screened actions, with the note "approved by the integration checks, not yet by a person".

The gateway returned the same model id it was sent for all three models used tonight: `claude-sonnet-5-5` (drafts, orient, critic), `claude-opus-4-5-20251101` (continuation and captions) and `claude-opus-5-5` (veto and the integration detector).

## Tonight's live rounds

All rounds ran on `sam/integration` with `HARNESS_PROVIDER=anthropic` through `harness.integration.live`, which bypasses `trace.instrument()`, so they ran the intended models before the tracing fix landed. Three were **measurement rounds** with overrides; read them as tests of one lane, not as production behavior.

| Run | Time (CT) | Settings | Result |
| --- | --- | --- | --- |
| 4 | 20:52 | Production, before Action's lanes merged; `espn_news, google_news, x_targets`, 2 narratives | 5 chorus drafts, critic kept 2 (a Boston post and a follow of @valkyries). Not recorded by `live.py`. |
| 14 | 21:00 | Production, Action's first lanes (judge ranks in two orders) | 0. Five reply drafts were near copies of one Ticketmaster joke, so the judge's top pick changed with order and it shipped nothing. |
| 22 | 21:12 | Production, 7 sources, 5 narratives | 1 quote: "nice arena now add caitlin" on @valkyries' "Ballhalla awaits". |
| 31 | 21:20 | Production | 0. The only reply target was stale, and post spacing (120 minutes after run 22's quote) blocked the post lane. |
| 36 | 21:28 | **Measurement**: no `x_targets`, `post_spacing_minutes=0` | 0. Two Boston captions shipped from decide; qualify's critic vetoed both. |
| 39 | 21:30 | **Measurement**: `reply_max_age_minutes=120` | 0. No fresh X post on the watch list; `x_targets` raised. |
| 40 | 21:32 | **Measurement**: 8-hour target window, `reply_max_age_minutes=480`, spacing off | 1 quote caption, later dropped by the hand screen. The planner declined all 4 reply targets. |

### What survived each gate

Runs 22, 31, 36, 39 and 40, which share the current pipeline:

| Stage | Reply lane | Post lane (captions of real sources) |
| --- | --- | --- |
| Offered | 6 target offers, 4 distinct posts | 3 sources captioned |
| Declined before drafting | 2 stale (71 and 81 min old against a 30-min cutoff), 4 by the planner (2 off-mission, 2 no angle) | |
| Drafts | 0 | 16 |
| Past the slop gate | | 15 (one said Clark "drops 30", a number absent from the source) |
| Past the Opus veto | | 10 (vetoes: 3 unsupported claim, 1 reads as AI, 1 split between orders) |
| Shipped by decide | | 4 |
| Kept by qualify's critic | | 2 (both run 36 captions vetoed for an opaque Google News link and a caption that only makes sense beside the article) |
| Past the integration checks | | 1 (run 40's was picked in both lineup orders) |
| Past the hand screen | | 1 |

Abstentions over the five rounds: 2 stale targets, 2 off-mission and 2 no-angle planner declines, 2 rounds with no reply target, 2 post-lane budget stops (spacing and the daily post cap, both counted against recommendations, not posts), and 1 follow of a handle the narratives didn't offer. Every round also asked a person for a take on the Boston story.

### The checks

- **Reply targets resolve.** All 6 checks on 4 distinct posts resolved on fxtwitter, matched the author in the URL, and matched the text orient carried. The @valkyries target showed 0 replies against 263 likes and 37 reposts, which fits a post with replies restricted; fxtwitter has no field to confirm it.
- **AI register.** On the 16 caption drafts, the Opus lineup picked the draft in 9 of 32 orders (28%, against 20% chance) and in both orders for 4 drafts. The 4 shipped captions were picked in 4 of 8 orders. Both lineups are weaker than the research's: run 22's target had no replies and run 40's had too few usable ones, so 8 drafts sat among real replies from the action corpus, and the other 8 among the round's Bluesky and X posts. Mean audited P(AI) was 8.
- **Grounding.** The audit, which sees every signal of the round, found one unsupported claim in 16 drafts: "the fever play them next week btw" (the Fever's season is over). The veto had already caught it. The veto also flagged two captions the audit called fine ("the whole league sending love rn", "aliyah cooked on a torn meniscus and nobody knew"). The veto sees only the caption's source and facts block, so its context is narrower; the disagreement is about context, not about the drafts.
- **X's penalty labels.** No draft drew a targeted-insult or engagement-bait flag.
- **Decide against qualify.** On run 36 the Opus veto passed both captions and qualify's critic vetoed them. The critic's reasons were right, and the defect was the opaque redirect link, not either bar. Re-measure once Observe's redirect fix lands before deciding either bar is wrong.
- **The reply writer never ran in integration.** Every target was stopped by the stale cutoff or the planner before drafting, so tonight's integration data says nothing about continuation quality. Action's own live run (`action/experiments/`) and Iterate's lineup runs measure it.

### Qualify evidence from Eval

On the dev-tool dataset, the panel's within-account viral-against-deadweight AUC is 0.797 [0.72, 0.89], `media` alone scores 0.737, and the difference is +0.06 [−0.05, 0.18]. The panel hasn't shown it beats one cheap feature.

### What to fix next

1. **Observe's target supply.** It is the binding constraint. In five rounds Observe offered 4 distinct reply targets: Finals-team posts and one cryptic teaser, none about Clark, none under 30 minutes old. The reply lane, the main lane for a 2-follower account, drafted nothing. Without a search key, `x_targets` reads a 13-account watch list that goes quiet after 9pm CT. Observe is adding mid-size accounts; a paid search read ($0.005 per post on the X API) would remove the limit.
2. **Caption sources.** Link the publisher's article and credit the publisher, not a Google News redirect. Two of the four shipped captions died on that alone.
3. **The retry loop.** Pass the critic's critique to the caption writer, or skip the retry when nothing revisable was vetoed.
4. **Budget semantics.** Post spacing and daily caps count recommendations, not posts. Each round stops the next one even when Grok posted nothing.

## Operating notes

- Poll `x_targets` no more often than every 5 minutes. fxtwitter's 1 request per second is shared across every process on this machine through `~/.cache/media-harness/throttle/`.
- The `attention` source costs about 3 minutes per round, mostly GDELT 429 backoff, which ages `x_targets` data before decide sees it.
- Each reply target costs one planning call, six continuation samples on `claude-opus-4-5-20251101` and two Opus 5.5 veto passes.
- Run `pre-commit install` only from the main checkout. The hook lives in the shared `.git`, and installing it from a temporary worktree points every worktree's hook at that worktree's `.venv`.
