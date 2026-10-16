// The Loop view's model: what a round's log says, which run the board shows, and each stage's
// state, count and time. Pure functions with no DOM or shell imports, so Node can test them
// (tests/js/loop.test.mjs). The view's markup lives in views/loop.js.

export const STAGES = ["observe", "orient", "decide", "qualify", "review", "learn"];
export const ROUND = STAGES.slice(0, 4); // what `python -m harness run` does; review and learn come after it
// trace.py rewrites out/trace-live.json at every stage boundary and LLM call. No bound was
// measured; the slowest call in out/trace-run5.json took 8.5 s, so a snapshot silent for three
// minutes is taken to belong to a killed run rather than one still working.
export const STALE_LIVE_MS = 180_000;
// trace-live.json's start is stamped by the child once Python is up, a moment after the server
// stamps the round, and the round's times are whole seconds. Two seconds of slack either side is
// a guess that covers both; nobody has measured the gap.
const SAME_RUN_SLACK_MS = 2000;
// `python -m harness run`'s --retries default (harness/cli.py), for a command that doesn't set it.
const DEFAULT_RETRIES = 2;
// trace.py's later ranking stages, on branches that have them, count toward the Qualify bay's time.
const FOLD = { tournament: "qualify", sharpen_hooks: "qualify" };

// --- Reading the round's log ----------------------------------------------------------------
// The harness prints human lines, not events. These match the prints in harness/sources,
// harness/orient.py, harness/minds and harness/qualify.py; a line that doesn't match still shows
// in the log. api.js's fixture round appends lines the same way, so both go through this parser.

export const TAG = /^\[(\w+)\]\s?(.*)$/;
const RX = {
  source: /^\[observe\] (\S+): (\d+) signals/,
  sourceFailed: /^\[observe\] (\S+) failed: (.*)$/,
  sent: /^\[orient\] sending (\d+) of (\d+) signals/,
  stale: /^\[orient\] dropped as older than (\d+)h: (.*)$/,
  picked: /^\[orient\] (\d+) narratives: (.*)$/,
  mindFailed: /^\[decide\] (\S+) failed: /,
  drafted: /^\[decide\] (\d+) minds (?:proposed|revised into) (\d+) actions/,
  kept: /^\[qualify\] kept (\d+)\/(\d+) drafts/,
  runId: /\(run (\d+)\)/,
};

export function readLines(lines = []) {
  const f = {
    sources: {}, failed: {}, sent: null, total: null, staleN: 0, staleH: null, picked: null, ids: [],
    minds: null, mindsFailed: 0, drafts: 0, decides: 0, kept: 0, scored: 0, qualifies: 0, lastKept: null,
    runId: null, seen: {}, last: {}, end: {},
  };
  for (const ln of lines) {
    const text = ln.text || "";
    const stage = ln.stage;
    if (ROUND.includes(stage)) {
      f.seen[stage] = true;
      f.last[stage] = text;
      if (stage === "observe") f.end.observe = ln.t;
    }
    let m;
    if ((m = RX.source.exec(text))) f.sources[m[1]] = +m[2];
    else if ((m = RX.sourceFailed.exec(text))) f.failed[m[1]] = m[2];
    else if ((m = RX.sent.exec(text))) {
      f.sent = +m[1];
      f.total = +m[2];
    }
    else if ((m = RX.stale.exec(text))) {
      f.staleH = +m[1];
      for (const c of m[2].matchAll(/:\s*(\d+)/g)) f.staleN += +c[1];
    } else if ((m = RX.picked.exec(text))) {
      f.picked = +m[1];
      f.end.orient = ln.t;
      f.ids = m[2].split(",").map((x) => x.trim()).filter(Boolean);
    } else if (RX.mindFailed.test(text)) f.mindsFailed++;
    else if ((m = RX.drafted.exec(text))) {
      f.minds = +m[1];
      f.drafts += +m[2];
      f.decides++;
      f.end.decide = ln.t;
    } else if ((m = RX.kept.exec(text))) {
      f.kept += +m[1];
      f.scored += +m[2];
      f.qualifies++;
      f.lastKept = +m[1];
      f.end.qualify = ln.t;
    }
    if ((m = RX.runId.exec(text))) f.runId = +m[1];
  }
  return f;
}

const NO_LINES = readLines([]);

/** True once the log shows stage `s` finished: a later stage spoke, or its summary line printed. */
export function lineDone(f, s) {
  if (s === "observe") return Boolean(f.seen.orient || f.seen.decide || f.seen.qualify);
  if (s === "orient") return f.picked != null;
  if (s === "decide") return f.decides > 0;
  return f.qualifies > 0;
}

