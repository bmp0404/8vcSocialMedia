# Clearance: the operator console

Clearance is the local console at `python -m harness review`. It is where a person does the human half of 8VC's loop: picks which drafts ship, edits them, kills the off-message ones, overrules what the harness held back, answers the questions it asks a person, and reads what the loop, its experiments and its memory are doing. This file is for teammates deciding what to build next in `harness/review/`.

## What was here

- `harness/review/index.html` on `main` was one page: a run picker, five filter tabs, one card per action with a textarea, a "Why this?" disclosure (rationale, critique, panel bars) and Approve/Kill buttons. Keys: j/k, a, x, u, e.
- `harness/review/__init__.py` served it with five routes. Any web page could POST approvals to it: it read the body as JSON regardless of Content-Type and never checked Host or Origin.
- Nothing showed the post a reply answers, the drafts the harness considered and dropped, its abstentions, a live round, the self-iteration ledger or memory.

## What others built that this uses

| Who | What | How Clearance uses it |
| --- | --- | --- |
| Will (`will/ui`) | `harness/trace.py` writes `out/trace-live.json` during a run and `out/trace-run<id>.json` after; `/console` is his observability page | The Queue reads signals and narratives from the trace to show a reply's target post and the story behind a draft. The Loop reads the live snapshot. `/console` is untouched and linked from the rail. |
| sam/action | `out/decisions-run<id>.json` (every opportunity: shipped, abstained with a reason code, or asked a person), `draft_meta.target`, `draft_meta.judge` (a veto, not a ranker), `harness.minds.gate` with span offsets | Held bay, overrule, Your take bay, veto evidence, candidate drafts, live slop underlines |
| sam/observe | `reply_targets` with counts, followers and the top replies | The X preview and "already in the replies" |
| sam/memory | `memory.set_review(reasons=...)`, `harness.learning.views` | Kill reasons, the Memory view |
| sam/iterate | `eval/experiments/<id>.json` (`iterate.experiment/v1`) | The Experiments view |
| sam/eval | `eval/reports/backtest-*.json`, fault verdicts | The Eval reports view, a placeholder Will can take over |
| Dhilan (PR #9) | A hosted demo dashboard on Fly (`engine/demo/`) with isolated per-browser workspaces and mock rounds | Separate on purpose: his shows each subsystem on demo data to visitors; Clearance works on the real local store and writes the real hand-off file. |

## The direction

Clearance is an air-traffic flight-strip board. Each recommended action is a strip in a bay; the person clears it, strikes it, or releases a held one. The metaphor comes from the brief itself: 8VC frames the harness as Boyd's OODA loop from air combat, and flight strips are the interface controllers use to clear aircraft one at a time under time pressure. It also gives every visual device a job:

- The coloured edge of a strip is its holder and names its lane: blue for replies, amber for posts, graphite for a person's own words. A hatched edge means held back, the same hatching the team's whiteboard used for the undesigned Eval box.
- The ruled cells across the top of a selected strip are its printed fields (type, lane, mind, story, panel score, status).
- Clearing stamps the strip `CLEARED 2137` in 24-hour time, the way controllers mark strips. Striking draws a line through it.
- The selected strip is cocked out of line by 8px, as a controller slides out the strip that needs attention.
- The readback bar at the bottom repeats what the console just did ("CLEARED reply by reply lane · hand-off out/approved-run31.json · z undo"), the way a pilot reads back a clearance.

Inside the post preview, everything switches to X's own look (palette, Chirp's fallback stack, 15/20 text, 40px avatars, X's number format and character ring), so the person judges a draft the way a reader will meet it.

Type: B612 Mono for fields, labels and numbers. Airbus commissioned it for cockpit displays. Atkinson Hyperlegible Next for everything a person reads at speed; the Braille Institute designed it so that characters like l, 1 and I never get confused, which matters when checking handles and stats. Both are OFL and vendored under `static/fonts/` so the console works offline.

Colour carries meaning only: the two lane colours, green and red for a controller's marks, amber for caution, grey for held. Contrast pairs are checked at WCAG AA (noted in `static/css/tokens.css`). Dark mode follows the system and uses X's "lights out" palette in the preview.

## The human checkpoints

8VC's brief gives the person a job at each step of the loop. Where each one happens:

| Step | The person's job | Where |
| --- | --- | --- |
| Map | Pick the conversations most aligned to the brand | Each draft shows the narrative behind it with relevance, velocity and whether velocity came from data or a model's guess. Will's signals view will own the full picture. |
| Plan | Choose the stories | Strips name their story; the Your take bay asks for one when the post lane has nothing good. |
| Create | Supply the faces and the final edit | Inline editing on the X preview with X's weighted character count, the gate's per-type cap, and the slop gate underlining exact spans as you type. "Use this text" swaps in any other draft the harness wrote. A take is posted verbatim. |
| Qualify | Kill anything off-message | Strike with a reason (keys 1 to 7 right after striking). Numbers and superlatives in a draft are flagged as "check before clearing". The critic's veto and the veto model's verdict are on the strip, and a person can clear over them. |
| Deploy | Approve and own relationships | Clearing writes `out/approved-run<id>.json`. Follows render as the profile being followed; mentions show who gets tagged. |
| Learn | Interpret outcomes | Memory shows what the harness believes, each belief's held-out evidence sentence, and when it must be retested. Kill reasons, notes and edits feed sam/memory's learning. |

## How the Queue works

Five bays: To clear, Held, Your take, Cleared, Struck. Held holds critic-vetoed drafts and abstentions from the decision log, releasable ones first. j/k moves, a clears (or releases a hold, or clears a take), x strikes, e edits, z undoes the last verdict, u returns a cleared or struck strip, `[` and `]` switch bays, `?` lists every key.

Verdicts are optimistic: the strip leaves its bay immediately, focus moves to the next one, and the request follows. A failed request puts the strip back and says why in the readback and a toast. Keyboard actions never animate; Emil Kowalski's rule for anything done hundreds of times a session. The only motion in the Queue is the strips' one-time entrance when a run loads, the mobile detail sheet sliding in, and press feedback on buttons, each with a reduced-motion variant.

The slop check runs the real gate (`harness.minds.gate.check`) through `POST /api/gate` on a 180ms debounce, and paints its findings as highlights in a mirror layer behind the textarea, so the underline sits on the exact characters. The gate is not ported to JavaScript: two copies of the rules would drift. On a branch without the gate, the editor says "Slop gate not connected" and why.

Overruling an abstention writes a new action into the same run, marked `origin: "overrule"`, approves it and rewrites the hand-off file. A take is stored the same way with `mind: "human"` and `origin: "take"`. Both use ids derived from the run, the decision and the text, so a double click or a resubmitted take replaces instead of duplicating.

## Stack

Plain ES modules served by the existing stdlib server, no build step, no framework, no dependencies. `index.html` is the shell; `static/js/shell.js` holds a view registry and a hash router; each view is one module under `static/js/views/` with its own stylesheet under `static/css/views/`. A view registers itself with `registerView({id, title, group, css, mount, unmount, onKey})` and needs one import line in `views/index.js`. That is how Will's views move in without touching anyone else's files.

Why no framework: most of the console renders read-only data, and the one stateful region, the editor, is handled with targeted updates so typing never loses the caret. A build step would cost every teammate a Node toolchain to change a label, for gains this console doesn't need. `lib/dom.js` provides an `html` tagged template that escapes every interpolated value, which covers the injection risk a framework would otherwise handle. What would change this: a view with heavy shared state across many components, which nothing here has yet.

Server routes live in modules registered through `routes.py` (`@route("GET", r"/api/...")`), so each view's API is its own file. Every POST must be `application/json` with a local Origin, and every request must carry a `127.0.0.1` or `localhost` Host header. A web page can't send JSON cross-origin without a CORS preflight, which this server never answers, so no other site can approve drafts or start paid rounds through the operator's browser. Static files resolve inside `static/` or 404.

## Fixtures

`?fixture=realistic`, `empty`, `stress` or `error` serves every GET from `static/fixtures/` and keeps POSTs in the tab, for states a live run can't produce on demand. `?delay=1500` slows every response to show loading states. The realistic fixture is built from real output: run 5 (live ESPN and Google News through anthropic), the integration worktree's runs 22 and 31 (real veto verdicts and decision logs), and sam/action's `x_threads.json` (real X posts and replies from 2026-10-15). A few reply drafts in it are written by hand to cover states no real run reached yet, and their gate findings come from the real gate. Timestamps written as `@now-38m` resolve at load so post ages read as they did when captured.

## API added

All additive; the original five routes keep their shapes.

| Route | What it returns |
| --- | --- |
| `GET /api/meta` | provider, model, sources, kill reasons, abstain reason labels |
| `GET /api/runs/<id>/decisions` | the decision log, or `available: false` and why |
| `GET /api/runs/<id>/context` | the run's narratives and signals from its trace |
| `POST /api/runs/<id>/overrule` | ship a held draft: `{decision, candidate?, text, note?}` |
| `POST /api/runs/<id>/takes` | clear a person's answer to `ask_human`: `{decision, text}` |
| `POST /api/gate` | the slop gate on `{text, kind}` |
| `GET /api/loop`, `POST /api/rounds`, `POST /api/rounds/stop` | the Loop view; a round is a fixed small argv with the server's own provider |
| `GET /api/experiments` | the self-iteration ledger |
| `GET /api/memory`, `GET /api/memory/notes?ids=` | beliefs, reflections, edit procedures, outcomes |
| `GET /api/evals`, `GET /api/evals/<file>` | backtest reports and fault verdicts |

## Ruled out

- **Porting the slop gate to JavaScript** for zero-latency checks. Two rule sets would drift, and the round trip to localhost is a few milliseconds.
- **A React or Preact build.** See Stack.
- **Animating verdicts.** A strike line that draws for 200ms before the next strip appears would slow the one flow that has to be fast.
- **Letting the page choose the round's provider.** The lead asked that no request data reach the command line. Start the server with `HARNESS_PROVIDER=...` instead.
- **Persisting draft edits before a verdict.** The store only records edits with a verdict; unsaved edits live in the tab until then.

## Open questions

- Dhilan owns `harness/review/` in CODEOWNERS. Will built `/console` beside it. Which one is the long-term home of Review is undecided; this PR proposes the shell at `/` as the one app and links `/console` until Will moves his views in.
- Should a cleared take or overrule also be logged in the decision log, so sam/action's daily budget counts it? Today only the hand-off file and the store know.
- The account's own X handle is unknown, so drafts render as "Our account". Set `HARNESS_ACCOUNT_HANDLE` once 8VC says what it is.
- Kill reasons on `main` fall back to a "Reasons: ..." prefix in the note until sam/memory's `set_review(reasons=)` lands; `checkpoint.save_verdict` switches automatically and the fallback can then be deleted.
