// Queue: the human checkpoint. Every recommended action waits here as a strip until a person
// clears it (approve), strikes it (kill) or edits it; abstentions the harness held back can be
// released; questions the post lane asks a person are answered here.
//
// Verdicts are optimistic: the strip leaves its bay at once and the request follows. A failed
// request puts it back and says why. Keyboard actions never animate.

import { registerView, setHints } from "../shell.js";
import { $, $$, html, isTyping, on, safeUrl, setHTML } from "../lib/dom.js";
import { ago, clock, compact, fixed, handleFromUrl, human, isXPost, minutesSince, stampTime, X_LIMIT, xAge, xLength } from "../lib/format.js";
import { avatar, charRing, profileCard, targetCard, targetOf, threadReplies, xText } from "../components/xpost.js";

// Mirrors harness/minds/schema.py ABSTAIN_REASONS (sam/action) for branches where the server
// can't import it; /api/meta's abstain_reasons wins when present.
const ABSTAIN = {
  no_target: "observe offered no live X post to answer",
  stale_target: "the post is too old for a reply to be seen",
  no_angle: "nothing specific to add to this post",
  gate_rejected: "every draft failed the slop gate",
  judge_rejected: "the best draft didn't clear the judge's bar",
  budget_exhausted: "the run or daily action budget is spent",
  unverifiable: "the draft needs a fact we can't check",
  off_mission: "the story doesn't fit the mission",
  mute_risk: "any reply risks reading as cruel or opportunistic",
};

const BAYS = [
  ["clear", "To clear"],
  ["held", "Held"],
  ["take", "Your take"],
  ["cleared", "Cleared"],
  ["struck", "Struck"],
];
const TEXT_KINDS = new Set(["reply", "quote", "post", "mention"]);
// The detail becomes a modal sheet below this width. It must match the breakpoint in queue.css.
const PHONE = "(max-width: 860px)";

const S = {
  el: null,
  ctx: null,
  runs: [],
  runId: null,
  actions: [],
  decisions: null,
  decisionsReason: "",
  context: null,
  contextReason: "",
  loading: true,
  error: null,
  bay: "clear",
  sel: null,
  edits: new Map(), // entry key -> text the person is editing
  notes: new Map(), // entry key -> note
  reasons: new Map(), // action id -> Set of kill reason codes
  reasonTimers: new Map(), // action id -> pending save of its kill reasons
  undo: [], // [{ id, key, prev, status, edit }], newest last
  lastStruck: null, // the action struck most recently, which number keys tag until another is selected
  gateCache: new Map(),
  gateTimer: 0,
  ageTimer: 0,
  detailOpen: false,
  enter: false, // play the strip entrance once per loaded run
  bayExplicit: false, // the URL named a bay; keep it even when empty
};
let entered = false; // the strips' entrance plays once per page load
let unwire = null;

const reasonsMeta = () => S.ctx.meta?.kill_reasons || {};
const abstainLabel = (code) => S.ctx.meta?.abstain_reasons?.[code] || ABSTAIN[code] || human(code);

// --- Data -----------------------------------------------------------------------------------

async function loadRuns(preferred) {
  S.runs = await S.ctx.api.get("/api/runs");
  if (!S.el) return;
  const id = S.runs.some((r) => r.id === +preferred) ? +preferred : S.runs[0]?.id ?? null;
  if (id != null) await loadRun(id);
  else {
    S.loading = false;
    render();
  }
}

async function loadRun(id) {
  S.runId = id;
  S.loading = true;
  S.error = null;
  render();
  const { api } = S.ctx;
  const [actions, decisions, context] = await Promise.allSettled([
    api.get(`/api/runs/${id}`),
    api.get(`/api/runs/${id}/decisions`),
    api.get(`/api/runs/${id}/context`),
  ]);
  // The person left the view or picked another run while this one loaded.
  if (S.runId !== id || !S.el) return;
  if (actions.status === "rejected") {
    S.loading = false;
    S.error = actions.reason;
    return render();
  }
  S.actions = actions.value;
  S.decisions = decisions.status === "fulfilled" && decisions.value.available ? decisions.value.decisions.map(withFindings) : null;
  S.decisionsReason = decisions.status === "rejected" ? decisions.reason.message : decisions.value.reason || "";
  S.context = null;
  if (context.status === "fulfilled" && context.value.available) {
    const c = context.value;
    const targets = new Map();
    for (const n of c.narratives) for (const t of n.reply_targets || []) if (t.url) targets.set(t.url, t);
    S.context = {
      started_at: c.started_at,
      narratives: new Map(c.narratives.map((n) => [n.id, n])),
      signals: new Map(c.signals.filter((s) => s.url).map((s) => [s.url, s])),
      targets,
    };
  }
  S.contextReason = context.status === "rejected" ? context.reason.message : context.value?.reason || "";
  S.loading = false;
  S.enter = !entered; // the board loads with strips once per page load, not on every return
  entered = true;
  S.undo = [];
  S.lastStruck = null;
  S.ctx.setParams({ run: id });
  const bays = groups();
  // A bay named in the URL stays, empty or not; otherwise open the first bay with work in it.
  if (!S.bayExplicit && !bays[S.bay].length) S.bay = BAYS.find(([b]) => bays[b].length)?.[0] || "clear";
  S.bayExplicit = false;
  if (!findEntry(S.sel)) S.sel = bays[S.bay][0]?.key ?? null;
  render();
}

/** Older decision logs list a candidate's gate findings as rule names; newer ones as objects. */
function withFindings(d) {
  if (!d.candidates) return d;
  const one = (f) => (typeof f === "string" ? { rule: f, detail: "" } : f);
  return { ...d, candidates: d.candidates.map((c) => (c.gate ? { ...c, gate: c.gate.map(one) } : c)) };
}

// --- Entries and bays -----------------------------------------------------------------------

const sentence = (t) => (/[.!?]$/.test(t.trim()) ? t.trim() : `${t.trim()}.`);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

const laneOf = (a) => a.lane || (a.mind === "human" ? "human" : a.type === "reply" || (a.type === "quote" && a.mind === "reply_lane") ? "reply" : "post");
const textOf = (a) => a.review?.text ?? a.text;
const statusOf = (a) => a.review?.status || "pending";
/** A draft that passed both the gate and the veto: the only kind the editor may start with. */
const clean = (c) => c.text && !(c.gate || []).length && !vetoProblem(c.veto);

function groups() {
  const g = { clear: [], held: [], take: [], cleared: [], struck: [] };
  for (const a of S.actions) {
    const st = statusOf(a);
    const e = { key: `a:${a.id}`, kind: "action", a };
    if (st === "approved") g.cleared.push(e);
    else if (st === "killed") g.struck.push(e);
    else if (a.vetoed) g.held.push({ ...e, veto: true });
    else g.clear.push(e);
  }
  const released = new Set(S.actions.filter((a) => a.origin === "overrule").map((a) => a.overrule?.decision));
  // Keys carry the run, so an unsaved edit or note never follows the person into another run.
  (S.decisions || []).forEach((d, i) => {
    const key = `d:${S.runId}:${i}`;
    // A hold a person already overruled is resolved; its released draft sits in Cleared.
    if (d.tool === "abstain" && !released.has(i)) g.held.push({ key, kind: "abstain", d, i });
    else if (d.tool === "ask_human") {
      const answer = S.actions.find((a) => a.origin === "take" && a.question === d.question);
      if (!answer) g.take.push({ key, kind: "ask", d, i });
    }
  });
  // Releasable holds first: a vetoed draft or an abstention with drafts to pick from.
  const weight = (e) => (e.veto ? 0 : (e.d?.candidates || []).some((c) => c.text) ? 1 : 2);
  g.held.sort((x, y) => weight(x) - weight(y));
  return g;
}

function findEntry(key) {
  if (!key) return null;
  for (const list of Object.values(groups())) {
    const e = list.find((x) => x.key === key);
    if (e) return e;
  }
  return null;
}

const visible = () => groups()[S.bay];
const selected = () => visible().find((e) => e.key === S.sel) || null;

function decisionFor(a) {
  return (S.decisions || []).find((d) => d.action_id === a.id) || null;
}

function narrativeOf(id) {
  return S.context?.narratives.get(id) || null;
}

// --- Rendering ------------------------------------------------------------------------------

function render() {
  if (!S.el) return;
  if (!S.el.querySelector(".q")) {
    setHTML(
      S.el,
      html`<div class="q">
        <header class="viewhead q-head" id="q-head"></header>
        <div class="bays" role="tablist" aria-label="Bays" id="q-bays"></div>
        <div class="q-body">
          <section class="q-list" id="q-list" role="tabpanel" aria-label="Strips"></section>
          <section class="q-detail" id="q-detail" aria-label="Selected strip"></section>
        </div>
      </div>`,
    );
  }
  renderHead();
  renderBays();
  renderList();
  renderDetail();
  S.ctx.refreshNav();
}