/** The stage a round from this page is in, from its log; null once all four have reported. */
function stageFromLines(f, liveDone, retries) {
  // After the critic vetoes every draft, cli.run sends the drafts back to decide, then qualify.
  if (f.qualifies && f.decides > f.qualifies) return "qualify";
  if (f.qualifies && f.decides === f.qualifies && f.lastKept === 0 && f.decides <= retries) return "decide";
  return ROUND.find((s) => !lineDone(f, s) && !liveDone.has(s)) ?? null;
}

// --- Which run the board shows --------------------------------------------------------------

export const ts = (iso) => (iso ? Date.parse(iso) : NaN);
const sum = (xs) => xs.reduce((a, b) => a + b, 0);

function belongs(live, r) {
  const ls = ts(live?.started_at);
  if (!r || Number.isNaN(ls)) return false;
  return ls >= ts(r.started_at) - SAME_RUN_SLACK_MS && (!r.finished_at || ls <= ts(r.finished_at) + SAME_RUN_SLACK_MS);
}

function fresh(live, now) {
  const at = ts(live.started_at) + (live.elapsed_ms || 0);
  return Number.isFinite(at) && now - at < STALE_LIVE_MS;
}

/** True when the round is at least as new as the newest run in the store. */
function newest(r, run) {
  if (!run) return true;
  if (r.run_id != null) return r.run_id >= run.id;
  return ts(r.started_at) >= ts(run.started_at); // runs.started_at is when the run was saved
}

/**
 * Which run the bays show, in this order:
 *   1. a round started from this page that is still running;
 *   2. a run started from a terminal, seen through trace.py's live snapshot;
 *   3. the last page round, when no newer run has been saved since;
 *   4. the newest run in the store, from its trace file and its review counts;
 *   5. nothing yet (kind null).
 * `current` is the stage in flight, or where a failed or stopped round ended.
 */
export function boardOf(d, now = Date.now()) {
  const r = d.round || null;
  const runs = d.runs || [];
  const live = d.live || null;
  const liveMine = live && belongs(live, r) ? live : null;
  const liveAway = live && !liveMine && live.status === "running" && fresh(live, now) ? live : null;

  let kind = null;
  if (r?.status === "running") kind = "round";
  else if (liveAway) kind = "terminal";
  else if (r && newest(r, runs[0])) kind = "round";
  else if (runs.length || d.latest || d.trace) kind = "run";

  const f = kind === "round" ? readLines(r.lines) : NO_LINES;
  const lv = kind === "round" ? liveMine : kind === "terminal" ? liveAway : null;
  const status = kind === "round" ? r.status : kind === "terminal" ? "running" : kind ? "done" : null;
  const runId =
    kind === "round" ? (r.run_id ?? f.runId) : kind === "terminal" ? live.run_id : (d.latest?.run_id ?? runs[0]?.id ?? d.trace?.run_id ?? null);
  const trace = d.trace && runId != null && d.trace.run_id === runId ? d.trace : null;
  const latest = d.latest && runId != null && d.latest.run_id === runId ? d.latest : null;
  const saved = runs.find((x) => x.id === runId)?.started_at ?? null;

  let current = null;
  if (kind === "round" && status !== "done") {
    const liveDone = new Set((lv?.done || []).map((e) => e.name));
    const retries = +(/--retries (\d+)/.exec(r.command || "")?.[1] ?? DEFAULT_RETRIES);
    // All four reported: a running round is saving its drafts; a failed one broke while saving.
    current = stageFromLines(f, liveDone, retries) ?? (status === "running" ? "qualify" : "review");
  } else if (kind === "terminal") {
    const last = lv.done?.at(-1)?.name;
    current = ROUND.includes(lv.stage) && lv.stage !== last ? lv.stage : (ROUND[ROUND.indexOf(last) + 1] ?? "qualify");
  }

  const startedIso = kind === "round" ? r.started_at : kind === "terminal" ? live.started_at : null;
  const finishedIso = kind === "round" ? r.finished_at : kind === "run" ? saved : null;
  const totalMs = trace?.total_ms ?? (kind === "round" && r.finished_at ? ts(r.finished_at) - ts(r.started_at) : null);

  return { kind, status, inFlight: status === "running", current, runId, round: kind === "round" ? r : null, live: lv, f, trace, latest, saved, startedIso, finishedIso, totalMs };
}

// --- Each round stage's state, count and time -----------------------------------------------

