// The Loop view: one round of the harness as six bays on the board (observe, orient, decide,
// qualify, a person's review, learn), what is running now, the round's log, the control that
// starts a round, the newest run's narratives, and recent runs.
//
// Data comes from GET /api/loop (harness/review/rounds.py). The view polls it every 700 ms while
// a round runs and every 5 s otherwise, only while mounted and while the tab is visible. A poll
// patches only the regions whose markup changed and the log only appends, so the log's scroll
// position and the run button's focus survive. Relative times ("4 min ago") are filled in after
// each patch, so a minute ticking over doesn't rebuild a region that holds a focused link.
// What the log says and which run the bays show come from lib/rounds.js; this file is the markup.

import { registerView } from "../shell.js";
import { $, $$, esc, html, on, setHTML } from "../lib/dom.js";
import { ago, clock, compact, duration, fixed } from "../lib/format.js";
import { boardOf, coreState, lineDone, lineCount, ROUND, sourceRows, STAGES, stageCount, stageMs, stageSince, TAG, ts } from "../lib/rounds.js";

const FAST_MS = 700;
const SLOW_MS = 5000;
// Poll fast for this long after a start, before the board can show the new round running.
const KICK_FAST_MS = 4000;
// Clicks this soon after the run button changes between Start and Stop are ignored, so a second
// click meant for the old button can't stop a round it just started or start a paid one.
const FLIP_GUARD_MS = 1000;
const NAMES = { observe: "Observe", orient: "Orient", decide: "Decide", qualify: "Qualify", review: "Review", learn: "Learn" };
const WHAT = {
  observe: ["signal in", "signals in"],
  orient: ["narrative picked", "narratives picked"],
  decide: ["draft written", "drafts written"],
  qualify: ["draft kept", "drafts kept"],
  review: ["draft waiting for a person", "drafts waiting for a person"],
  learn: ["lesson on file", "lessons on file"],
};
// What a stage does, shown in its bay before the round reaches it.
const ABOUT = {
  observe: "Reads each source for new signals.",
  orient: "Groups signals into narratives and scores them.",
  decide: "Each mind drafts posts and replies.",
  qualify: "The panel scores drafts; the critic vetoes weak ones.",
  review: "A person clears or strikes each draft in the Queue.",
  learn: html`Rewrites <code>memory/lessons.md</code> from recorded outcomes.`,
};
const DOING = {
  observe: "Reading sources",
  orient: "Grouping signals into narratives",
  decide: "The minds are writing drafts",
  qualify: "The panel and the critic are scoring drafts",
};
const GROUPS = [
  { id: "round", label: "Round", cmd: "python -m harness run", stages: ROUND },
  { id: "checkpoint", label: "Checkpoint", aside: "You, in the Queue", stages: ["review"] },
  { id: "memory", label: "Memory", cmd: "python -m harness learn", stages: ["learn"] },
];
const LAMP = { done: "on", running: "busy", yours: "busy", failed: "bad" };
const SAY = { done: "done", running: "running", waiting: "waiting", failed: "failed", stopped: "stopped", yours: "waiting for you" };
const BASIS = { data: "measured from signal times", llm: "the model's estimate" };
const TYPES = { post: ["post", "posts"], reply: ["reply", "replies"], quote: ["quote", "quotes"], mention: ["mention", "mentions"] };

// --- What each bay says ---------------------------------------------------------------------

function elapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

const words = (n, [one, many]) => (n === 1 ? one : many);
const untag = (text) => {
  const t = TAG.exec(text || "")?.[2] ?? text ?? "";
  return t.charAt(0).toUpperCase() + t.slice(1);
};
const agoSpan = (iso) => html`<span data-ago="${iso}"></span>`;

function lastLine(r) {
  const lines = r.lines;
  for (let i = lines.length - 1; i >= 0; i--) if (lines[i].text.trim()) return lines[i].text.trim();
  return "";
}