function renderHead() {
  const run = S.runs.find((r) => r.id === S.runId);
  setHTML(
    $("#q-head"),
    html`<div>
        <h1>Queue</h1>
        <p class="sub">${run ? html`Run ${run.id} · ${run.provider} · ${run.sources} · started ${ago(run.started_at)}` : S.loading ? "Loading runs…" : "No runs yet"}</p>
      </div>
      <div class="actions">
        ${S.runs.length
          ? html`<label class="sr-only" for="q-run">Run</label>
              <select id="q-run" class="select">
                ${S.runs.map(
                  (r) => html`<option value="${r.id}" ${r.id === S.runId ? "selected" : ""}>
                    Run ${r.id} · ${clock(r.started_at, true)} · ${plural(r.n_actions, "draft")}${r.n_approved ? ` · ${r.n_approved} cleared` : ""}
                  </option>`,
                )}
              </select>
              <button class="btn btn-quiet" type="button" data-act="copy" title="Copy the approved hand-off JSON for the posting bot">Copy hand-off</button>`
          : ""}
      </div>`,
  );
}

function renderBays() {
  const g = groups();
  setHTML(
    $("#q-bays"),
    html`${BAYS.map(([b, label]) => {
      const n = g[b].length;
      const attn = (b === "clear" || b === "take") && n > 0;
      return html`<button class="bay ${attn ? "attn" : ""}" role="tab" type="button" data-bay="${b}" aria-selected="${String(b === S.bay)}" tabindex="${b === S.bay ? 0 : -1}" aria-controls="q-list">
        <span>${label}</span><span class="n">${n}</span>
      </button>`;
    })}`,
  );
}

function scoreOf(a) {
  const p = Object.values(a.panel || {}).filter((v) => typeof v === "number");
  if (!p.length) return null;
  const mean = p.reduce((s, v) => s + v, 0) / p.length;
  return { mean, lo: Math.min(...p), hi: Math.max(...p), n: p.length };
}

function stripHtml(e) {
  const isSel = e.key === S.sel;
  if (e.kind === "action") {
    const a = e.a;
    const st = statusOf(a);
    const lane = laneOf(a);
    const t = a.target_url ? targetOf(a, S.context) : null;
    const n = narrativeOf(a.narrative_id);
    const sc = scoreOf(a);
    const verdict = a.draft_meta?.judge;
    return html`<button class="strip" type="button" role="option" data-key="${e.key}" data-lane="${lane}" data-status="${st}" ${e.veto ? "data-held" : ""} aria-selected="${String(isSel)}" tabindex="${isSel ? 0 : -1}">
      <span class="strip-top">
        <span class="label">${a.type}</span>
        <span class="where">${human(a.mind)}${n ? ` · ${n.title}` : a.narrative_id ? ` · ${a.narrative_id}` : ""}</span>
        ${sc ? html`<span class="strip-score" title="Simulated panel, ${sc.n} personas: mean ${Math.round(sc.mean * 100)}, range ${Math.round(sc.lo * 100)}–${Math.round(sc.hi * 100)}">${Math.round(sc.mean * 100)}<small>${Math.round(sc.lo * 100)}–${Math.round(sc.hi * 100)}</small></span>` : ""}
      </span>
      <span class="strip-text">${a.type === "follow" ? `Follow @${a.target_handle}` : textOf(a)}</span>
      <span class="strip-foot">
        ${t && a.type !== "follow" ? html`<span>↳ @${t.author}${t.followers != null ? ` · ${compact(t.followers)}` : ""}</span>` : ""}
        ${e.veto ? html`<span class="warn-text">critic veto</span>` : ""}
        ${verdict && verdict.passed === false ? html`<span class="warn-text">veto failed</span>` : ""}
        ${a.review?.text ? html`<span>edited</span>` : ""}
        ${a.origin === "take" ? html`<span>your words</span>` : a.origin === "overrule" ? html`<span>released by a person</span>` : ""}
        ${st === "approved" ? html`<span class="stamp cleared" title="Cleared at ${clock(a.review.reviewed_at, true)}">Cleared ${stampTime(a.review.reviewed_at)}</span>` : ""}
        ${st === "killed" ? html`<span class="stamp struck" title="Struck at ${clock(a.review.reviewed_at, true)}">Struck ${stampTime(a.review.reviewed_at)}</span>` : ""}
      </span>
    </button>`;
  }
  if (e.kind === "abstain") {
    const d = e.d;
    const n = (d.candidates || []).filter((c) => c.text).length;
    const target = isXPost(d.opportunity) ? handleFromUrl(d.opportunity) : null;
    return html`<button class="strip" type="button" role="option" data-key="${e.key}" data-lane="${d.lane}" data-held aria-selected="${String(isSel)}" tabindex="${isSel ? 0 : -1}">
      <span class="strip-top"><span class="label">Held · ${d.lane}</span><span class="where">${human(d.reason)}</span></span>
      <span class="strip-text">${abstainLabel(d.reason)}${d.detail ? `: ${d.detail}` : ""}</span>
      <span class="strip-foot">
        ${target ? html`<span>↳ @${target}</span>` : ""}
        <span>${n ? `${plural(n, "draft")} to choose from` : "no drafts"}</span>
      </span>
    </button>`;
  }
  const d = e.d;
  const n = narrativeOf(d.opportunity);
  return html`<button class="strip" type="button" role="option" data-key="${e.key}" data-lane="human" aria-selected="${String(isSel)}" tabindex="${isSel ? 0 : -1}">
    <span class="strip-top"><span class="label">Your take</span><span class="where">${n?.title || d.opportunity}</span></span>
    <span class="strip-text">${d.question.split(". Got a")[0].split("? ")[0]}</span>
    <span class="strip-foot"><span>${(d.candidates || []).length} angles from the minds</span></span>
  </button>`;
}

function renderList() {
  const list = $("#q-list");
  if (S.loading && !S.actions.length) {
    setHTML(list, html`<div class="q-strips" aria-busy="true">${[0, 1, 2, 3].map(() => html`<div class="strip skeleton-strip"><span class="skeleton" style="width:40%"></span><span class="skeleton"></span><span class="skeleton" style="width:70%"></span></div>`)}</div>`);
    return;
  }
  if (S.error) {
    setHTML(list, html`<div class="state error" role="alert"><span class="label">Error ${S.error.status || ""}</span><h2>${S.error.message}</h2><button class="btn btn-quiet" type="button" data-act="retry">Try again</button></div>`);
    return;
  }
  if (!S.runs.length) {
    setHTML(
      list,
      html`<div class="state">
        <span class="label">Empty board</span>
        <h2>No runs to review yet</h2>
        <p>A round writes its drafts here. Start one from the Loop view, or run <code>uv run python -m harness run --narratives 2 --per-mind 1</code>.</p>
        <a class="btn btn-quiet" href="#/loop">Open the Loop</a>
      </div>`,
    );
    return;
  }
  const g = groups();
  const items = g[S.bay];
  const notices = [];
  if (S.bay === "held" && !S.decisions && S.decisionsReason) notices.push(S.decisionsReason);
  if (!items.length) {
    const empty = {
      clear: ["Nothing waiting", g.cleared.length || g.struck.length ? "Every draft in this run has a verdict. The hand-off file has the cleared ones." : "This run produced no drafts to clear."],
      held: ["Nothing held back", S.decisions ? "The harness acted on every opportunity it had, and the critic vetoed nothing." : "No critic vetoes in this run."],
      take: ["No questions for you", S.decisions ? "The post lane shipped a draft this run, so it didn't ask for your take." : "This run's decide stage doesn't ask for takes."],
      cleared: ["Nothing cleared yet", "Clear a strip with A, or the Clear button."],
      struck: ["Nothing struck", "Strike a strip with X when it's off-message, shaky or reads as AI."],
    }[S.bay];
    const next =
      S.bay === "clear" && (g.held.length || g.take.length)
        ? html`<div class="q-next">
            ${g.take.length ? html`<button class="btn btn-quiet" type="button" data-goto="take">${plural(g.take.length, "question")} for you</button>` : ""}
            ${g.held.length ? html`<button class="btn btn-quiet" type="button" data-goto="held">${plural(g.held.length, "hold")} you could overrule</button>` : ""}
            ${g.cleared.length ? html`<button class="btn btn-quiet" type="button" data-act="copy">Copy hand-off (${g.cleared.length} cleared)</button>` : ""}
          </div>`
        : "";
    setHTML(list, html`${notices.map((n) => html`<p class="notice q-notice">${n}</p>`)}<div class="state"><h2>${empty[0]}</h2><p>${empty[1]}</p>${next}</div>`);
    return;
  }
  const enter = S.enter;
  S.enter = false;
  setHTML(list, html`${notices.map((n) => html`<p class="notice q-notice">${n}</p>`)}<div class="q-strips ${enter ? "enter" : ""}" role="listbox" aria-label="${BAYS.find(([b]) => b === S.bay)[1]}">${items.map(stripHtml)}</div>`);
}