/** done, running, waiting, failed or stopped, for one of the four round stages. */
export function coreState(b, s) {
  if (!b.kind) return "waiting";
  if (b.status === "done") return "done";
  const i = STAGES.indexOf(s);
  const c = STAGES.indexOf(b.current);
  if (i < c) return "done";
  if (i > c) return "waiting";
  return b.status === "running" ? "running" : b.status;
}

function traceCount(t, s) {
  if (s === "observe") return t.signals ?? null;
  if (s === "orient") return t.narratives?.length ?? null;
  if (s === "decide") return t.n_drafts ?? null;
  return t.kept ?? null;
}

function liveCount(done, s) {
  const mine = (done || []).filter((e) => e.name === s && e.n_out != null);
  if (!mine.length) return null;
  return s === "decide" ? sum(mine.map((e) => e.n_out)) : mine.at(-1).n_out;
}

/** Drafts written and kept, from the store's saved actions, for a run with no trace or log. */
function storeCount(latest, s) {
  if (!latest) return null;
  if (s === "decide") return latest.actions;
  if (s === "qualify") return latest.actions - latest.vetoed;
  return null;
}

export function lineCount(f, s) {
  if (s === "observe") {
    const n = Object.values(f.sources);
    return n.length || Object.keys(f.failed).length ? sum(n) : null;
  }
  if (s === "orient") return f.picked;
  if (s === "decide") return f.decides ? f.drafts : null;
  return f.qualifies ? f.kept : null;
}

/** A stage's headline number: the trace when the run has one, else trace-live, the log, the store. */
export function stageCount(b, s, state) {
  // With sam/action's lanes, decide returns only the drafts it ships, so the trace's count is
  // shipped drafts. "Drafts written" is what the minds proposed, which only the log says.
  if (s === "decide" && b.f?.decides) return lineCount(b.f, s);
  if (b.trace) return traceCount(b.trace, s);
  return liveCount(b.live?.done, s) ?? lineCount(b.f, s) ?? (state === "done" ? storeCount(b.latest, s) : null);
}

/** Seconds into the round when stage `s` started, from the log. */
function lineStart(f, s) {
  return { observe: 0, orient: f.end.observe, decide: f.end.orient, qualify: f.end.decide }[s] ?? null;
}

function lineMs(f, s) {
  if (f.decides > 1 && (s === "decide" || s === "qualify")) return null; // passes overlap after a revision
  const end = f.end[s];
  const start = lineStart(f, s);
  return end != null && start != null && end >= start ? (end - start) * 1000 : null;
}

function msOf(entries, s) {
  const mine = (entries || []).filter((e) => (FOLD[e.name] || e.name) === s);
  return mine.length ? sum(mine.map((e) => e.ms || 0)) : null;
}

/** How long a finished stage took, in ms, or null when nothing timed it. */
export function stageMs(b, s) {
  if (b.trace) return msOf(b.trace.stages, s);
  return msOf(b.live?.done, s) ?? lineMs(b.f, s);
}

/** When the running stage `s` started, as epoch ms, or null. */
export function stageSince(b, s) {
  const done = b.live?.status === "running" ? b.live.done || [] : null;
  // trace-live times every finished stage; use it once it has timed all the stages before this one.
  if (done && ROUND.slice(0, ROUND.indexOf(s)).every((x) => done.some((e) => e.name === x))) {
    return ts(b.live.started_at) + sum(done.map((e) => e.ms || 0));
  }
  const f = b.f;
  // A revision pass of decide starts when qualify reports its full veto.
  const start = s === "decide" && f.qualifies && f.qualifies >= f.decides ? f.end.qualify : lineStart(f, s);
  return start == null ? null : ts(b.startedIso) + start * 1000;
}

function cleanReason(name, why) {
  let s = String(why || "").replace(/^failed:\s*/, "");
  if (s.startsWith(`${name}: `)) s = s.slice(name.length + 2);
  return s;
}

/** Observe's per-source counts, with each failure's reason stripped of trace.py's prefixes. */
export function sourceRows(b) {
  const list = b.trace?.sources?.length ? b.trace.sources : b.live?.sources?.length ? b.live.sources : null;
  if (list) return list.map((x) => ({ name: x.name, n: x.n ?? 0, error: cleanReason(x.name, x.error) }));
  const names = [...new Set([...Object.keys(b.f.sources), ...Object.keys(b.f.failed)])];
  return names.map((name) => ({ name, n: b.f.sources[name] ?? 0, error: cleanReason(name, b.f.failed[name]) }));
}
