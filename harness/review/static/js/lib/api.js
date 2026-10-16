// HTTP client for the review server, with a fixture mode for states the harness can't produce
// on demand: an empty store, a broken server, slow loads, very long text, hundreds of drafts.
//
// Open the page with ?fixture=realistic|empty|error|stress to serve GETs from
// static/fixtures/<name>.json (stress is generated from realistic) and keep POSTs in memory for
// this tab. ?delay=<ms> slows every response, to see loading states.

export class ApiError extends Error {
  constructor(status, message, path) {
    super(message);
    this.status = status;
    this.path = path;
  }
}

const params = new URLSearchParams(location.search);
const delayMs = Math.min(10_000, Math.max(0, +(params.get("delay") || 0)));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let fixture = null; // { name, routes, errors, posts }

export const fixtureName = () => fixture?.name ?? null;

export async function initFixture() {
  const name = params.get("fixture");
  if (!name) return null;
  if (!/^[\w-]+$/.test(name)) throw new ApiError(400, `bad fixture name "${name}"`, "fixture");
  // The stress scenario is generated from the realistic one rather than committed.
  const file = name === "stress" ? "realistic" : name;
  const res = await fetch(`/static/fixtures/${file}.json`);
  if (!res.ok) throw new ApiError(res.status, `no fixture named "${name}"`, "fixture");
  const data = await res.json();
  let routes = relativeTimes(data.routes || {}, Date.now());
  if (name === "stress") routes = (await import("./stress.js")).stressFrom(routes);
  fixture = { name, routes, errors: { ...data.errors } };
  return fixture;
}

const clone = (x) => (x == null ? x : structuredClone(x));

// Fixture timestamps can be written relative to page load ("@now-38m", "@now-2h", "@now-3d"),
// so post ages read the way they did when the data was captured.
const REL = /^@now([+-]\d+(?:\.\d+)?)([smhd])$/;
const UNIT = { s: 1e3, m: 6e4, h: 3.6e6, d: 8.64e7 };
function relativeTimes(value, now) {
  if (typeof value === "string") {
    const m = REL.exec(value);
    return m ? new Date(now + +m[1] * UNIT[m[2]]).toISOString() : value;
  }
  if (Array.isArray(value)) return value.map((v) => relativeTimes(v, now));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, relativeTimes(v, now)]));
  return value;
}

async function fixtureGet(path) {
  const key = `GET ${path.split("?")[0]}`;
  if (fixture.errors[key]) {
    const e = fixture.errors[key];
    throw new ApiError(e.status || 500, e.error || "fixture error", path);
  }
  if (key in fixture.routes) return clone(fixture.routes[key]);
  // A route the fixture doesn't cover comes from the live server, so one fixture can stage the
  // Queue and Loop while the other views show whatever this branch really has.
  return liveGet(path);
}

// POSTs in fixture mode change the fixture's own data, so the page behaves as if the server took them.
const fixturePosts = [
  [
    /^\/api\/actions\/(\w+)\/review$/,
    (m, body) => {
      for (const [key, value] of Object.entries(fixture.routes)) {
        if (!/^GET \/api\/runs\/\d+$/.test(key)) continue;
        const a = value.find((x) => x.id === m[1]);
        if (!a) continue;
        a.review =
          body.status === "pending"
            ? { status: "pending", text: null, note: "", reasons: [], reviewed_at: null }
            : { status: body.status, text: body.text ?? null, note: body.note || "", reasons: body.reasons || [], reviewed_at: new Date().toISOString() };
        return { ok: true, export: `out/approved-run${key.split("/").pop()}.json (fixture: not written)` };
      }
      throw new ApiError(404, "unknown action", m[0]);
    },
  ],
  [
    /^\/api\/runs\/(\d+)\/(overrule|takes)$/,
    (m, body) => {
      const list = fixture.routes[`GET /api/runs/${m[1]}`];
      const decisions = fixture.routes[`GET /api/runs/${m[1]}/decisions`]?.decisions || [];
      const d = decisions[body.decision];
      if (!d) throw new ApiError(400, "unknown decision", m[0]);
      const take = m[2] === "takes";
      const id = `${take ? "take" : "ovr"}${m[1]}${body.decision}`;
      const existing = list.findIndex((a) => a.id === id);
      const action = {
        id,
        type: take ? "post" : d.lane === "reply" ? "reply" : "post",
        text: body.text,
        mind: take ? "human" : (d.candidates?.[body.candidate]?.mind ?? `${d.lane}_lane`),
        narrative_id: take ? d.opportunity : "",
        rationale: take ? `A person's answer to: ${d.question}` : `Overruled abstention (${d.reason}): ${d.detail}`,
        target_url: !take && d.lane === "reply" ? d.opportunity : null,
        target_handle: null,
        predicted_score: 0,
        panel: {},
        vetoed: false,
        critique: "",
        lane: d.lane,
        origin: take ? "take" : "overrule",
        question: d.question,
        review: { status: "approved", text: null, note: take ? "Written by a person." : "Overruled.", reviewed_at: new Date().toISOString() },
      };
      if (existing >= 0) list[existing] = action;
      else list.push(action);
      return { ok: true, action, export: "(fixture: not written)" };
    },
  ],
  [/^\/api\/gate$/, (m, body) => realOrDemoGate(body.text || "", body.kind || "post")],
  [/^\/api\/rounds$/, () => simulateRound()],
  [/^\/api\/rounds\/stop$/, () => stopSimulatedRound()],
];