// --- Detail ---------------------------------------------------------------------------------

function renderDetail() {
  const box = $("#q-detail");
  const e = selected();
  const q = S.el.querySelector(".q");
  if (S.detailOpen && !e) {
    S.detailOpen = false;
    setInert(false);
  }
  q.toggleAttribute("data-detail-open", S.detailOpen && !!e);
  const phone = matchMedia(PHONE).matches;
  box.toggleAttribute("aria-modal", phone && S.detailOpen);
  if (phone) box.setAttribute("role", "dialog");
  else box.removeAttribute("role");
  q.toggleAttribute("data-solo", !!S.error || (!S.loading && !S.runs.length));
  if (S.loading || S.error || !S.runs.length) {
    setHTML(box, "");
    return;
  }
  if (!e) {
    const g = groups();
    // With nothing to select, the paper shows where the run stands and where its hand-off went.
    setHTML(
      box,
      visible().length
        ? html`<div class="state"><p class="muted">Select a strip to see the draft, the post it answers and why the harness chose it.</p></div>`
        : html`<div class="state q-summary">
            <span class="label">Run ${S.runId}</span>
            <dl class="kv">
              ${BAYS.map(([b, label]) => html`<dt>${label}</dt><dd class="num">${g[b].length}</dd>`)}
            </dl>
            <p class="muted">Cleared drafts are in <code>out/approved-run${S.runId}.json</code> for the posting bot.</p>
          </div>`,
    );
    return;
  }
  const body = e.kind === "action" ? actionDetail(e) : e.kind === "abstain" ? abstainDetail(e) : askDetail(e);
  setHTML(
    box,
    html`<button class="btn btn-quiet btn-small q-back" type="button" data-act="back">← Strips</button>
      <div class="q-scroll" id="q-scroll">${body.main}</div>
      <footer class="q-decide" id="q-decide">${body.footer}</footer>`,
  );
  wireEditor();
}

function fieldsHtml(cells) {
  return html`<div class="fields q-fields">${cells.filter(Boolean).map(([k, v, cls = ""]) => html`<div class="field ${cls.includes("grow") ? "grow" : ""}"><span class="label">${k}</span><span class="v ${cls}" title="${v}">${v}</span></div>`)}</div>`;
}

function kindForGate(type) {
  return TEXT_KINDS.has(type) ? type : "post";
}

function editorHtml(key, text, kind, original, context = "") {
  const n = xLength(text);
  const edited = original != null && text.trim() !== original.trim();
  return html`<div class="editor" data-editor="${key}" data-kind="${kind}" data-context="${context}">
    <div class="ed-wrap">
      <div class="ed-mirror xtext" aria-hidden="true"></div>
      <textarea class="ed-input xtext" aria-label="Draft text. Press E to edit, Escape to stop." spellcheck="true" rows="2">${text}</textarea>
    </div>
    <div class="ed-bar">
      <span class="ed-count">${charRing(n)}<span class="num" data-count>${n}/${X_LIMIT}</span><span class="ed-cap" data-cap></span></span>
      <span class="ed-gate" data-gate></span>
      <span class="sr-only" data-gate-live aria-live="polite"></span>
      ${original != null ? html`<button class="btn btn-quiet btn-small" type="button" data-act="revert" ${edited ? "" : "hidden"}>Undo edits</button>` : ""}
    </div>
    <ul class="ed-findings" data-findings></ul>
  </div>`;
}

// Claims a person should check: numbers with a unit or two or more digits, and superlatives.
// "Game 1" and "4 words" stay out; "337 assists", "10-0", "27%" and "single-season record" come in.
const factRe = /(\b\d+(?:[.,]\d+)?\s?(?:%|percent|points?|assists?|rebounds?|steals?|blocks?|wins?|losses|seasons?|years?|threes?|3-pointers?|minutes|million)\b|\b\d{2,}(?:[-–]\d+)?\b|\b(?:single-season|all-time|career-high|first-ever|franchise) record\b|\brecord\b)/gi;

function factsHtml(text) {
  const hits = [...new Set([...String(text).matchAll(factRe)].map((m) => m[0].trim()))].slice(0, 8);
  if (!hits.length) return "";
  return html`<div class="q-facts"><span class="label">Check before clearing</span>${hits.map((h) => html`<span class="chip flag">${h}</span>`)}<span class="muted">Numbers and superlatives in the draft. The harness can't verify them.</span></div>`;
}

function whySection(a) {
  const n = narrativeOf(a.narrative_id);
  const dm = a.draft_meta || {};
  return html`<section class="section">
    <div class="section-head"><h2>Why this draft</h2><span class="aside">${human(a.mind)} · ${laneOf(a)} lane${dm.method && dm.method !== a.mind ? ` · ${dm.method}` : ""}</span></div>
    ${n
      ? html`<div class="q-narr">
          <p class="q-narr-title">${n.title}</p>
          ${n.summary ? html`<p class="prose">${n.summary}</p>` : ""}
          <div class="q-meters">
            <span class="label">Relevance</span><span class="meter"><i style="width:${Math.round((n.relevance || 0) * 100)}%"></i></span><span class="num">${Math.round((n.relevance || 0) * 100)}</span>
            <span class="label">Velocity</span><span class="meter"><i style="width:${Math.round((n.velocity || 0) * 100)}%"></i></span><span class="num">${Math.round((n.velocity || 0) * 100)}</span>
          </div>
          <p class="muted q-basis">${n.velocity_basis === "data" ? "Velocity from post timestamps and counts." : n.velocity_basis === "llm" ? "Velocity is the model's guess: no member post had a timestamp." : ""}</p>
          ${n.angle ? html`<p><span class="label">Angle</span> ${n.angle}</p>` : ""}
        </div>`
      : a.narrative_id
        ? html`<p class="muted">Narrative <code>${a.narrative_id}</code>${S.contextReason ? html`. ${S.contextReason}` : ""}</p>`
        : ""}
    <dl class="kv">
      ${a.rationale ? html`<dt>Rationale</dt><dd>${a.rationale}</dd>` : ""}
      ${dm.intent ? html`<dt>Intent</dt><dd>${dm.intent}</dd>` : ""}
      ${dm.writer_model ? html`<dt>Writer</dt><dd class="mono">${dm.writer_model}${dm.samples ? ` · ${dm.samples} samples` : ""}${dm.page_replies != null ? ` · saw ${dm.page_replies} replies` : ""}</dd>` : ""}
      ${dm.source_url ? html`<dt>Source</dt><dd>${safeUrl(dm.source_url) ? html`<a href="${safeUrl(dm.source_url)}" target="_blank" rel="noopener noreferrer">${dm.source_url}</a>` : dm.source_url}</dd>` : ""}
    </dl>
  </section>`;
}

function vetoSide(label, s) {
  if (!s) return "";
  return html`<div class="q-veto-side">
    <span class="label">${label}</span>
    <span class="num ${s.p_ai >= 50 ? "bad-text" : ""}">${s.p_ai == null ? "–" : `${s.p_ai}%`}</span><span class="muted">reads as AI</span>
    ${s.unsupported ? html`<span class="chip flag">unsupported claim</span>` : ""}${s.risk ? html`<span class="chip bad">brand risk</span>` : ""}
    ${s.reason ? html`<p class="q-veto-reason">${s.reason}</p>` : ""}
  </div>`;
}

