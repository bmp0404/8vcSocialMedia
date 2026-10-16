# Clearance: the operator console

Clearance is the local console that `python -m harness review` serves. A person uses it for the human half of 8VC's loop. They pick which drafts ship and edit them. They kill the off-message ones, overrule what the harness held back, and answer the questions it asks a person. They also read what the loop, its experiments and its memory are doing. This file is for teammates deciding what to build next in `harness/review/`.

## What was here

- `harness/review/index.html` on `main` was one page: a run picker, five filter tabs, one card per action with a textarea, a "Why this?" disclosure (rationale, critique, panel bars), and Approve and Kill buttons. Its keys were j, k, a, x, u and e.
- `harness/review/__init__.py` served it with five routes. Any web page could POST an approval to it, because it read every body as JSON and checked neither Host nor Origin.
- Nothing showed the post a reply answers, the drafts the harness considered and dropped, its abstentions, a live round, the self-iteration ledger or memory.

## What others built that this uses

| Who | What | How Clearance uses it |
| --- | --- | --- |
| Will (`will/ui`, PR #11) | `harness/trace.py` writes `out/trace-live.json` during a run and `out/trace-run<id>.json` after it. `/console` is his observability page. | The Queue reads signals and narratives from the trace to show a reply's target post and the story behind a draft. The Loop reads the live snapshot. Neither file exists until PR #11 lands, and both views say so and fall back to the round's own log. Clearance carries none of Will's commits. |
| sam/action | `out/decisions-run<id>.json` (every opportunity: shipped, abstained with a reason code, or asked a person), `draft_meta.target`, `draft_meta.judge` (a veto, not a ranker), and `harness.minds.gate` with span offsets | The Held bay, overrules, the Your take bay, veto evidence, candidate drafts and the live slop underlines |
| sam/observe | `reply_targets` with counts, followers and the top replies | The X preview and "Already in the replies" |
| sam/memory | `memory.set_review(reasons=...)` and `harness.learning.views` | Kill reasons and the Memory view |
| sam/iterate | `eval/experiments/<id>.json` (`iterate.experiment/v1`) | The Experiments view |
| Dhilan (PR #9) | A hosted demo dashboard on Fly (`engine/demo/`) with isolated per-browser workspaces and mock rounds | Separate on purpose. His dashboard shows each subsystem to visitors on demo data. Clearance works on the real local store and writes the real hand-off file. |

## What each part is for

Sam asked that every addition earn its place. Each row says what the operator can now do, which human checkpoint from 8VC's brief it serves, how big it is, and the call. Lines count this branch's JS, CSS and Python, without tests. The commands under the table regenerate them.

| Part | What the operator can now do | Checkpoint | Lines | Call |
| --- | --- | --- | --- | --- |
| Server hardening and routes | Nothing new to do. Other web pages can no longer approve drafts, start paid rounds or frame the page through the operator's browser. Before, any site could POST an approval. | Deploy (the hand-off file) | 200 py | Keep |
| Shell and design system | Move between views by key or rail, read what the console just did in the readback bar, and load fixture states. Every view builds on it, and Will's views can register in it. | All | 869 js, 1,688 css, 50 html | Keep |
| Queue: the post as it looks on X | See the target post (author, followers, age, velocity, the replies already there) with the draft where it would land, instead of a bare URL and a textarea. | Qualify, Create | 1,503 js, 911 css, 357 py for every Queue row | Keep |
| Queue: inline edit with the live slop gate | Edit in place with X's weighted character count, and see the gate underline the exact words it flags as you type, checked against the post being answered. | Create (the final edit) | in Queue | Keep |
| Queue: Held bay and overrule | See every opportunity the harness held back and why, then release one of its drafts or write your own. Before, abstentions were invisible. | Qualify, Deploy | in Queue | Keep |
| Queue: Your take bay | Answer the post lane's question in your own words. The answer ships verbatim. | Create (faces and stories) | in Queue | Keep |
| Queue: evidence and candidates | See why the harness chose a draft: the story, the veto model's verdict in both orders, the panel's spread, the critic, and every other draft it wrote, with one button to swap one in. | Qualify | in Queue | Keep |
| Queue: kill reasons, undo, keyboard flow | Strike with a reason in one extra key, undo the last verdict, and triage without the mouse. | Qualify | in Queue | Keep |
| Loop | Start a small round from the page, watch each stage report in, then jump to its drafts. The brief asks for a live view of the loop. | Map, Plan (seeing what the loop picked) | 946 js, 795 css, 270 py | Keep. Reconcile it with Will's live view once PR #11 lands, because his trace is its data. |
| Experiments | Read the self-iteration ledger: what is in force, what was promoted and reverted, and each effect with its interval against the noise floor. | Learn | 939 js, 858 css, 69 py | Keep. It shows "not on this branch" until sam/iterate's ledger merges. |
| Memory | See what the harness believes, each belief's held-out evidence sentence, and when it must be retested. | Learn (interpret outcomes) | 1,027 js, 963 css, 74 py | Keep. It shows `lessons.md` until sam/memory merges. |
| Eval reports | Backtest AUCs and fault verdicts | Learn | was about 1,200 | Cut. Will owns eval views, and the code is on `sam/ui` for him to take. |
| Fixtures | Demo and test states a live run can't produce on demand: realistic, empty, error, and a stress scenario the tab generates | None (testing) | 923 json | Keep. The stress scenario is generated, not committed. |

```sh
cd harness/review
n() { cat "$@" | wc -l; }
n __init__.py routes.py                                                       # Server hardening and routes
n static/js/{shell,app}.js static/js/views/index.js static/js/lib/{dom,api,format,stress}.js  # Shell js
n static/css/{tokens,base,components}.css                                     # Shell css
n index.html                                                                  # Shell html
n static/js/views/queue.js static/js/components/xpost.js                      # Queue js
n static/css/views/queue.css static/css/xpost.css                             # Queue css
n checkpoint.py                                                               # Queue py
n static/js/views/loop.js static/js/lib/rounds.js                             # Loop js
n static/css/views/loop.css                                                   # Loop css
n rounds.py                                                                   # Loop py
n static/js/views/experiments.js static/js/components/ci.js                   # Experiments js
n static/css/views/experiments.css                                            # Experiments css
n experiments_api.py                                                          # Experiments py
n static/js/views/memory.js                                                   # Memory js
n static/css/views/memory.css                                                 # Memory css
n memory_api.py                                                               # Memory py
n static/fixtures/*.json                                                      # Fixtures
```

## The direction

Clearance is an air-traffic flight-strip board. Each recommended action is a strip in a bay, and the person clears it, strikes it, or releases a held one. Clearing a strip approves it (review status `approved`), striking it kills it (`killed`), and releasing a hold overrules an abstention. The page uses the controller's words and the API keeps its own. The metaphor comes from the brief: 8VC frames the harness as Boyd's OODA loop from air combat, and flight strips are what controllers use to clear aircraft one at a time under time pressure. It gives every visual device a job:

- The coloured edge of a strip is its holder and names its lane: blue for replies, amber for posts, graphite for a person's own words. A hatched edge means held back, the same hatching the team's whiteboard used for the undesigned Eval box.
- The ruled cells across the top of a selected strip are its printed fields (type, lane, mind, story, panel score, status).
- Clearing stamps the strip `CLEARED 2137` in 24-hour time, the way controllers mark strips. Striking draws a line through it.
- The selected strip sits 8px out of line, as a controller slides out the strip that needs attention.
- The readback bar at the bottom repeats what the console just did ("CLEARED reply by reply lane · hand-off out/approved-run31.json · z undo"), the way a pilot reads back a clearance.

Inside the post preview, everything switches to X's own look: palette, Chirp's fallback stack, 15/20 text, 40px avatars, X's number format and character ring. The person judges a draft the way a reader will meet it.

Type: B612 Mono sets the printed labels, field values and stamps. Airbus commissioned it for cockpit displays. Atkinson Hyperlegible Next sets everything a person reads at speed, numbers included, with tabular figures. The Braille Institute designed it so that l, 1 and I never get confused, which matters when checking handles and stats. Paths, knob names and logs use the system mono, because B612 gives "." and ":" a full cell and "action.reply.generation" reads as "action. reply. generation". Both faces are OFL and vendored under `static/fonts/`, so the console works offline.

Colour carries meaning only: the two lane colours, green and red for a controller's marks, amber for caution, grey for held. Contrast pairs are checked at WCAG AA and noted in `static/css/tokens.css`. Dark mode follows the system and uses X's "lights out" palette in the preview.

## The human checkpoints

8VC's brief gives the person a job at each step of the loop. This table says where each one happens.

| Step | The person's job | Where |
| --- | --- | --- |
| Map | Pick the conversations most aligned to the brand | Each draft shows the narrative behind it with relevance, velocity, and whether velocity came from data or a model's guess. Will's signals view will own the full picture. |
| Plan | Choose the stories | Strips name their story. The Your take bay asks for one when the post lane has nothing good. |
| Create | Supply the faces and the final edit | Inline editing on the X preview, with X's weighted character count, the gate's per-type cap, and the slop gate underlining exact spans as you type. "Use this text" swaps in any other draft the harness wrote. A take is posted verbatim. |
| Qualify | Kill anything off-message | Strike with a reason (keys 1 to 7 right after striking). Numbers and superlatives in a draft are flagged "Check before clearing". The critic's veto and the veto model's verdict are on the strip, and a person can clear over them. |
| Deploy | Approve and own relationships | Clearing writes `out/approved-run<id>.json`. Follows render as the profile being followed, and mentions show who gets tagged. |
| Learn | Interpret outcomes | Memory shows what the harness believes, each belief's held-out evidence sentence, and when it must be retested. Kill reasons, notes and edits feed sam/memory's learning. |

## How the Queue works

The Queue has five bays: To clear, Held, Your take, Cleared and Struck. Held holds the critic's vetoed drafts and the decision log's abstentions, releasable ones first. j and k move, a clears (or releases a hold, or clears a take), x strikes, e edits, z undoes the last verdict, u returns a cleared or struck strip, `[` and `]` switch bays, and `?` lists every key.

Verdicts are optimistic. The strip leaves its bay at once, focus moves to the next strip, and the request follows. If the request fails, the strip goes back with the person's edit, and the readback and a toast say why. Keyboard actions never animate, because a person does them hundreds of times a session. The Queue's only motion is the strips' entrance once per page load, the phone sheet sliding when tapped, and press feedback on buttons. Each has a reduced-motion variant.

The slop check calls the real gate (`harness.minds.gate.check`) through `POST /api/gate` on a 180ms debounce, with the limits the lanes use (`harness.minds.settings.load().gate`). The page paints the findings as highlights in a mirror layer behind the textarea, so the underline sits on the exact characters. On a branch without the gate, the editor says "Slop gate not connected" and why.

Releasing a hold writes a new action into the same run, marked `origin: "overrule"`, approves it and rewrites the hand-off file. A take is stored the same way with `mind: "human"` and `origin: "take"`. Both ids come from the run and the decision. A double click, a second release of the same hold, or a resubmitted take therefore replaces the earlier action instead of adding one.

## Stack

The page is plain ES modules served by the existing stdlib server, with no build step, no framework and no dependencies. `index.html` is the shell. `static/js/shell.js` holds a view registry and a hash router. Each view is one module under `static/js/views/` with its own stylesheet under `static/css/views/`. A view registers itself with `registerView({id, title, group, css, mount, unmount, onKey})` and needs one import line in `views/index.js`. Will's views can move in that way without touching anyone else's files.

Most of the console renders read-only data. The one stateful region, the editor, gets targeted updates so typing never loses the caret. A build step would cost every teammate a Node toolchain to change a label, for gains this console doesn't need. `lib/dom.js` provides an `html` tagged template that escapes every interpolated value, and `safeUrl` admits only http and https links from run data. Together they cover the injection risk a framework would otherwise handle. A view with heavy state shared across many components would change this call, and nothing here has one yet.

Server routes live in modules that register through `routes.py` (`@route("GET", r"/api/...")`), so each view's API is its own file. Every request must carry a `127.0.0.1` or `localhost` Host header, which defeats DNS rebinding. Every POST must be `application/json`, and an Origin header, when the browser sends one, must be local. A web page can't send JSON across origins without a CORS preflight, which this server never answers. No other site can therefore approve drafts or start paid rounds through the operator's browser. Static files resolve inside `static/` or return 404.

## Fixtures

`?fixture=realistic`, `empty` or `error` serves GETs from `static/fixtures/<name>.json` and keeps POSTs in the tab, for states a live run can't produce on demand. `?fixture=stress` builds itself in the tab from the realistic fixture (`static/js/lib/stress.js`): 160 strips, long and unbreakable text, mixed scripts, missing fields and unknown codes. A route a fixture doesn't list comes from the live server, so one fixture can stage the Queue and the Loop while the other views show what the branch really has. A run's hand-off (`/approved`) is built from the fixture's own verdicts. `?delay=1500` slows every response, to show loading states.

The realistic fixture is built from real output. The Queue's run 31 comes from the integration worktree's runs 22 and 31 (real veto verdicts and decision logs), live run 5 (ESPN and Google News through anthropic), and sam/action's `x_threads.json` (real X posts and replies from 2026-10-15). The Loop's round is the log of a real round, saved as run 14. A few reply drafts are written by hand to cover states no real run reached yet, and the real gate produced their findings. Timestamps written as `@now-38m` resolve at load, so post ages read as they did when captured.

## API routes

The first five routes are the original dashboard's and keep their shapes.

| Route | What it does |
| --- | --- |
| `GET /` | The console page |
| `GET /api/runs` | Recent runs with draft, cleared and struck counts |
| `GET /api/runs/<id>` | A run's actions, each with its review state |
| `GET /api/runs/<id>/approved` | The run's cleared actions in hand-off shape |
| `POST /api/actions/<id>/review` | A verdict: `{status, text?, note?, reasons?}`. Rewrites the hand-off file. |
| `GET /api/meta` | Provider, model, sources, kill reasons and abstention reason labels |
| `GET /api/runs/<id>/decisions` | The decision log, or `available: false` and why |
| `GET /api/runs/<id>/context` | The run's narratives and signals from its trace |
| `POST /api/runs/<id>/overrule` | Release a hold: `{decision, candidate?, text, note?}` |
| `POST /api/runs/<id>/takes` | Clear a person's answer to `ask_human`: `{decision, text, note?}` |
| `POST /api/gate` | The slop gate on `{text, kind?, context?}`. `kind` defaults to `post`. |
| `GET /api/loop` | The Loop view's board, round log and recent runs |
| `POST /api/rounds`, `POST /api/rounds/stop` | Start or stop a round. A round runs a fixed small argv with the server's own provider, and stop ends its whole process group. |
| `GET /api/experiments` | The self-iteration ledger |
| `GET /api/memory`, `GET /api/memory/notes?ids=` | Beliefs, reflections, edit procedures and outcomes; notes by id, at most 50 at a time |

## Ruled out

- **Porting the slop gate to JavaScript** for zero-latency checks. Two copies of the rules would drift, and the round trip to localhost takes a few milliseconds.
- **A React or Preact build.** See Stack.
- **Animating verdicts.** A strike line that draws for 200ms before the next strip appears would slow the one flow that has to be fast.
- **Letting the page choose the round's provider.** The lead asked that no request data reach the command line. Start the server with `HARNESS_PROVIDER=...` instead.
- **Saving draft edits before a verdict.** The store records an edit only with its verdict, so unsaved edits live in the tab until then.

## Open questions

- Dhilan owns `harness/review/` in CODEOWNERS, and Will built `/console` beside it (PR #11). This PR proposes the shell at `/` as the one app. Will's console has its own Review tab that writes the same hand-off file, and one review UI is enough. His other views can register in the shell (`registerView`), and `EXTERNAL` in `shell.js` can link `/console` until then.
- sam/action's daily budget already counts what Clearance clears, because `Budget.from_approvals` reads `out/approved-run*.json`. Its `shipped_takes` reads only the decision logs, though, so it never sees a take cleared here. Should Clearance append to the decision log, or should `shipped_takes` read the hand-off files?
- The account's own X handle is unknown, so drafts render as "Our account". Set `HARNESS_ACCOUNT_HANDLE` once 8VC says what it is.
- Kill reasons on `main` fall back to a "Reasons: ..." prefix in the note until sam/memory's `set_review(reasons=)` lands. `checkpoint.save_verdict` switches on its own, and the fallback can then go.