function observeDetail(b, running) {
  const rows = sourceRows(b);
  const pending = running ? (b.round?.sources || []).filter((n) => !rows.some((x) => x.name === n)) : [];
  if (!rows.length && !pending.length) return running ? html`<p>${DOING.observe}</p>` : "";
  const failed = rows.filter((x) => x.error);
  const all = [...failed, ...rows.filter((x) => !x.error), ...pending.map((name) => ({ name, wait: true }))];
  const shown = all.slice(0, 5);
  const first = failed[0];
  return html`<ul class="loop-src">
      ${shown.map(
        (x) => html`<li class="${x.error ? "bad" : x.wait ? "wait" : ""}">
          <span title="${x.name}">${x.name}</span><span>${x.error ? "failed" : x.wait ? "reading" : compact(x.n)}</span>
        </li>`,
      )}
    </ul>
    ${all.length > shown.length ? html`<p class="loop-more">and ${all.length - shown.length} more</p>` : ""}
    ${first ? html`<p class="loop-why" title="${first.name}: ${first.error}"><span class="mono">${first.name}</span> ${first.error}</p>` : ""}`;
}

function typesText(byType) {
  return Object.entries(byType)
    .sort((a, b) => b[1] - a[1])
    .map(([t, n]) => `${n} ${words(n, TYPES[t] || [t, `${t}s`])}`)
    .join(", ");
}

function doneDetail(b, s, n) {
  const f = b.f;
  if (s === "observe") return observeDetail(b, false);
  if (s === "orient") {
    if (f.sent != null) return html`<p>Read ${f.sent} of ${f.total} signals${f.staleN ? `; dropped ${f.staleN} older than ${f.staleH}h` : ""}</p>`;
    if (f.ids.length) return html`<p class="loop-ids">${f.ids.join(", ")}</p>`;
    const rel = (b.trace?.narratives || []).map((x) => x.relevance).filter((x) => x != null);
    return rel.length ? html`<p>Relevance ${fixed(Math.max(...rel))} to ${fixed(Math.min(...rel))}</p>` : "";
  }
  if (s === "decide") {
    const calls = b.trace?.calls_by_stage?.decide?.n;
    const parts = [
      f.minds ? `${f.minds} ${words(f.minds, ["mind", "minds"])}` : calls ? `${calls} ${words(calls, ["model call", "model calls"])}` : null,
      b.latest ? typesText(b.latest.by_type) : null,
      f.decides > 1 ? "revised after a full veto" : null,
      f.mindsFailed ? `${f.mindsFailed} ${words(f.mindsFailed, ["mind", "minds"])} failed` : null,
    ].filter(Boolean);
    return parts.length ? html`<p>${parts.join(" · ")}</p>` : "";
  }
  const total = b.trace?.n_drafts ?? (f.qualifies ? f.scored : (b.latest?.actions ?? null));
  if (total == null || n == null) return "";
  const cut = total - n;
  return html`<p>${cut > 0 ? `The critic vetoed ${cut} of ${total}` : `The critic kept all ${total}`}</p>`;
}

function coreBay(b, s, now) {
  const state = coreState(b, s);
  if (state === "waiting") return { state, n: null, time: "Waiting", detail: html`<p>${ABOUT[s]}</p>` };
  const f = b.f;
  const n = stageCount(b, s, state);
  if (state === "running") {
    const since = stageSince(b, s);
    const retrying = s === "decide" && f.qualifies > 0 && f.lastKept === 0;
    const detail =
      s === "observe"
        ? observeDetail(b, true)
        : html`<p>${retrying ? "Revising after the critic vetoed every draft" : f.last[s] && !lineDone(f, s) ? untag(f.last[s]) : DOING[s]}</p>`;
    return { state, n, time: since == null ? "Running" : `Running ${elapsed(now - since)}`, detail };
  }
  if (state === "stopped") return { state, n, time: "Stopped", detail: html`<p>Stopped from this page.</p>` };
  if (state === "failed") {
    const why = lastLine(b.round) || `Exited with code ${b.round.exit_code}`;
    return { state, n, time: "Failed", detail: html`<p class="loop-fail" title="${why}">${why}</p>` };
  }
  const ms = stageMs(b, s);
  const unrecorded = n == null && b.kind === "run" && !b.trace ? html`<p>Run ${b.runId} left no trace file, so this wasn't recorded.</p>` : "";
  return { state, n, time: ms == null ? "" : duration(ms), detail: doneDetail(b, s, n) || unrecorded };
}