async function fixturePost(path, body) {
  for (const [re, fn] of fixturePosts) {
    const m = re.exec(path);
    if (m) return fn(m, body);
  }
  throw new ApiError(404, `fixture mode can't handle POST ${path}`, path);
}

export async function get(path) {
  if (delayMs) await sleep(delayMs);
  if (fixture) return fixtureGet(path);
  return liveGet(path);
}

async function liveGet(path) {
  let res;
  try {
    res = await fetch(path, { headers: { Accept: "application/json" } });
  } catch (e) {
    throw new ApiError(0, "Can't reach the review server. Is `python -m harness review` still running?", path);
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, data?.error || `${res.status} ${res.statusText}`, path);
  return data;
}

export async function post(path, body = {}) {
  if (delayMs) await sleep(Math.min(delayMs, 800));
  if (fixture) return fixturePost(path, body);
  let res;
  try {
    res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError(0, "Can't reach the review server. Is `python -m harness review` still running?", path);
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, data?.error || `${res.status} ${res.statusText}`, path);
  return data;
}

// --- Fixture stand-ins for server work ------------------------------------------------------

// A few of sam/action's gate rules, so fixture mode can show findings with offsets. The real
// gate runs server-side in harness.minds.gate; this copy exists only for demos.
const DEMO_RULES = [
  ["em_dash", /—|\s–\s|\s--\s/g, () => "dash"],
  ["not_x_but_y", /\bnot (?:just|only|merely)\b[^.!?]{0,80}\bbut\b/gi, (m) => `'${m}'`],
  ["ai_vocab", /\b(?:delve|testament|tapestry|pivotal|showcas\w*|unwavering|game[- ]changer|let that sink in|make no mistake)\b/gi, (m) => `'${m}'`],
  ["placeholder", /\[(?:verify|insert|todo|tbd)[^\]]*\]/gi, (m) => `unresolved placeholder '${m}'`],
];
const DEMO_LIMITS = { reply: 280, quote: 280, post: 280, mention: 280 }; // GateLimits.max_chars on sam/action

// The gate is a pure function, so fixture mode asks the real server's gate first and only falls
// back to the demo rules when this branch doesn't have harness.minds.gate.
async function realOrDemoGate(text, kind) {
  try {
    const res = await fetch("/api/gate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, kind }),
    });
    const data = await res.json();
    if (res.ok && data.available) return data;
  } catch {}
  return simulatedGate(text, kind);
}

function simulatedGate(text, kind) {
  const findings = [];
  for (const [rule, re, detail] of DEMO_RULES) {
    for (const m of text.matchAll(re)) findings.push({ rule, detail: detail(m[0]), start: m.index, end: m.index + m[0].length });
  }
  const limit = DEMO_LIMITS[kind] ?? 280;
  if ([...text].length > limit) findings.push({ rule: "length", detail: `${[...text].length} chars (max ${limit} for ${kind})`, start: null, end: null });
  return { available: true, simulated: true, passed: findings.length === 0, limit, findings };
}

let roundTimers = [];
function stopSimulatedRound() {
  const loop = fixture.routes["GET /api/loop"];
  if (!loop?.round || loop.round.status !== "running") throw new ApiError(409, "No round is running.", "/api/rounds/stop");
  roundTimers.forEach(clearTimeout);
  roundTimers = [];
  Object.assign(loop.round, { status: "stopped", exit_code: -15, finished_at: new Date().toISOString() });
  if (loop.live) loop.live.status = "stopped";
  return clone(loop.round);
}

function simulateRound() {
  const loop = fixture.routes["GET /api/loop"];
  if (!loop) throw new ApiError(404, "this fixture has no loop data", "/api/rounds");
  if (loop.round?.status === "running") throw new ApiError(409, "A round is already running. Wait for it or stop it first.", "/api/rounds");
  const script = [
    [400, "observe", "[observe] espn_news: 27 signals"],
    [900, "observe", "[observe] google_news: 41 signals"],
    [1700, "observe", "[observe] x_targets: 9 signals"],
    [4200, "orient", "[orient] 2 narratives: wnba-finals-tipoff, boston-knee-surgery-support"],
    [7400, "decide", "[decide] 5 minds proposed 6 actions"],
    [10200, "qualify", "[qualify] kept 5/6 drafts"],
    [10800, null, "Top 5 recommendations (run 15) -> out/recommendations-20261015-221210.json"],
  ];
  const started = new Date().toISOString();
  loop.round = { id: "round-demo", started_at: started, provider: loop.provider, sources: loop.sources, command: loop.command, status: "running", stage: "observe", lines: [], run_id: null, exit_code: null, finished_at: null };
  loop.live = { status: "running", stage: "observe", started_at: started, elapsed_ms: 0, done: [], llm_calls: 0 };
  roundTimers.forEach(clearTimeout);
  const t0 = Date.now();
  roundTimers = script.map(([at, stage, text], i) =>
    setTimeout(() => {
      const r = loop.round;
      r.lines.push({ t: at / 1000, stage, text });
      if (stage) r.stage = stage;
      loop.live.stage = stage || loop.live.stage;
      loop.live.elapsed_ms = Date.now() - t0;
      loop.live.llm_calls = Math.max(loop.live.llm_calls, i * 3 - 3);
      if (i === script.length - 1) {
        r.status = "done";
        r.stage = "review";
        r.run_id = 15;
        r.exit_code = 0;
        r.finished_at = new Date().toISOString();
        loop.live.status = "done";
      }
    }, at),
  );
  return clone(loop.round);
}