function evidenceSection(a) {
  const j = a.draft_meta?.judge;
  const sc = scoreOf(a);
  const legacy = a.draft_meta?.verdict ? a.draft_meta : null;
  const parts = [];
  if (j) {
    parts.push(html`<div class="q-ev">
      <div class="q-ev-head">
        <span class="stamp ${j.passed ? "cleared" : "struck"}">${j.passed ? "Veto passed" : "Vetoed"}</span>
        <span class="mono muted">${j.veto_model || "veto"}${j.max_p_ai != null ? ` · fails at ${j.max_p_ai}% AI-like` : ""}</span>
        ${j.orders_agree === false ? html`<span class="chip flag">orders disagree</span>` : j.orders_agree ? html`<span class="chip ok">both orders agree</span>` : ""}
      </div>
      <div class="q-veto">${vetoSide("Forward", j.fwd)}${vetoSide("Reversed", j.rev)}</div>
      ${j.why ? html`<p class="bad-text">${j.why}</p>` : ""}
      ${j.batch
        ? html`<p class="q-batch"><span class="num">${j.batch.drafts}</span> drafts → <span class="num">${j.batch.gate_passed}</span> passed the slop gate → <span class="num">${j.batch.veto_passed}</span> passed the veto → ${j.pick === "random among drafts that passed" ? "this one picked at random among those" : j.pick || "picked"}. The veto rejects; it doesn't rank.</p>`
        : ""}
      ${j.grounding ? html`<p><span class="label">Grounding</span> <span class="num">${fixed(j.grounding.score)}</span> <span class="muted">(needs ${fixed(j.grounding.min)})</span> ${j.grounding.reason || ""}</p>` : ""}
      ${(j.references || []).length ? referencesHtml(j.references) : ""}
    </div>`);
  } else if (legacy) {
    parts.push(html`<div class="q-ev"><div class="q-ev-head"><span class="stamp ${legacy.verdict === "Strong" ? "cleared" : "held"}">${legacy.verdict}</span><span class="mono muted">${legacy.ranker || "judge"} · ${legacy.confidence} confidence</span></div>${legacy.judge_reason ? html`<p>${legacy.judge_reason}</p>` : ""}</div>`);
  }
  if (sc) {
    parts.push(html`<div class="q-ev">
      <div class="q-ev-head"><span class="label">Simulated audience panel</span><span class="mono">${Math.round(sc.mean * 100)}</span><span class="muted">mean of ${sc.n} model-played personas, range ${Math.round(sc.lo * 100)}–${Math.round(sc.hi * 100)}. A prediction, not a measurement.</span></div>
      <div class="q-panel">${Object.entries(a.panel).map(([k, v]) => html`<span class="q-panel-name">${human(k)}</span><span class="meter"><i style="width:${Math.round(v * 100)}%"></i></span><span class="num">${Math.round(v * 100)}</span>`)}</div>
    </div>`);
  }
  if (a.critique) {
    parts.push(html`<div class="q-ev"><div class="q-ev-head"><span class="label">Critic</span>${a.vetoed ? html`<span class="stamp struck">Vetoed</span>` : html`<span class="chip ok">kept</span>`}</div><p class="prose">${a.critique}</p></div>`);
  }
  if (!parts.length) return "";
  return html`<section class="section"><div class="section-head"><h2>Evidence</h2></div>${parts}</section>`;
}

function referencesHtml(refs) {
  return html`<div class="q-refs"><span class="label">Reference posts the judge compared against</span>${refs.map((r) => html`<div class="q-ref"><span class="chip ${r.performance === "Successful" ? "ok" : "bad"}">${r.performance}</span><span class="num muted">${compact(r.engagement)}</span><span>${r.text}</span></div>`)}</div>`;
}

/** The veto's verdict on one candidate in words: null when it passed or wasn't run. */
function vetoProblem(v) {
  if (!v) return null;
  if (v.orders_agree === false) return v.why && /split/i.test(v.why) ? v.why : `split verdict${v.why ? `: ${v.why}` : ""}`;
  if (v.passed !== false) return null;
  const sides = [v.fwd, v.rev].filter(Boolean);
  return v.why || (sides.some((x) => x.unsupported) ? "unsupported claim" : sides.some((x) => x.risk) ? "brand risk" : "reads as AI");
}

function candidateHtml(c, i, { shippedText, action } = {}) {
  if (!c.text) {
    return html`<li class="q-cand broken"><span class="label">Draft ${i + 1}</span><span class="muted">${c.schema_error || c.error || (c.abstain ? `abstained: ${c.abstain.reason}` : "no text")}</span></li>`;
  }
  const gate = c.gate || [];
  const v = c.veto;
  const problem = vetoProblem(v);
  const shipped = shippedText && c.text === shippedText;
  return html`<li class="q-cand ${shipped ? "shipped" : ""}">
    <div class="q-cand-head">
      <span class="label">${c.mind ? human(c.mind) : `Draft ${i + 1}`}${c.type ? ` · ${c.type}` : ""}</span>
      ${shipped ? html`<span class="stamp cleared">Shipped</span>` : ""}
      ${gate.length ? html`<span class="chip flag">${plural(gate.length, "gate flag")}</span>` : html`<span class="chip ok">gate clear</span>`}
      ${v ? html`<span class="chip ${problem ? "bad" : "ok"}">${problem ? `vetoed: ${problem}` : "veto passed"}</span>` : ""}
      ${v?.fwd ? html`<span class="num muted">AI-like ${v.fwd.p_ai}% / ${v.rev?.p_ai ?? "–"}%</span>` : ""}
      ${c.grounding ? html`<span class="num muted">grounding ${fixed(c.grounding.score)}</span>` : ""}
      ${action ? html`<button class="btn btn-quiet btn-small" type="button" data-act="use" data-text="${c.text}">${problem ? `${action} anyway` : action}</button>` : ""}
    </div>
    <p class="q-cand-text">${c.text}</p>
    ${problem && (v.fwd?.reason || v.rev?.reason) ? html`<p class="q-cand-veto">${[v.fwd?.reason, v.rev?.reason].filter(Boolean).map((r, k) => html`<span><span class="label">${k ? "Reversed" : "Forward"}</span> ${r}</span>`)}</p>` : ""}
    ${gate.length ? html`<p class="q-cand-gate">${gate.map((f) => html`<span class="mono">${f.rule}${f.detail ? `: ${f.detail}` : ""}</span>`)}</p>` : ""}
  </li>`;
}

function previewForAction(a, key, editable) {
  const t = a.target_url ? targetOf(a, S.context) : null;
  const text = S.edits.get(key) ?? textOf(a);
  const handle = S.ctx.meta?.account_handle;
  const ours = html`<div class="xpost ours">
    ${avatar(handle, { ours: true })}
    <div class="xbody">
      <header class="xhead">
        <span class="xname">${handle ? `@${handle}` : "Our account"}</span>
        <span class="xmuted">· ${{ approved: "cleared", killed: "struck" }[statusOf(a)] || "draft"}</span>
      </header>
      ${a.type === "reply" && t ? html`<p class="xmuted xreplying">Replying to <span class="xlink">@${t.author}</span></p>` : ""}
      ${editable ? editorHtml(key, text, kindForGate(a.type), a.text, t?.text || "") : html`<p class="xtext">${xText(text)}</p>`}
      ${a.type === "quote" && t ? targetCard(t, { quoted: true, runStartedAt: S.context?.started_at }) : ""}
    </div>
  </div>`;
  if (a.type === "follow") return html`<div class="xstage">${profileCard(a.target_handle, { note: "The account would follow this profile." })}</div>`;
  if (a.type === "reply" && t) {
    return html`<div class="xstage">
      ${targetCard(t, { runStartedAt: S.context?.started_at })}
      <div class="xthread-line" aria-hidden="true"></div>
      ${ours}
      ${threadReplies(t.thread)}
      ${!t.thread.length && t.thread_attached ? html`<p class="muted q-thread-note">${t.thread_attached} replies were on the page when the draft was written.</p>` : ""}
    </div>`;
  }
  return html`<div class="xstage">${ours}</div>`;
}

function actionDetail(e) {
  const a = e.a;
  const st = statusOf(a);
  const editable = st === "pending" && a.type !== "follow";
  const sc = scoreOf(a);
  const n = narrativeOf(a.narrative_id);
  const dec = decisionFor(a);
  const reasons = S.reasons.get(a.id) || new Set(a.review?.reasons || []);
  const main = html`
    ${fieldsHtml([
      ["Type", a.type],
      ["Lane", laneOf(a)],
      ["Mind", human(a.mind), "ui"],
      ["Story", n?.title || a.narrative_id || "–", "ui grow"],
      sc ? ["Panel", `${Math.round(sc.mean * 100)} (${Math.round(sc.lo * 100)}–${Math.round(sc.hi * 100)})`] : null,
      ["Status", e.veto ? "held by critic" : st === "pending" ? "waiting" : st === "approved" ? "cleared" : "struck"],
    ])}
    ${e.veto ? html`<p class="notice warn"><b>The critic vetoed this draft.</b> ${a.critique || ""} Clear it anyway if you disagree.</p>` : ""}
    ${previewForAction(a, e.key, editable)}
    ${a.type !== "follow" ? factsHtml(S.edits.get(e.key) ?? textOf(a)) : ""}
    ${a.review?.text && st !== "pending" ? html`<div class="q-diff"><span class="label">Your edit</span>${diffHtml(a.text, a.review.text)}</div>` : ""}
    ${whySection(a)}
    ${evidenceSection(a)}
    ${dec?.candidates?.length ? html`<section class="section"><div class="section-head"><h2>Every draft for this opportunity</h2><span class="aside">${dec.candidates.length} written; the shipped one is marked</span></div><ol class="q-cands">${dec.candidates.map((c, i) => candidateHtml(c, i, { shippedText: a.text, action: editable && c.text !== a.text ? "Use this text" : "" }))}</ol></section>` : ""}
  `;
  let footer;
  if (st === "pending") {
    footer = html`
      <div class="q-decide-row">
        <label class="sr-only" for="q-note">Note for learning</label>
        <input id="q-note" class="input q-note" placeholder="Note for learning (optional)" value="${S.notes.get(e.key) ?? a.review?.note ?? ""}">
        <button class="btn btn-strike" type="button" data-act="strike">Strike <kbd>x</kbd></button>
        <button class="btn btn-clear" type="button" data-act="clear">${e.veto ? "Clear anyway" : "Clear"} <kbd>a</kbd></button>
      </div>`;
  } else {
    footer = html`<div class="q-decide-row">
      <span class="stamp ${st === "approved" ? "cleared" : "struck"}" title="${clock(a.review.reviewed_at, true)}">${st === "approved" ? "Cleared" : "Struck"} ${stampTime(a.review.reviewed_at)}</span>
      ${st === "killed" ? reasonChips(a.id, reasons) : ""}
      ${a.review.note ? html`<span class="muted q-note-text">“${a.review.note}”</span>` : ""}
      <button class="btn btn-quiet" type="button" data-act="reset">Return to queue <kbd>u</kbd></button>
    </div>`;
  }
  return { main, footer };
}

function reasonChips(id, set) {
  const meta = reasonsMeta();
  return html`<div class="q-reasons inline" role="group" aria-label="Why strike it">
    ${Object.entries(meta).map(([code, label], i) => html`<button class="chip" type="button" data-reason="${code}" data-id="${id}" aria-pressed="${String(set.has(code))}"><kbd>${i + 1}</kbd>${label}</button>`)}
  </div>`;
}

function abstainDetail(e) {
  const d = e.d;
  const cands = d.candidates || [];
  const usable = cands.filter((c) => c.text);
  // Pre-fill only a draft that passed both the gate and the veto. A vetoed draft can be loaded on
  // purpose, but the editor never starts with one: a clean gate badge beside it would read as safe.
  const best = usable.find(clean);
  // An action in the same run may already carry what observe saw of this post.
  const sibling = S.actions.find((a) => a.target_url && a.target_url === d.opportunity);
  const fakeAction = { target_url: isXPost(d.opportunity) ? d.opportunity : null, target_text: sibling?.target_text || "", draft_meta: { target: sibling?.draft_meta?.target } };
  const t = fakeAction.target_url ? targetOf(fakeAction, S.context) : null;
  const text = S.edits.get(e.key) ?? best?.text ?? "";
  // The same rule as post_overrule in checkpoint.py: a draft the harness wrote keeps its type when
  // released unchanged. Otherwise the reply lane answers the post, and in the post lane an X post
  // as the opportunity is quoted and anything else becomes a plain post.
  const picked = usable.find((c) => c.text.trim() === text.trim());
  const kind = TEXT_KINDS.has(picked?.type) ? picked.type : d.lane === "reply" ? "reply" : t ? "quote" : "post";
  const handle = S.ctx.meta?.account_handle;
  const canWrite = d.lane !== "reply" || !!t;
  const ours = html`<div class="xpost ours">${avatar(handle, { ours: true })}<div class="xbody">
    <header class="xhead"><span class="xname">${handle ? `@${handle}` : "Our account"}</span><span class="xmuted">· overrule</span></header>
    ${kind === "reply" && t ? html`<p class="xmuted xreplying">Replying to <span class="xlink">@${t.author}</span></p>` : ""}
    ${editorHtml(e.key, text, kind, null, t?.text || "")}
    ${kind === "quote" && t ? targetCard(t, { quoted: true, runStartedAt: S.context?.started_at }) : ""}
  </div></div>`;
  const main = html`
    ${fieldsHtml([
      ["Held", d.lane + " lane"],
      ["Reason", human(d.reason), "grow"],
      ["Drafts", `${cands.length} written, ${cands.filter(clean).length} passed`],
    ])}
    <p class="notice"><b>The harness held back:</b> ${abstainLabel(d.reason)}.${d.detail ? ` ${sentence(d.detail)}` : ""} ${!canWrite ? "There was nothing to answer, so there's nothing to release." : usable.length ? "To overrule it, release one of its drafts or write your own." : `To overrule it, write the ${kind} yourself.`}</p>
    ${canWrite
      ? html`<div class="section-head q-overrule-head"><h2>What you'd release</h2><span class="aside">Edit freely. The gate checks as you type.</span></div>
          <div class="xstage">
            ${kind === "reply" && t
              ? html`${targetCard(t, { runStartedAt: S.context?.started_at })}<div class="xthread-line" aria-hidden="true"></div>${ours}${threadReplies(t.thread)}`
              : ours}
          </div>
          ${factsHtml(text)}`
      : t
        ? html`<div class="xstage">${targetCard(t, { runStartedAt: S.context?.started_at })}</div>`
        : ""}
    ${cands.length ? html`<section class="section"><div class="section-head"><h2>Drafts the harness wrote</h2><span class="aside">Load one into the editor, then release it</span></div><ol class="q-cands">${cands.map((c, i) => candidateHtml(c, i, { action: canWrite ? "Load" : "" }))}</ol></section>` : ""}
  `;
  const footer = canWrite
    ? html`<div class="q-decide-row">
        <label class="sr-only" for="q-note">Note for learning</label>
        <input id="q-note" class="input q-note" placeholder="Why overrule? (optional, goes to learning)" value="${S.notes.get(e.key) ?? ""}">
        <button class="btn btn-ink" type="button" data-act="release" ${text.trim() ? "" : "disabled"}><span data-label>Release</span> <kbd>a</kbd></button>
      </div>`
    : html`<div class="q-decide-row"><span class="muted">Nothing to release for this hold.</span></div>`;
  return { main, footer };
}

function askDetail(e) {
  const d = e.d;
  const n = narrativeOf(d.opportunity);
  const text = S.edits.get(e.key) ?? "";
  const handle = S.ctx.meta?.account_handle;
  const [ask, how0] = d.question.split(/(?<=\?) /);
  const how = how0 ? how0.replace(/Add it to action\/takes\.json;\s*/, "") : "";
  const main = html`
    ${fieldsHtml([["Ask", "your take"], ["Story", n?.title || d.opportunity, "ui grow"]])}
    <div class="q-ask">
      <p class="q-ask-q">${ask}</p>
      ${how ? html`<p class="muted">${how[0].toUpperCase() + how.slice(1)}</p>` : ""}
    </div>
    ${n?.summary ? html`<p class="prose muted">${n.summary}</p>` : ""}
    <section class="section">
      <div class="section-head"><h2>Your words</h2><span class="aside">Posted as written. The gate flags slop but can't block you.</span></div>
      <div class="xstage"><div class="xpost ours">${avatar(handle, { ours: true })}<div class="xbody">
        <header class="xhead"><span class="xname">${handle ? `@${handle}` : "Our account"}</span><span class="xmuted">· your take</span></header>
        ${editorHtml(e.key, text, "post", null)}
      </div></div></div>
    </section>
    ${(d.candidates || []).length ? html`<section class="section"><div class="section-head"><h2>Angles the minds tried</h2><span class="aside">Starting points. None shipped.</span></div><ol class="q-cands">${d.candidates.map((c, i) => candidateHtml(c, i, { action: "Start from this" }))}</ol></section>` : ""}
  `;
  const footer = html`<div class="q-decide-row">
    <span class="muted q-grow">⌘↵ clears from the editor</span>
    <button class="btn btn-clear" type="button" data-act="take" ${text.trim() ? "" : "disabled"}>Clear your take <kbd>a</kbd></button>
  </div>`;
  return { main, footer };
}

/** Word-level diff between the harness's draft and the person's edit. */
function diffHtml(a, b) {
  const A = a.split(/(\s+)/), B = b.split(/(\s+)/);
  const m = A.length, n = B.length;
  if (m * n > 250_000) return html`<p><del>${a}</del></p><p><ins>${b}</ins></p>`;
  const L = Array.from({ length: m + 1 }, () => new Uint16Array(n + 1));
  for (let i = m - 1; i >= 0; i--) for (let j = n - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < m && j < n) {
    if (A[i] === B[j]) {
      out.push(html`${A[i++]}`);
      j++;
    } else if (L[i + 1][j] >= L[i][j + 1]) out.push(html`<del>${A[i++]}</del>`);
    else out.push(html`<ins>${B[j++]}</ins>`);
  }
  while (i < m) out.push(html`<del>${A[i++]}</del>`);
  while (j < n) out.push(html`<ins>${B[j++]}</ins>`);
  return html`<p class="q-diff-text">${out}</p>`;
}

// --- Editor: count, gate and highlights as the person types -----------------------------------

function wireEditor() {
  const ed = $("#q-detail .editor");
  if (!ed) return;
  const ta = ed.querySelector("textarea");
  autosize(ta);
  updateEditor(ed, false);
}

function autosize(ta) {
  ta.style.height = "auto";
  ta.style.height = `${ta.scrollHeight}px`;
}

function updateEditor(ed, typed) {
  const ta = ed.querySelector("textarea");
  const key = ed.dataset.editor;
  const text = ta.value;
  if (typed) S.edits.set(key, text);
  const n = xLength(text);
  const count = ed.querySelector("[data-count]");
  count.textContent = `${n}/${X_LIMIT}`;
  count.classList.toggle("bad-text", n > X_LIMIT);
  ed.querySelector(".xring").outerHTML = String(charRing(n));
  const revert = ed.querySelector("[data-act=revert]");
  const e = findEntry(key);
  if (revert && e?.a) revert.hidden = text.trim() === e.a.text.trim();
  const release = $("#q-decide [data-act=release], #q-decide [data-act=take]");
  if (release) release.disabled = !text.trim();
  // Releasing a draft the veto rejected is allowed, but the button says so.
  const relLabel = $("#q-decide [data-act=release] [data-label]");
  if (relLabel) relLabel.textContent = vetoForText(key, text) ? "Release over the veto" : "Release";
  autosize(ta);
  clearTimeout(S.gateTimer);
  const kind = ed.dataset.kind;
  const context = ed.dataset.context || "";
  if (!text.trim()) {
    setHTML(ed.querySelector("[data-gate]"), html`<span class="muted">The gate checks as you type.</span>`);
    setHTML(ed.querySelector("[data-findings]"), "");
    setHTML(ed.querySelector(".ed-mirror"), "");
    return;
  }
  const cacheKey = `${kind}\u0000${context}\u0000${text}`;
  const cached = S.gateCache.get(cacheKey);
  if (cached) return paintGate(ed, text, cached);
  setHTML(ed.querySelector("[data-gate]"), html`<span class="muted">checking…</span>`);
  S.gateTimer = setTimeout(() => runGate(ed, text, kind, context, cacheKey), typed ? 180 : 0);
}

async function runGate(ed, text, kind, context, cacheKey) {
  let res;
  try {
    res = await S.ctx.api.post("/api/gate", context ? { text, kind, context } : { text, kind });
    S.gateCache.set(cacheKey, res); // a failed request isn't cached, so the next keystroke retries
  } catch (err) {
    res = { available: false, reason: err.message };
  }
  if (ed.isConnected && ed.querySelector("textarea").value === text) paintGate(ed, text, res);
}

function paintGate(ed, text, res) {
  const gate = ed.querySelector("[data-gate]");
  const list = ed.querySelector("[data-findings]");
  const mirror = ed.querySelector(".ed-mirror");
  const cap = ed.querySelector("[data-cap]");
  if (!res.available) {
    setHTML(gate, html`<span class="muted" title="${res.reason}">Slop gate not connected</span>`);
    setHTML(list, "");
    setHTML(mirror, "");
    cap.textContent = "";
    return;
  }
  cap.textContent = res.limit && res.limit < X_LIMIT ? ` · gate cap ${res.limit}` : "";
  const f = res.findings || [];
  const vetoed = vetoForText(ed.dataset.editor, text);
  setHTML(
    gate,
    html`${f.length ? html`<span class="chip flag">${plural(f.length, "slop flag")}</span>` : html`<span class="chip ok">Gate clear</span>`}${vetoed ? html`<span class="chip bad">veto failed: ${vetoed}</span>` : ""}${res.simulated ? html`<span class="muted"> (demo rules)</span>` : ""}`,
  );
  setHTML(list, html`${f.map((x) => html`<li><span class="label">${human(x.rule)}</span> ${x.detail}</li>`)}`);
  const live = ed.querySelector("[data-gate-live]");
  const said = (f.length ? `${plural(f.length, "slop flag")}: ${f.map((x) => human(x.rule)).join(", ")}` : "Gate clear") + (vetoed ? `. Veto failed: ${vetoed}` : "");
  if (live && live.textContent !== said) live.textContent = said;
  // Underline the exact spans when the gate reports offsets; whole-text rules have none.
  const spans = f.filter((x) => Number.isInteger(x.start) && Number.isInteger(x.end) && x.end > x.start).sort((p, q) => p.start - q.start);
  const out = [];
  let at = 0;
  for (const x of spans) {
    if (x.start < at) continue;
    out.push(text.slice(at, x.start), html`<mark title="${x.rule}">${text.slice(x.start, x.end)}</mark>`);
    at = x.end;
  }
  // The zero-width space keeps a trailing newline in the mirror the same height as in the textarea.
  setHTML(mirror, spans.length ? html`${out}${text.slice(at)}${"\u200B"}` : "");
}

/** If the editor holds a draft the veto already rejected, say why; the gate can't see that. */
function vetoForText(key, text) {
  const e = findEntry(key);
  if (!e) return null;
  const cands = e.kind === "action" ? decisionFor(e.a)?.candidates || [] : e.d?.candidates || [];
  const c = cands.find((x) => x.text && x.text.trim() === text.trim());
  return c ? vetoProblem(c.veto) : null;
}

// --- Focus ---------------------------------------------------------------------------------

/** True when keyboard focus is in this view or nowhere (on body), so a re-render may move it. */
const focusIsOurs = () => !document.activeElement || document.activeElement === document.body || !!S.el?.contains(document.activeElement);

/** After a re-render deleted the focused element, put focus on the selected strip, or on the
    sheet's back button when the phone sheet is open, so keyboard and screen-reader users keep
    their place. */
function restoreFocus(had) {
  if (!had) return;
  const target = S.detailOpen ? $("#q-detail .q-back") : $(`#q-list [aria-selected="true"]`) || $(`#q-bays [aria-selected="true"]`);
  target?.focus({ preventScroll: true });
}

// --- Verdicts -------------------------------------------------------------------------------

function nextKeyAfter(key) {
  const items = visible();
  const i = items.findIndex((x) => x.key === key);
  const rest = items.filter((x) => x.key !== key);
  return rest[Math.min(i, rest.length - 1)]?.key ?? null;
}

function currentText(e) {
  return S.edits.get(e.key) ?? textOf(e.a);
}

async function verdict(e, status) {
  if (!e || e.kind !== "action") return;
  const a = e.a;
  const prev = { ...a.review };
  let edited = null;
  if (status === "approved" && a.type !== "follow") {
    const text = currentText(e).trim();
    if (!text) return S.ctx.readback(html`<b class="bad">Not cleared</b> · the draft is empty`);
    if (xLength(text) > X_LIMIT) return S.ctx.readback(html`<b class="bad">Not cleared</b> · ${xLength(text)} characters is over X's ${X_LIMIT}`);
    if (text !== a.text.trim()) edited = text;
  }
  const reasons = status === "killed" ? [...(S.reasons.get(a.id) || [])] : [];
  let note = $("#q-note")?.value ?? S.notes.get(e.key) ?? "";
  if (status === "approved" && e.veto && !note) note = "Cleared over the critic's veto.";
  const next = status === "pending" ? e.key : nextKeyAfter(e.key);
  const draft = S.edits.get(e.key);
  a.review = { status, text: status === "pending" ? null : edited, note: status === "pending" ? "" : note, reasons, reviewed_at: new Date().toISOString() };
  S.edits.delete(e.key);
  if (S.lastStruck === a.id) S.lastStruck = null;
  S.sel = next;
  S.ctx.setParams({ sel: S.sel });
  const had = focusIsOurs();
  render();
  restoreFocus(had);
  // The entry keeps the person's unsaved text, so undoing a clear brings their edit back too.
  const entry = { id: a.id, key: e.key, prev, status, edit: draft };
  S.undo.push(entry);
  const what = a.type === "follow" ? `follow @${a.target_handle}` : `${a.type} by ${human(a.mind)}`;
  if (status === "approved") {
    S.ctx.readback(html`<b class="ok">CLEARED</b> ${what}${edited ? " with your edit" : ""} · hand-off out/approved-run${S.runId}.json · <kbd>z</kbd> undo`);
  } else if (status === "killed") {
    S.lastStruck = a.id;
    readbackStrike(a);
  } else {
    S.ctx.readback(html`<b>RETURNED</b> ${what} to the queue`);
  }
  try {
    await S.ctx.api.post(`/api/actions/${a.id}/review`, { status, text: edited, note, reasons });
    S.ctx.refreshNav();
    refreshRunCounts();
  } catch (err) {
    a.review = prev;
    if (draft != null) S.edits.set(e.key, draft);
    // Remove this verdict's own entry; a later verdict may have been pushed while this one was in flight.
    const at = S.undo.indexOf(entry);
    if (at >= 0) S.undo.splice(at, 1);
    if (S.lastStruck === a.id) S.lastStruck = null;
    S.sel = e.key;
    const hadFocus = focusIsOurs();
    render();
    restoreFocus(hadFocus);
    S.ctx.toast(`Couldn't save the verdict: ${err.message}`, { kind: "error" });
    S.ctx.readback(html`<b class="bad">NOT SAVED</b> ${what} · ${err.message}`);
  }
}

function readbackStrike(a) {
  const set = S.reasons.get(a.id) || new Set();
  const codes = Object.keys(reasonsMeta());
  const chips = Object.entries(reasonsMeta()).map(([code, label], i) => ({ key: String(i + 1), label, on: set.has(code) }));
  const onChip = (key) => {
    const code = codes[+key - 1];
    if (code && S.lastStruck === a.id && statusOf(a) === "killed") toggleReason(a.id, code);
  };
  S.ctx.readback(html`<b class="bad">STRUCK</b> ${a.type} by ${human(a.mind)} · why?`, { chips, onChip });
}

/** Which struck strip number keys tag: the selected one if it's struck, else the one just struck. */
function reasonTarget(e) {
  if (e?.kind === "action" && statusOf(e.a) === "killed") return e.a.id;
  const last = S.actions.find((a) => a.id === S.lastStruck);
  return last && statusOf(last) === "killed" ? last.id : null;
}

function toggleReason(id, code) {
  const set = S.reasons.get(id) || new Set();
  if (set.has(code)) set.delete(code);
  else set.add(code);
  S.reasons.set(id, set);
  const a = S.actions.find((x) => x.id === id);
  if (!a) return;
  if (statusOf(a) === "killed") {
    a.review.reasons = [...set];
    if (S.lastStruck === id) readbackStrike(a);
    // Several quick taps save once; each strip has its own timer so tagging another doesn't drop this save.
    clearTimeout(S.reasonTimers.get(id));
    S.reasonTimers.set(
      id,
      setTimeout(() => {
        S.reasonTimers.delete(id);
        if (statusOf(a) !== "killed") return; // undone or returned since: saving would strike it again
        S.ctx.api.post(`/api/actions/${id}/review`, { status: "killed", text: null, note: a.review.note || "", reasons: [...set] }).catch((err) => S.ctx.toast(`Couldn't save the reasons: ${err.message}`, { kind: "error" }));
      }, 350),
    );
  }
  for (const b of $$(`[data-reason][data-id="${id}"]`)) b.setAttribute("aria-pressed", String(set.has(b.dataset.reason)));
}

async function undo() {
  const last = S.undo.pop();
  if (!last) return S.ctx.readback("Nothing to undo.");
  const a = S.actions.find((x) => x.id === last.id);
  if (!a) return;
  const undone = a.review;
  const back = last.prev;
  const status = back.status || "pending";
  a.review = { ...back, status };
  if (status === "pending" && last.edit != null) S.edits.set(last.key, last.edit);
  if (S.lastStruck === a.id && status !== "killed") S.lastStruck = null;
  const show = () => {
    S.bay = statusOf(a) === "approved" ? "cleared" : statusOf(a) === "killed" ? "struck" : a.vetoed ? "held" : "clear";
    S.sel = last.key;
    S.ctx.setParams({ bay: S.bay, sel: S.sel });
    const had = focusIsOurs();
    render();
    restoreFocus(had);
  };
  show();
  S.ctx.readback(html`<b>UNDONE</b> ${a.type} by ${human(a.mind)} is back in ${BAYS.find(([b]) => b === S.bay)[1]}`);
  try {
    await S.ctx.api.post(`/api/actions/${a.id}/review`, { status, text: back.text ?? null, note: back.note || "", reasons: back.reasons || [] });
  } catch (err) {
    // The server still holds the verdict, so the page shows it again and the undo stays available.
    a.review = undone;
    S.undo.push(last);
    show();
    S.ctx.toast(`Couldn't undo: ${err.message}`, { kind: "error" });
    S.ctx.readback(html`<b class="bad">NOT UNDONE</b> ${a.type} by ${human(a.mind)} · ${err.message}`);
  }
}

async function release(e) {
  const text = (S.edits.get(e.key) ?? $("#q-detail textarea")?.value ?? "").trim();
  if (!text) return;
  if (xLength(text) > X_LIMIT) return S.ctx.readback(html`<b class="bad">Not released</b> · ${xLength(text)} characters is over X's ${X_LIMIT}`);
  const cands = e.d.candidates || [];
  const ci = cands.findIndex((c) => c.text?.trim() === text);
  const note = $("#q-note")?.value || "";
  const btn = $("#q-decide [data-act=release]");
  if (btn) btn.disabled = true;
  // The hold leaves Held once the overrule is stored, so find the strip after it first.
  const next = nextKeyAfter(e.key);
  try {
    await S.ctx.api.post(`/api/runs/${S.runId}/overrule`, { decision: e.i, candidate: ci >= 0 ? ci : null, text, note: note || undefined });
    await refreshActions();
    S.edits.delete(e.key);
    S.ctx.readback(html`<b class="ok">RELEASED</b> held ${e.d.lane} draft, overruling “${human(e.d.reason)}” · hand-off updated`);
    S.sel = next;
    const had = focusIsOurs();
    render();
    restoreFocus(had);
  } catch (err) {
    if (btn) btn.disabled = false;
    S.ctx.toast(`Couldn't release it: ${err.message}`, { kind: "error" });
  }
}

async function submitTake(e) {
  const text = (S.edits.get(e.key) ?? "").trim();
  if (!text) return;
  if (xLength(text) > X_LIMIT) return S.ctx.readback(html`<b class="bad">Not cleared</b> · ${xLength(text)} characters is over X's ${X_LIMIT}`);
  const btn = $("#q-decide [data-act=take]");
  if (btn) btn.disabled = true;
  try {
    await S.ctx.api.post(`/api/runs/${S.runId}/takes`, { decision: e.i, text });
    S.edits.delete(e.key);
    await refreshActions();
    S.ctx.readback(html`<b class="ok">CLEARED</b> your take, posted as written · hand-off updated`);
    S.sel = groups().take[0]?.key ?? null;
    const had = focusIsOurs();
    render();
    restoreFocus(had);
  } catch (err) {
    if (btn) btn.disabled = false;
    S.ctx.toast(`Couldn't save your take: ${err.message}`, { kind: "error" });
  }
}

/** The run picker shows cleared counts; refresh them quietly without touching the selection. */
async function refreshRunCounts() {
  try {
    S.runs = await S.ctx.api.get("/api/runs");
  } catch {
    return;
  }
  const sel = $("#q-run");
  if (!sel || document.activeElement === sel) return;
  renderHead();
}

async function refreshActions() {
  S.actions = await S.ctx.api.get(`/api/runs/${S.runId}`);
}

async function copyHandoff() {
  try {
    const data = await S.ctx.api.get(`/api/runs/${S.runId}/approved`);
    await navigator.clipboard.writeText(JSON.stringify({ run_id: S.runId, actions: data }, null, 2));
    S.ctx.readback(html`<b>COPIED</b> ${plural(data.length, "cleared action")} · also in out/approved-run${S.runId}.json`);
  } catch (err) {
    S.ctx.readback(html`<b class="bad">NOT COPIED</b> ${err.message || "clipboard blocked"} · the file is out/approved-run${S.runId}.json`);
  }
}

// --- Navigation -----------------------------------------------------------------------------

// The list is one tab stop (roving tabindex): Tab reaches the selected strip, j/k or the arrows
// move within the list, and Tab again goes on to the draft.
function select(key, { scroll = true } = {}) {
  if (!key) return;
  const listHadFocus = $("#q-list")?.contains(document.activeElement);
  const prev = $(`#q-list [aria-selected="true"]`);
  if (prev) {
    prev.setAttribute("aria-selected", "false");
    prev.tabIndex = -1;
  }
  S.sel = key;
  const el = $(`#q-list [data-key="${CSS.escape(key)}"]`);
  if (el) {
    el.setAttribute("aria-selected", "true");
    el.tabIndex = 0;
    if (scroll) el.scrollIntoView({ block: "nearest" });
    if (listHadFocus) el.focus({ preventScroll: true });
  }
  S.ctx.setParams({ sel: key });
  renderDetail();
}

function move(d) {
  const items = visible();
  if (!items.length) return;
  const i = items.findIndex((x) => x.key === S.sel);
  const j = Math.max(0, Math.min(items.length - 1, (i < 0 ? 0 : i + d)));
  select(items[j].key);
}

function setBay(b) {
  S.bay = b;
  S.sel = groups()[b][0]?.key ?? null;
  S.ctx.setParams({ bay: b, sel: S.sel });
  renderBays();
  renderList();
  renderDetail();
  $(`#q-bays [data-bay="${b}"]`)?.focus({ preventScroll: true });
}

function focusEditor() {
  const ta = $("#q-detail textarea");
  if (!ta) return false;
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  return true;
}

function primary(e) {
  if (!e) return;
  if (e.kind === "action") return statusOf(e.a) === "pending" ? verdict(e, "approved") : null;
  if (e.kind === "abstain") return release(e);
  if (e.kind === "ask") return (S.edits.get(e.key) || "").trim() ? submitTake(e) : focusEditor();
}

function onKey(ev) {
  const e = selected();
  if (isTyping(ev.target)) {
    if (ev.key === "Escape") {
      // Leave the field for the selected strip rather than <body>, so the next key lands somewhere.
      const strip = S.detailOpen ? $("#q-detail .q-back") : $(`#q-list [aria-selected="true"]`);
      if (strip) strip.focus({ preventScroll: true });
      else ev.target.blur();
      return true;
    }
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey) && ev.target.closest("#q-detail")) {
      primary(e);
      return true;
    }
    return false;
  }
  if (ev.ctrlKey || ev.metaKey) return false;
  const k = ev.key;
  if (k === "Escape" && S.detailOpen) {
    closeSheet({ instant: true });
    return true;
  }
  // Arrow keys move between bays while a bay tab has focus (the tablist pattern).
  if (["ArrowRight", "ArrowLeft", "Home", "End"].includes(k) && ev.target.closest?.("#q-bays")) {
    const i = BAYS.findIndex(([b]) => b === S.bay);
    const j = k === "Home" ? 0 : k === "End" ? BAYS.length - 1 : (i + (k === "ArrowRight" ? 1 : BAYS.length - 1)) % BAYS.length;
    setBay(BAYS[j][0]);
    return true;
  }
  if (k === "j" || k === "ArrowDown") return move(1), true;
  if (k === "k" || k === "ArrowUp") return move(-1), true;
  if (k === "]" || k === "[") {
    const i = BAYS.findIndex(([b]) => b === S.bay);
    setBay(BAYS[(i + (k === "]" ? 1 : BAYS.length - 1)) % BAYS.length][0]);
    return true;
  }
  if (/^[1-9]$/.test(k)) {
    const code = Object.keys(reasonsMeta())[+k - 1];
    const id = reasonTarget(e);
    if (code && id) {
      toggleReason(id, code);
      return true;
    }
    return false;
  }
  if (!e) return false;
  if (k === "a") return primary(e), true;
  if (k === "x" && e.kind === "action" && statusOf(e.a) === "pending") return verdict(e, "killed"), true;
  if (k === "e") return focusEditor();
  if (k === "u" && e.kind === "action" && statusOf(e.a) !== "pending") return verdict(e, "pending"), true;
  if (k === "z") return undo(), true;
  if (k === "n") return $("#q-note")?.focus(), true;
  if (k === "o") {
    const url = safeUrl(e.a?.target_url || (isXPost(e.d?.opportunity) ? e.d.opportunity : null));
    if (url) window.open(url, "_blank", "noopener");
    return !!url;
  }
  // Enter opens the phone sheet only from the strip list or nothing; on links and buttons it must
  // keep activating them (WCAG 2.1.1).
  const onStrip = ev.target === document.body || !!ev.target.closest?.("#q-list .strip");
  if (k === "Enter" && onStrip && matchMedia(PHONE).matches) {
    openSheet({ instant: true });
    return true;
  }
  return false;
}