function reviewBay(b, d) {
  const settled = b.kind === "run" || (b.kind === "round" && b.status === "done");
  if (settled && b.runId != null) {
    const lt = b.latest;
    // Without the store's counts (fixture rounds), every draft qualify kept is still unreviewed.
    const n = lt ? lt.to_review : b.f.runId === b.runId ? lineCount(b.f, "qualify") : null;
    const held = lt ? lt.pending - n : 0;
    const counts = lt ? [`${lt.approved} cleared`, `${lt.killed} struck`, held > 0 ? `${held} vetoed` : null].filter(Boolean) : [];
    const href = `#/queue?run=${b.runId}`;
    return {
      state: n > 0 ? "yours" : "done",
      n,
      time: n > 0 ? "Your turn" : "",
      detail: html`<p>Run ${b.runId}${b.saved || b.finishedIso ? html`, saved ${agoSpan(b.saved || b.finishedIso)}` : ""}</p>${counts.length ? html`<p>${counts.map((c, i) => html`${i ? " · " : ""}<span class="loop-nowrap">${c}</span>`)}</p>` : ""}`,
      link:
        n > 0
          ? html`<a class="btn btn-ink btn-small" href="${href}" data-fk="review">Review in Queue</a>`
          : html`<a class="btn btn-quiet btn-small" href="${href}" data-fk="review">Open in Queue</a>`,
    };
  }
  const other = d.latest && d.latest.run_id !== b.runId && d.latest.to_review > 0 ? d.latest : null;
  const broke = b.kind === "round" && !b.inFlight && b.current === "review";
  const text = b.inFlight ? "Drafts land here when qualify finishes." : b.kind === "round" ? "This round saved no drafts." : ABOUT.review;
  return {
    state: broke ? b.status : "waiting",
    n: null,
    time: broke ? (b.status === "failed" ? "Failed" : "Stopped") : "Waiting",
    detail: html`<p>${text}</p>`,
    link: other
      ? html`<a class="btn btn-quiet btn-small" href="#/queue?run=${other.run_id}" data-fk="review-other">Run ${other.run_id}: ${other.to_review} waiting</a>`
      : "",
  };
}

function learnBay(b, d) {
  const L = d.learn;
  const n = L.lessons_file ? L.bullets : null;
  const updated = ts(L.updated_at);
  const end = ts(b.finishedIso);
  const done = b.status === "done" && updated > end;
  let text;
  if (!L.lessons_file) text = html`<p>No <code>memory/lessons.md</code> yet. Learn writes it once outcomes are recorded.</p>`;
  else if (!n) text = html`<p>No lessons yet. Learn writes them once outcomes are recorded.</p>`;
  else text = html`<p>Rewritten ${L.updated_at ? agoSpan(L.updated_at) : "at an unknown time"}${done ? ", after this run" : ""}.</p>${done ? "" : html`<p>Runs again after outcomes are recorded.</p>`}`;
  return {
    state: done ? "done" : "waiting",
    n,
    time: done ? "" : "Waiting",
    detail: text,
    link: html`<a class="btn btn-quiet btn-small" href="#/memory" data-fk="memory">Open Memory</a>`,
  };
}

function bayOf(stage, b, d, now) {
  if (stage === "review") return reviewBay(b, d);
  if (stage === "learn") return learnBay(b, d);
  return coreBay(b, stage, now);
}

function statusLine(b, now) {
  if (!b.kind) return html`No rounds yet. Start one below, or run <code>python -m harness run</code> in a terminal.`;
  const at = b.current;
  if (b.kind === "terminal") return html`A run started from a terminal is in ${at}, ${elapsed(now - ts(b.startedIso))} in.`;
  if (b.inFlight) return html`Round running for ${elapsed(now - ts(b.startedIso))}, now in ${at}.`;
  const when = b.finishedIso ? html` ${agoSpan(b.finishedIso)}` : "";
  if (b.status === "failed") return html`Round failed at ${at}${when}${b.round?.exit_code != null ? `, exit code ${b.round.exit_code}` : ""}.`;
  if (b.status === "stopped") return html`Round stopped at ${at}${when}.`;
  const took = Number.isFinite(b.totalMs) ? ` in ${duration(b.totalMs)}` : "";
  return html`Run ${b.runId ?? "with no id"} finished${when}${took}.`;
}

// --- Markup ---------------------------------------------------------------------------------

