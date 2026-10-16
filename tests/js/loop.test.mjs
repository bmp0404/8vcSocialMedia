// node --test tests/js/  (tests/test_review_js.py runs this under pytest when node is installed)
// The Loop view's model has one right answer per input: what a round's log says, which run the
// board shows, and which stage is in flight. The realistic fixture's round is the real run-5 stdout.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { boardOf, coreState, readLines, sourceRows, stageCount, stageMs, stageSince } from "../../harness/review/static/js/lib/rounds.js";

const fixture = JSON.parse(readFileSync(new URL("../../harness/review/static/fixtures/parts/loop.realistic.json", import.meta.url), "utf8"));
const real = fixture.routes["GET /api/loop"];
const NOW = Date.parse("2026-10-16T02:30:00Z");
const run6 = { id: 6, started_at: "2026-10-16T02:00:00+00:00" };

/** A page round with these [stage, text] lines, one second apart. */
function round(lines, status = "running", extra = {}) {
  return {
    round: {
      id: "r",
      started_at: "2026-10-16T02:29:00+00:00",
      command: "python -m harness run --narratives 2 --per-mind 1 --retries 1",
      status,
      lines: lines.map(([stage, text], i) => ({ t: i, stage, text })),
      run_id: null,
      finished_at: status === "running" ? null : "2026-10-16T02:29:30+00:00",
      ...extra,
    },
    live: null,
    runs: [run6],
    latest: null,
    trace: null,
  };
}

const L = {
  observe: ["observe", "[observe] sample: 4 signals"],
  sending: ["orient", "[orient] sending 4 of 4 signals (967 chars)"],
  picked: ["orient", "[orient] 2 narratives: a, b"],
  drafted: ["decide", "[decide] 5 minds proposed 5 actions"],
  vetoAll: ["qualify", "[qualify] kept 0/5 drafts"],
  keptOne: ["qualify", "[qualify] kept 1/5 drafts"],
  revised: ["decide", "[decide] 5 minds revised into 5 actions"],
};

const ROUND_COUNTS = (b) => ["observe", "orient", "decide", "qualify"].map((s) => stageCount(b, s, coreState(b, s)));
const ROUND_MS = (b) => ["observe", "orient", "decide", "qualify"].map((s) => stageMs(b, s));

const live = {
  status: "running",
  run_id: null,
  started_at: "2026-10-16T02:29:40+00:00",
  elapsed_ms: 15000,
  stage: "decide",
  done: [
    { name: "observe", ms: 1000, n_out: 4 },
    { name: "orient", ms: 2000, n_out: 2 },
  ],
};

test("the run-5 log reads as the trace says", () => {
  const f = readLines(real.round.lines);
  assert.deepEqual(f.sources, { espn_news: 52, google_news: 51 });
  assert.match(f.failed.x_targets, /every read failed/);
  assert.deepEqual([f.sent, f.total, f.staleN, f.staleH], [68, 103, 35, 72]);
  assert.deepEqual([f.picked, f.ids], [2, ["clark-boston-knee-surgery", "cunningham-reese-dream-discourse"]]);
  assert.deepEqual([f.minds, f.drafts, f.decides], [5, 3, 1]);
  assert.deepEqual([f.kept, f.scored, f.qualifies, f.lastKept], [1, 3, 1, 1]);
  assert.equal(f.runId, 14);
});

test("a decide revision adds its drafts, and qualify passes add up", () => {
  const f = readLines(round([L.observe, L.picked, L.drafted, L.vetoAll, L.revised, L.keptOne]).round.lines);
  assert.deepEqual([f.decides, f.drafts, f.qualifies, f.kept, f.scored, f.lastKept], [2, 10, 2, 1, 10, 1]);
});

test("the realistic fixture shows its saved round, matched to its trace and store counts", () => {
  const b = boardOf(real, NOW);
  assert.deepEqual([b.kind, b.status, b.runId, b.current, Boolean(b.trace), Boolean(b.latest)], ["round", "done", 14, null, true, true]);
  assert.deepEqual(ROUND_COUNTS(b), [103, 2, 3, 1]);
  assert.deepEqual(ROUND_MS(b), [3322, 7815, 8505, 8697]);
});

test("nothing run yet shows an empty board", () => {
  assert.equal(boardOf({ round: null, live: null, runs: [], latest: null, trace: null }, NOW).kind, null);
});

