import { handleFromUrl } from "./format.js";

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

let fixture = null; // { name, routes, errors }

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

const clone = structuredClone;

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
  const approved = /^GET \/api\/runs\/(\d+)\/approved$/.exec(key);
  if (approved && fixture.routes[`GET /api/runs/${approved[1]}`]) return approvedActions(fixture.routes[`GET /api/runs/${approved[1]}`]);
  if (key in fixture.routes) return clone(fixture.routes[key]);
  // A route the fixture doesn't cover comes from the live server, so a fixture can stage one view
  // while the others show whatever this branch really has.
  return liveGet(path);
}

/** The hand-off shape memory.approved_actions builds, from the fixture's own verdicts. */
function approvedActions(actions) {
  return actions
    .filter((a) => a.review?.status === "approved")
    .map(({ review, ...a }) => ({
      ...a,
      ...(review.text ? { original_text: a.text, text: review.text } : {}),
      review_note: review.note,
      approved_at: review.reviewed_at,
    }));
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
      const cand = body.candidate == null ? {} : d.candidates?.[body.candidate];
      if (!cand) throw new ApiError(400, "unknown candidate", m[0]);
      // The same rule as post_overrule in checkpoint.py.
      const type = take ? "post" : cand.type || (d.lane === "reply" ? "reply" : handleFromUrl(d.opportunity) ? "quote" : "post");
      const id = `${take ? "take" : "ovr"}${m[1]}${body.decision}`;
      const existing = list.findIndex((a) => a.id === id);
      const target = type === "reply" || type === "quote" ? d.opportunity : null;
      const action = {
        id,
        type,
        text: body.text,
        mind: take ? "human" : (cand.mind ?? `${d.lane}_lane`),
        narrative_id: take ? d.opportunity : "",
        rationale: take ? `A person's answer to: ${d.question}` : `Overruled abstention (${d.reason}): ${d.detail}`,
        target_url: target,
        target_handle: handleFromUrl(target),
        predicted_score: 0,
        panel: {},
        vetoed: false,
        critique: "",
        lane: take ? "post" : d.lane,
        origin: take ? "take" : "overrule",
        ...(take ? { question: d.question } : { overrule: { decision: body.decision, candidate: body.candidate ?? null, reason: d.reason } }),
        review: { status: "approved", text: null, note: body.note || (take ? "Written by a person." : "Overruled."), reasons: [], reviewed_at: new Date().toISOString() },
      };
      if (existing >= 0) list[existing] = action;
      else list.push(action);
      return { ok: true, action, export: "(fixture: not written)" };
    },
  ],
  [/^\/api\/gate$/, (m, body) => realOrDemoGate(body.text || "", body.kind || "post")],
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

export async function post(path, body = {}) {
  if (delayMs) await sleep(delayMs);
  if (fixture) return fixturePost(path, body);
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
}

const liveGet = (path) => request(path, { headers: { Accept: "application/json" } });

async function request(path, init) {
  let res;
  try {
    res = await fetch(path, init);
  } catch {
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