function bayFrame(stage, i, loading) {
  return html`<li class="loop-bay" data-stage="${stage}" data-state="${loading ? "loading" : "waiting"}" style="--i: ${i}">
    <i class="loop-bar" aria-hidden="true"></i>
    <div class="loop-bay-head">
      <span class="lamp" aria-hidden="true" data-r="lamp"></span>
      <h3 class="loop-bay-name">${NAMES[stage]}<span class="sr-only" data-r="state"></span></h3>
      <span class="loop-bay-time num" data-r="time"></span>
    </div>
    <div class="loop-bay-body" data-r="body">
      ${loading ? html`<span class="skeleton" style="width: 42%; height: 22px"></span><span class="skeleton" style="width: 70%"></span><span class="skeleton" style="width: 85%"></span>` : ""}
    </div>
  </li>`;
}

// The bays settle in once per page load. Coming back to the view (often by `g l`) shows them at once.
let entered = false;

function frame(loading) {
  let i = 0;
  const sk = (w) => html`<span class="skeleton" style="width: ${w}"></span>`;
  return html`<div class="loop">
    <header class="viewhead">
      <div>
        <h1>Loop</h1>
        <p class="sub" data-r="sub">${loading ? sk("260px") : ""}</p>
      </div>
    </header>
    <p class="sr-only" aria-live="polite" data-r="say"></p>
    <div class="loop-pad loop-notices" data-r="notices"></div>
    <section class="loop-pad loop-board" aria-labelledby="loop-board-h" ${loading || entered ? "" : html`data-enter`}>
      <h2 class="sr-only" id="loop-board-h">Stages of a round</h2>
      <div class="loop-grid">
        ${GROUPS.map(
          (g) => html`<div class="loop-group" data-group="${g.id}">
            <p class="loop-grouphead"><span class="label">${g.label}</span><span class="loop-aside">${g.cmd ? html`<code>${g.cmd}</code>` : g.aside}</span></p>
            <ol class="loop-bays" start="${STAGES.indexOf(g.stages[0]) + 1}">${g.stages.map((s) => bayFrame(s, i++, loading))}</ol>
          </div>`,
        )}
        <p class="loop-track"><span>Decide reads these lessons in the next round</span></p>
      </div>
      <p class="loop-note">Learn's lessons go to decide's minds in the next round.</p>
    </section>
    <div class="loop-pad loop-lower">
      <section class="panel loop-run" aria-labelledby="loop-run-h">
        <div class="section">
          <div class="section-head"><h2 id="loop-run-h">Run a round</h2></div>
          <p class="loop-run-what">Runs observe, orient, decide and qualify once. Its drafts then wait in the Queue.</p>
          ${loading
            ? html`${sk("90%")}${sk("60%")}${sk("75%")}`
            : html`<dl class="kv">
                  <dt>Command</dt>
                  <dd><code data-r="cmd"></code></dd>
                  <dt>Provider</dt>
                  <dd data-r="provider"></dd>
                  <dt>Sources</dt>
                  <dd data-r="sources"></dd>
                </dl>
                <div data-r="cost" id="loop-cost"></div>
                <div class="loop-run-act">
                  <button class="btn btn-ink" type="button" data-act="run" data-role="start" aria-describedby="loop-cost">Start round</button>
                  <span class="loop-run-state num" data-r="runstate"></span>
                </div>
                <div class="loop-run-msg" data-r="runmsg"></div>`}
        </div>
      </section>
      <section class="panel loop-log" aria-labelledby="loop-log-h">
        <div class="section">
          <div class="section-head"><h2 id="loop-log-h">Round log</h2><span class="aside" data-r="logmeta"></span></div>
          ${loading
            ? html`${sk("80%")}${sk("65%")}${sk("72%")}`
            : html`<p class="loop-empty" data-r="logempty">No round started from this page yet. Its log streams here while it runs.</p>
                <div class="loop-logwrap" data-r="logwrap" hidden>
                  <div class="loop-logbox" tabindex="0" role="log" aria-live="off" aria-labelledby="loop-log-h" data-r="logbox">
                    <ol class="loop-lines" data-r="lines"></ol>
                  </div>
                  <button class="btn btn-small loop-jump" type="button" data-act="jump" hidden>Jump to latest</button>
                </div>`}
        </div>
      </section>
    </div>
    <section class="loop-pad loop-sec" aria-labelledby="loop-nar-h">
      <div class="section-head"><h2 id="loop-nar-h">Narratives</h2><span class="aside" data-r="narmeta"></span></div>
      <div data-r="narratives">${loading ? html`<div class="panel loop-sk">${sk("55%")}${sk("85%")}</div>` : ""}</div>
    </section>
    <section class="loop-pad loop-sec" aria-labelledby="loop-runs-h">
      <div class="section-head"><h2 id="loop-runs-h">Recent runs</h2><span class="aside" data-r="runsmeta"></span></div>
      <div data-r="runs">${loading ? html`<div class="panel loop-sk">${sk("70%")}${sk("70%")}${sk("70%")}</div>` : ""}</div>
    </section>
  </div>`;
}