// --- The phone sheet: modal while open --------------------------------------------------------

// The sheet slides only when a tap opened or closed it; keyboard actions never animate.
function openSheet({ instant = false } = {}) {
  S.detailOpen = true;
  S.el.querySelector(".q").toggleAttribute("data-instant", instant);
  renderDetail();
  setInert(true);
  $("#q-detail .q-back")?.focus({ preventScroll: true });
}

function closeSheet({ instant = false } = {}) {
  S.detailOpen = false;
  S.el.querySelector(".q").toggleAttribute("data-instant", instant);
  renderDetail();
  setInert(false);
  $(`#q-list [data-key="${CSS.escape(S.sel || "")}"]`)?.focus({ preventScroll: true });
}

function setInert(inert) {
  S.ctx.setModal(inert);
  for (const el of [$("#q-head"), $("#q-bays"), $("#q-list")]) el?.toggleAttribute("inert", inert);
}

// --- Events ---------------------------------------------------------------------------------

/** Listeners go on the shell's #view element, which outlives this view, so unmount must remove
 *  them; otherwise each visit adds another set and one click clears two strips. */
function wire(el) {
  const offs = [
    on(el, "click", "#q-list .strip", (ev, b) => {
      select(b.dataset.key, { scroll: false });
      if (matchMedia(PHONE).matches) openSheet({ instant: ev.detail === 0 });
    }),
    on(el, "click", "#q-bays .bay", (ev, b) => setBay(b.dataset.bay)),
    on(el, "click", "[data-goto]", (ev, b) => setBay(b.dataset.goto)),
    on(el, "change", "#q-run", (ev, s) => {
      S.sel = null;
      loadRun(+s.value).catch((err) => S.ctx.toast(err.message, { kind: "error" }));
    }),
    on(el, "click", "[data-reason]", (ev, b) => toggleReason(b.dataset.id, b.dataset.reason)),
    on(el, "click", "[data-act]", (ev, b) => onAct(ev, b)),
    on(el, "input", "#q-detail textarea", (ev, ta) => updateEditor(ta.closest(".editor"), true)),
    on(el, "input", "#q-note", () => {
      const e = selected();
      if (e) S.notes.set(e.key, $("#q-note").value);
    }),
  ];
  return () => offs.forEach((off) => off());
}