test("the stage in flight follows the log", () => {
  const at = (lines) => boardOf(round(lines), NOW).current;
  assert.equal(at([]), "observe");
  assert.equal(at([L.observe]), "observe");
  assert.equal(at([L.observe, L.sending]), "orient");
  assert.equal(at([L.observe, L.picked]), "decide");
  assert.equal(at([L.observe, L.picked, L.drafted]), "qualify");
});

test("a full veto sends the round back to decide, once per allowed retry", () => {
  const at = (lines) => boardOf(round(lines), NOW).current;
  assert.equal(at([L.observe, L.picked, L.drafted, L.vetoAll]), "decide");
  assert.equal(at([L.observe, L.picked, L.drafted, L.vetoAll, L.revised]), "qualify");
  // --retries 1 allows no third pass; the round is saving.
  assert.equal(at([L.observe, L.picked, L.drafted, L.vetoAll, L.revised, L.vetoAll]), "qualify");
});

test("a failed round marks the stage it broke in", () => {
  const b = boardOf(round([L.observe, L.picked, [null, "Traceback (most recent call last):"]], "failed"), NOW);
  assert.deepEqual([b.kind, b.status, b.current], ["round", "failed", "decide"]);
  assert.deepEqual(["observe", "orient", "decide", "qualify"].map((s) => coreState(b, s)), ["done", "done", "failed", "waiting"]);
  assert.equal(boardOf(round([L.observe, L.picked, L.drafted, L.keptOne], "failed"), NOW).current, "review");
});

test("a round older than the newest saved run gives way to that run", () => {
  const older = round([L.observe, L.picked, L.drafted, L.keptOne, [null, "Top 1 recommendations (run 5) -> x"]], "done", { run_id: 5 });
  assert.equal(boardOf(older, NOW).kind, "run");
  assert.equal(boardOf({ ...older, runs: [{ id: 5, started_at: run6.started_at }] }, NOW).kind, "round");
});

test("a fresh live snapshot without a page round is a terminal run", () => {
  const b = boardOf({ round: null, live, runs: [run6] }, NOW);
  assert.deepEqual([b.kind, b.status, b.current], ["terminal", "running", "decide"]);
  // Between stages trace.py still names the stage that just closed; the next one is in flight.
  assert.equal(boardOf({ round: null, live: { ...live, stage: "orient" }, runs: [run6] }, NOW).current, "decide");
  assert.equal(stageSince(b, "decide"), Date.parse(live.started_at) + 3000);
});

test("a live snapshot silent for three minutes is ignored", () => {
  const stale = { ...live, started_at: "2026-10-16T02:20:00+00:00", elapsed_ms: 10_000 };
  assert.equal(boardOf({ round: null, live: stale, runs: [run6] }, NOW).kind, "run");
});

test("a running page round wins over another run's live snapshot", () => {
  const b = boardOf({ ...round([L.observe]), live: { ...live, started_at: "2026-10-16T02:28:00+00:00" } }, NOW);
  assert.deepEqual([b.kind, b.live], ["round", null]);
});

test("without trace-live times, a running stage is timed from the log", () => {
  const b = boardOf(round([L.observe, L.picked]), NOW);
  // decide started when orient printed its picks, one second into the round.
  assert.equal(stageSince(b, "decide"), Date.parse("2026-10-16T02:29:00Z") + 1000);
  const revising = boardOf(round([L.observe, L.picked, L.drafted, L.vetoAll]), NOW);
  assert.equal(stageSince(revising, "decide"), Date.parse("2026-10-16T02:29:00Z") + 3000);
});

test("source failures lose trace.py's prefixes", () => {
  const rows = sourceRows(boardOf(real, NOW));
  assert.deepEqual(
    rows.map((r) => [r.name, r.n, r.error.slice(0, 18)]),
    [
      ["espn_news", 52, ""],
      ["google_news", 51, ""],
      ["x_targets", 0, "every read failed;"],
    ],
  );
});

test("later ranking stages count toward qualify's time", () => {
  const b = boardOf({ ...real, trace: { ...real.trace, stages: [...real.trace.stages, { name: "tournament", ms: 1000, n_out: 1 }] } }, NOW);
  assert.equal(stageMs(b, "qualify"), 8697 + 1000);
});

test("a run with no trace still shows drafts written and kept from the store", () => {
  const b = boardOf({ round: null, live: null, runs: [{ id: 2, started_at: run6.started_at }], latest: { run_id: 2, actions: 5, vetoed: 1, pending: 1 }, trace: null }, NOW);
  assert.deepEqual(ROUND_COUNTS(b), [null, null, 5, 4]);
});