function lineHTML(ln) {
  const m = TAG.exec(ln.text || "");
  const text = m ? m[2] : ln.text || "";
  const bad = /\bfailed\b|Traceback|Error\b|error:/.test(ln.text || "");
  return html`<li class="${bad ? "bad" : ""}"><span class="t">${ln.t == null ? "" : fixed(ln.t, 1)}</span><span class="s">${m ? m[1] : ""}</span><span class="x">${text}</span></li>`;
}

function score(label, x, note) {
  const v = typeof x === "number" ? Math.min(1, Math.max(0, x)) : null;
  return html`<div class="loop-score">
    <dt class="label">${label}</dt>
    <dd class="num">${v == null ? "–" : fixed(x, 2)}</dd>
    <dd class="meter" aria-hidden="true"><i style="width: ${v == null ? 0 : (v * 100).toFixed(1)}%"></i></dd>
    ${note ? html`<dd class="loop-basis">${note}</dd>` : ""}
  </div>`;
}

function narrativesHTML(d) {
  const t = d.trace;
  if (!t) {
    return d.latest
      ? html`<p class="loop-empty">Run ${d.latest.run_id} left no trace file (<code>out/trace-run${d.latest.run_id}.json</code>), so its narratives aren't on record.</p>`
      : html`<p class="loop-empty">No narratives yet. Orient picks them when a round runs.</p>`;
  }
  if (!t.narratives?.length) return html`<p class="loop-empty">Orient picked no narratives in run ${t.run_id}.</p>`;
  return html`<ol class="panel loop-nars">
    ${t.narratives.map(
      (n) => html`<li class="loop-nar">
        <div class="loop-nar-main">
          <h3 class="loop-nar-title" dir="auto">${n.title || n.id || "Untitled narrative"}</h3>
          ${n.angle ? html`<p class="loop-nar-angle"><span class="label">Angle</span> <span dir="auto">${n.angle}</span></p>` : ""}
        </div>
        <dl class="loop-nar-nums">${score("Relevance", n.relevance)}${score("Velocity", n.velocity, BASIS[n.velocity_basis])}</dl>
      </li>`,
    )}
  </ol>`;
}

function runsHTML(d, b) {
  const runs = d.runs || [];
  if (!runs.length) return html`<p class="loop-empty">No runs in the store yet. Each round saves one.</p>`;
  return html`<div class="panel loop-tablewrap" tabindex="0" role="region" data-fk="runs-table" aria-label="Recent runs, scrolls sideways">
    <table class="loop-runs">
      <thead>
        <tr>
          <th scope="col">Run</th><th scope="col">Saved</th><th scope="col">Provider</th><th scope="col">Sources</th>
          <th scope="col" class="n">Drafts</th><th scope="col" class="n">Cleared</th><th scope="col" class="n">Struck</th><th scope="col" class="n">Unreviewed</th>
        </tr>
      </thead>
      <tbody>
        ${runs.map((r) => {
          const all = r.n_actions;
          const ok = r.n_approved ?? 0;
          const no = r.n_killed ?? 0;
          return html`<tr ${r.id === b.runId ? html`aria-current="true"` : ""}>
            <th scope="row"><a href="#/queue?run=${r.id}" data-fk="run-${r.id}">Run ${r.id}</a></th>
            <td title="${clock(r.started_at, true)}">${r.started_at ? agoSpan(r.started_at) : "–"}</td>
            <td>${r.provider || "–"}</td>
            <td class="src">${String(r.sources || "").split(",").filter(Boolean).join(", ") || "–"}</td>
            <td class="n num">${all}</td><td class="n num">${ok}</td><td class="n num">${no}</td><td class="n num">${Math.max(0, all - ok - no)}</td>
          </tr>`;
        })}
      </tbody>
    </table>
  </div>`;
}