function onAct(ev, b) {
  const e = selected();
  const act = b.dataset.act;
  if (act === "copy") return copyHandoff();
  if (act === "retry") {
    return loadRuns(S.runId).catch((err) => {
      S.error = err;
      S.loading = false;
      render();
    });
  }
  if (act === "back") return closeSheet({ instant: ev.detail === 0 });
  if (!e) return;
  if (act === "clear") return verdict(e, "approved");
  if (act === "strike") return verdict(e, "killed");
  if (act === "reset") return verdict(e, "pending");
  if (act === "release") return release(e);
  if (act === "take") return submitTake(e);
  if (act === "revert") {
    S.edits.delete(e.key);
    const ta = $("#q-detail textarea");
    ta.value = e.a.text;
    updateEditor(ta.closest(".editor"), false);
    return;
  }
  if (act === "use") {
    S.edits.set(e.key, b.dataset.text);
    renderDetail();
    focusEditor();
    S.ctx.readback(html`<b>LOADED</b> a draft into the editor`);
  }
}

registerView({
  id: "queue",
  title: "Queue",
  group: "checkpoint",
  css: "/static/css/views/queue.css",
  count: () => {
    if (!S.actions.length) return 0;
    const g = groups();
    return g.clear.length + g.take.length;
  },
  hot: () => true,
  keys: [
    ["j / k", "Next or previous strip"],
    ["a", "Clear (approve), release a hold, or clear your take"],
    ["x", "Strike (kill)"],
    ["1 / 7", "After striking: tag why"],
    ["e", "Edit the draft"],
    ["⌘ / ↵", "Clear from the editor"],
    ["esc", "Stop editing"],
    ["z", "Undo the last verdict"],
    ["u", "Return a cleared or struck strip to the queue"],
    ["n", "Write a note"],
    ["o", "Open the target post on X"],
    ["[ / ]", "Previous or next bay"],
  ],
  async mount(el, ctx, params) {
    S.el = el;
    S.ctx = ctx;
    S.bayExplicit = BAYS.some(([b]) => b === params.bay);
    S.bay = S.bayExplicit ? params.bay : "clear";
    S.sel = params.sel || null;
    S.detailOpen = false;
    render();
    unwire = wire(el);
    setHints([
      ["j k", "move"],
      ["a", "clear"],
      ["x", "strike"],
      ["e", "edit"],
      ["z", "undo"],
      ["?", "all keys"],
    ]);
    S.ageTimer = setInterval(() => {
      // Post ages tick like X's do; only the age labels change.
      for (const span of $$("[data-age-from]", el)) span.textContent = `· ${xAge(minutesSince(span.dataset.ageFrom), span.dataset.ageFrom)}`;
    }, 30_000);
    await loadRuns(params.run);
  },
  unmount() {
    setInert(false);
    clearInterval(S.ageTimer);
    clearTimeout(S.gateTimer);
    unwire?.();
    unwire = null;
    S.el = null;
  },
  onKey,
});