function costHTML(d) {
  if (d.provider === "mock") return html`<p class="loop-cost">The mock provider answers locally, so a round costs nothing.</p>`;
  const t = d.trace;
  return html`<p class="notice warn loop-cost">
    <span>Rounds on <b>${d.provider}</b> spend credits.${t?.llm_calls ? ` Run ${t.run_id} made ${t.llm_calls} model calls.` : ""}</span>
  </p>`;
}

// --- Rendering ------------------------------------------------------------------------------

const lastMarkup = new WeakMap();

/** Replace a region's markup only when it changed. If focus was inside, it goes back to the element
 *  with the same data-fk, else the region's first control, else the region itself.
 *  A plain string is text and gets escaped; markup comes from `html`. */
function patch(node, markup) {
  const s = typeof markup === "string" ? esc(markup) : String(markup ?? "");
  if (!node || lastMarkup.get(node) === s) return;
  const had = node.contains(document.activeElement);
  const fk = had ? document.activeElement.dataset.fk : null;
  node.innerHTML = s;
  lastMarkup.set(node, s);
  if (had) {
    const target = (fk && node.querySelector(`[data-fk="${CSS.escape(fk)}"]`)) || node.querySelector("a, button") || node;
    if (target === node && !node.hasAttribute("tabindex")) node.tabIndex = -1;
    target.focus({ preventScroll: true });
  }
}

function refreshAgo(root) {
  for (const el of $$("[data-ago]", root)) {
    const t = ago(el.dataset.ago);
    if (el.textContent !== t) el.textContent = t;
  }
}

function renderBay(s, stage, v) {
  const bay = s.bays[stage];
  if (bay.li.dataset.state !== v.state) bay.li.dataset.state = v.state;
  bay.lamp.className = `lamp ${LAMP[v.state] || ""}`;
  bay.state.textContent = `, ${SAY[v.state]}`;
  if (bay.time.textContent !== v.time) bay.time.textContent = v.time;
  patch(
    bay.body,
    html`<p class="loop-fig"><span class="loop-n num ${v.n == null ? "is-none" : ""}">${v.n == null ? "–" : compact(v.n)}</span><span class="loop-what">${words(v.n, WHAT[stage])}</span></p>
      ${v.detail ? html`<div class="loop-detail">${v.detail}</div>` : ""}${v.link || ""}`,
  );
}

function renderNotices(s) {
  const items = [];
  if (s.pollError) {
    items.push(html`<p class="notice warn" role="alert">
      <span><b>The last refresh failed.</b> ${s.pollError.message} The board shows the previous answer and keeps retrying.</span>
    </p>`);
  }
  patch(s.r.notices, html`${items}`);
}

function renderRun(s, d, b, now) {
  const R = s.r;
  // One token per span, so the command wraps between arguments rather than inside "--per-mind".
  patch(R.cmd, html`${d.command.split(" ").map((t, i) => html`${i ? " " : ""}<span class="loop-nowrap">${t}</span>`)}`);
  patch(R.provider, d.provider === "mock" ? html`mock <span class="muted">· free</span>` : d.provider || "–");
  patch(R.sources, d.sources.join(", ") || "none set");
  patch(R.cost, costHTML(d));

  const role = b.kind === "round" && b.inFlight ? "stop" : "start";
  const btn = R.btn;
  // The click's request has shown up once a new round appears (a fast round may finish between
  // polls, so the button never shows Stop) or the button flips.
  if (s.posting && (d.round?.id ?? null) !== s.postedRoundId) s.posting = null;
  if (btn.dataset.role !== role) {
    btn.dataset.role = role;
    s.flippedAt = Date.now();
    s.posting = null;
    // A 409 explains the round that is now running; keep it until that round ends.
    if (role === "start") s.runMsg = null;
  }
  const label = s.posting ? (s.posting === "stop" ? "Stopping…" : "Starting…") : role === "stop" ? "Stop round" : "Start round";
  if (btn.textContent !== label) btn.textContent = label;
  btn.className = role === "stop" ? "btn" : "btn btn-ink";
  if (s.posting) btn.setAttribute("aria-disabled", "true");
  else btn.removeAttribute("aria-disabled");

  const state = role === "stop" ? `Running ${elapsed(now - ts(b.startedIso))}` : "";
  if (R.runstate.textContent !== state) R.runstate.textContent = state;

  const msgs = [];
  if (b.kind === "terminal") msgs.push(html`<p class="notice"><span>A run started from a terminal is in flight. Starting a round now runs both at once.</span></p>`);
  if (s.runMsg) msgs.push(html`<p class="notice warn" role="alert"><span>${s.runMsg}</span></p>`);
  patch(R.runmsg, html`${msgs}`);
}

function renderLog(s, d) {
  const r = d.round || null;
  const R = s.r;
  const key = r?.id ?? null;
  if (key !== s.logKey) {
    R.lines.replaceChildren();
    s.logCount = 0;
    s.logKey = key;
    s.pinned = true;
    R.jump.hidden = true;
  }
  R.logempty.hidden = Boolean(r);
  R.logwrap.hidden = !r;
  patch(
    R.logmeta,
    r ? html`<span class="mono">${r.id}</span>${r.run_id != null ? ` · run ${r.run_id}` : ""} · ${r.lines.length} ${words(r.lines.length, ["line", "lines"])}` : "",
  );
  const lines = r?.lines || [];
  if (lines.length < s.logCount) {
    R.lines.replaceChildren();
    s.logCount = 0;
  }
  if (lines.length === s.logCount) return;
  R.lines.insertAdjacentHTML("beforeend", lines.slice(s.logCount).map(lineHTML).join(""));
  s.logCount = lines.length;
  // The ResizeObserver in mount() keeps a pinned log at the bottom as it grows.
  if (!s.pinned) R.jump.hidden = false;
}

function announce(s, prev, b) {
  let say = "";
  // Only in-flight stage changes go to this region; how a round ended is said once, by the readback.
  if (b.inFlight) say = `${NAMES[b.current]} running.`;
  if (!prev) {
    s.said = say;
    return;
  }
  if (say && say !== s.said) {
    s.said = say;
    s.r.say.textContent = say;
  }
  if (prev.inFlight && !b.inFlight && b.kind === "round") {
    if (b.status === "done") s.ctx.readback(html`Round finished: run <b>${b.runId}</b> is waiting in the Queue.`);
    else if (b.status === "failed") s.ctx.readback(html`<span class="bad">Round failed</span> at ${b.current}. The log shows why.`);
    else s.ctx.readback(html`Round stopped at ${b.current}.`);
  }
}

function apply(s, d) {
  const now = Date.now();
  const prev = s.board;
  const b = boardOf(d, now);
  s.data = d;
  s.board = b;
  patch(s.r.sub, statusLine(b, now));
  renderNotices(s);
  for (const stage of STAGES) renderBay(s, stage, bayOf(stage, b, d, now));
  renderRun(s, d, b, now);
  renderLog(s, d);
  patch(s.r.narmeta, d.trace ? `Run ${d.trace.run_id} · ${d.trace.narratives.length} picked from ${compact(d.trace.signals)} signals` : "");
  patch(s.r.narratives, narrativesHTML(d));
  patch(s.r.runsmeta, d.runs?.length ? "Each run's drafts open in the Queue" : "");
  patch(s.r.runs, runsHTML(d, b));
  refreshAgo(s.el);
  announce(s, prev, b);
}

// --- Polling and actions --------------------------------------------------------------------

function schedule(s) {
  clearTimeout(s.timer);
  if (!s.alive || document.visibilityState !== "visible") return;
  const fast = s.board?.inFlight || Date.now() - s.kickedAt < KICK_FAST_MS;
  s.timer = setTimeout(() => poll(s), fast ? FAST_MS : SLOW_MS);
}

async function poll(s) {
  if (!s.alive) return;
  if (s.fetching) {
    s.again = true;
    return;
  }
  s.fetching = true;
  clearTimeout(s.timer);
  try {
    const d = await s.ctx.api.get("/api/loop");
    if (!s.alive) return;
    s.pollError = null;
    apply(s, d);
  } catch (e) {
    if (!s.alive) return;
    s.pollError = e;
    renderNotices(s);
  } finally {
    s.fetching = false;
    const again = s.again;
    s.again = false;
    if (again && s.alive && document.visibilityState === "visible") poll(s);
    else schedule(s);
  }
}

async function onRun(s, e) {
  const btn = s.r.btn;
  // A double-click, or a click landing just after the button turned from Start into Stop (or
  // back), must not stop the round it started or start a paid one.
  if (e.detail > 1 || s.posting || Date.now() - s.flippedAt < FLIP_GUARD_MS) return;
  const role = btn.dataset.role;
  s.posting = role;
  s.postedRoundId = s.data?.round?.id ?? null;
  s.runMsg = null;
  renderRun(s, s.data, s.board, Date.now());
  try {
    if (role === "stop") {
      await s.ctx.api.post("/api/rounds/stop");
      s.ctx.readback(html`Stopping the round.`);
    } else {
      const r = await s.ctx.api.post("/api/rounds");
      s.kickedAt = Date.now();
      s.ctx.readback(html`Round started with <b>${r.provider}</b> on ${(r.sources || []).join(", ")}.`);
    }
    // Stay "Starting…" / "Stopping…" until a poll shows the round flipped (renderRun clears it),
    // so a second click can't post again into the gap and leave a misleading 409 alert.
    if (s.alive) poll(s);
  } catch (err) {
    if (!s.alive) return;
    s.runMsg = err.status === 409 ? err.message : `${role === "stop" ? "Couldn't stop" : "Couldn't start"} the round: ${err.message}`;
    s.posting = null;
    renderRun(s, s.data, s.board, Date.now());
    poll(s);
  }
}

let S = null;

registerView({
  id: "loop",
  title: "Loop",
  group: "loop",
  css: "/static/css/views/loop.css",
  async mount(el, ctx) {
    const s = (S = { el, ctx, alive: true, timer: 0, kickedAt: 0, flippedAt: 0, logKey: undefined, logCount: 0, pinned: true, offs: [] });
    setHTML(el, frame(true));
    const d = await ctx.api.get("/api/loop");
    if (!s.alive) return;
    setHTML(el, frame(false));
    entered = true;
    const r = (name) => $(`[data-r="${name}"]`, el);
    s.r = Object.fromEntries(
      ["sub", "say", "notices", "cmd", "provider", "sources", "cost", "runstate", "runmsg", "logmeta", "logempty", "logwrap", "logbox", "lines", "narmeta", "narratives", "runsmeta", "runs"].map((k) => [k, r(k)]),
    );
    s.r.btn = $('[data-act="run"]', el);
    s.r.jump = $('[data-act="jump"]', el);
    s.bays = {};
    for (const li of $$(".loop-bay", el)) {
      s.bays[li.dataset.stage] = { li, lamp: $('[data-r="lamp"]', li), state: $('[data-r="state"]', li), time: $('[data-r="time"]', li), body: $('[data-r="body"]', li) };
    }
    s.offs.push(
      on(el, "click", '[data-act="run"]', (e) => onRun(s, e)),
      on(el, "click", '[data-act="jump"]', () => {
        // The button hides once the log is pinned, so focus goes to the log it just scrolled.
        s.r.logbox.focus({ preventScroll: true });
        s.r.logbox.scrollTop = s.r.logbox.scrollHeight;
      }),
    );
    const onScroll = () => {
      const box = s.r.logbox;
      s.pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
      if (s.pinned) s.r.jump.hidden = true;
    };
    s.r.logbox.addEventListener("scroll", onScroll, { passive: true });
    // Keep a pinned log at its bottom whenever it or its lines change size: new lines, a resize,
    // and the first paint, which can land before this view's stylesheet gives the box its height.
    const pin = new ResizeObserver(() => {
      if (s.pinned) s.r.logbox.scrollTop = s.r.logbox.scrollHeight;
    });
    pin.observe(s.r.logbox);
    pin.observe(s.r.lines);
    s.offs.push(() => pin.disconnect());
    const onVisible = () => (document.visibilityState === "visible" ? poll(s) : clearTimeout(s.timer));
    document.addEventListener("visibilitychange", onVisible);
    s.offs.push(() => document.removeEventListener("visibilitychange", onVisible));
    apply(s, d);
    schedule(s);
  },
  unmount() {
    if (!S) return;
    S.alive = false;
    clearTimeout(S.timer);
    for (const off of S.offs) off();
    S = null;
  },
});
