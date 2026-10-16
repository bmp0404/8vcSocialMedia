// Memory: what the harness believes now, the evidence behind each belief, and when it last held up.
// This is the Learn step. GET /api/memory carries harness.learning.views' rows (sam/memory), whose
// keys are the contract; without that package it sends only memory/lessons.md and outcomes, and
// the view says so. Evidence posts load on demand from GET /api/memory/notes.

import { registerView } from "../shell.js";
import { $, $$, html, isTyping, on, raw, setHTML } from "../lib/dom.js";
import { clock, compact, fixed, human } from "../lib/format.js";

const NOTES_PER_REQUEST = 50; // memory_api.get_notes answers at most 50 ids per request

const BAYS = [
  { id: "lessons", label: "Lessons" },
  { id: "reflections", label: "Reflections" },
  { id: "edits", label: "Reviewer edits" },
  { id: "outcomes", label: "Outcomes" },
];
const FILTERS = [
  ["all", "All", () => true],
  ["notkept", "Not kept", (r) => r.status !== "supported"],
  ["supported", "Supported", (r) => r.status === "supported"],
];
// X API v2 public_metrics names first, then the shorter names `harness record` files often use.
const METRICS = [
  ["Views", ["impression_count", "impressions", "views"], "view", "views"],
  ["Likes", ["like_count", "likes"], "like", "likes"],
  ["Replies", ["reply_count", "replies"], "reply", "replies"],
  ["Reposts", ["retweet_count", "reposts", "retweets"], "repost", "reposts"],
  ["Quotes", ["quote_count", "quotes"], "quote", "quotes"],
  ["Bookmarks", ["bookmark_count", "bookmarks"], "bookmark", "bookmarks"],
];

let view = null; // state for the mounted view
let dueCount = 0; // lessons due for a retest; feeds the rail badge and outlives a mount

registerView({
  id: "memory",
  title: "Memory",
  group: "learn",
  css: "/static/css/views/memory.css",
  keys: [
    ["1 / 2 / 3 / 4", "Lessons, reflections, reviewer edits, outcomes"],
    ["j / k", "Next or previous strip"],
    ["o", "Show or hide evidence posts"],
  ],
  count: () => dueCount,
  async mount(el, ctx, params) {
    const token = {};
    view = { token, el, off: [] };
    setHTML(el, skeleton());
    const data = await ctx.api.get("/api/memory");
    if (view?.token !== token) return; // navigated away while loading
    view = makeState(token, el, ctx, data, params);
    dueCount = view.lessons.filter(isDue).length;
    ctx.refreshNav();
    render(true);
    bind(view);
  },
  unmount() {
    view?.off.forEach((off) => off());
    view = null;
  },
  onKey(e) {
    if (!view?.data || isTyping(e.target) || e.ctrlKey) return false;
    if (view.available && /^[1-4]$/.test(e.key)) {
      switchBay(BAYS[+e.key - 1].id);
      return true;
    }
    if (e.key === "j" || e.key === "k") return moveStrip(e.key === "j" ? 1 : -1);
    const onStrip = e.target.classList?.contains("mem-strip");
    if (e.key === "o" || (e.key === "Enter" && onStrip)) {
      const strip = document.activeElement?.closest?.(".mem-strip") || $(".mem-strip[data-cur]", view.el);
      if (!strip?.querySelector("[data-act=evidence]")) return false;
      toggleEvidence(strip);
      return true;
    }
    return false;
  },
});

// --- State ----------------------------------------------------------------------------------

const arr = (x) => (Array.isArray(x) ? x : []);

function makeState(token, el, ctx, data, params) {
  const lessons = arr(data.lessons);
  const reflections = arr(data.reflections);
  const lessonOf = new Map(); // reflection id -> the lesson whose tests include it
  for (const l of lessons) for (const r of arr(l.reflections)) if (!lessonOf.has(r)) lessonOf.set(r, l.id);
  return {
    token,
    el,
    ctx,
    data,
    off: [],
    available: data.available !== false,
    lessons,
    reflections,
    procedures: arr(data.procedures),
    outcomes: arr(data.outcomes),
    reflIds: new Set(reflections.map((r) => r.id)),
    lessonOf,
    bay: BAYS.some((b) => b.id === params.bay) ? params.bay : "lessons",
    filter: FILTERS.some(([id]) => id === params.filter) ? params.filter : "all",
    open: new Set(), // strip keys whose evidence is showing
    notes: new Map(), // note id -> note, filled on demand
    missing: new Set(), // ids the store didn't have
    errors: new Map(), // strip key -> the error from loading its evidence
    entering: false,
  };
}

// --- Dates: the store writes UTC days (YYYY-MM-DD), so compare against today's UTC day ---------

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
const asDay = (s) => (typeof s === "string" && DAY_RE.test(s.slice(0, 10)) ? s.slice(0, 10) : null);
const utcToday = () => new Date().toISOString().slice(0, 10);
const daysBetween = (a, b) => Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / 864e5);

function fmtDay(s) {
  const d = asDay(s);
  if (!d) return "–";
  const sameYear = d.slice(0, 4) === utcToday().slice(0, 4);
  return new Date(`${d}T00:00:00Z`).toLocaleDateString("en", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

function agoDays(s) {
  const d = asDay(s);
  if (!d) return "";
  const n = daysBetween(d, utcToday());
  if (n <= 0) return "today";
  if (n === 1) return "yesterday";
  return `${compact(n)} days ago`;
}

/** Days until a lesson's retest date; null without one. */
const daysLeft = (s) => (asDay(s) ? daysBetween(utcToday(), asDay(s)) : null);

function isDue(l) {
  if (l.status === "retired") return false;
  if (l.status === "stale") return true;
  const n = daysLeft(l.valid_until);
  return n != null && n <= 0;
}

function untilText(l) {
  if (l.status === "retired") return "retired, not retested";
  const n = daysLeft(l.valid_until);
  if (n == null) return l.status === "stale" ? "due now" : "no date set";
  if (n < 0) return `overdue by ${plural(-n, "day")}`;
  if (n === 0) return "due today";
  if (l.status === "stale") return "due now, new posts arrived";
  return n === 1 ? "tomorrow" : `in ${compact(n)} days`;
}

// --- Small formatters -----------------------------------------------------------------------

const plural = (n, word) => `${compact(n)} ${n === 1 ? word : `${word}s`}`;
const count = (n) => (n == null || Number.isNaN(+n) ? "–" : compact(n));
const capFirst = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const sentence = (s) => (/[.!?]$/.test(String(s).trim()) ? String(s).trim() : `${String(s).trim()}.`);
const isHttp = (u) => typeof u === "string" && /^https?:\/\//i.test(u);

function pValue(p) {
  if (p == null || Number.isNaN(+p)) return null;
  return +p < 0.001 ? "p < 0.001" : `p = ${(+p).toPrecision(2)}`;
}

function metricValue(actual, keys) {
  for (const k of keys) if (actual && actual[k] != null) return actual[k];
  return undefined;
}

// --- Render ---------------------------------------------------------------------------------

function render(first = false) {
  const v = view;
  v.entering = first;
  setHTML(
    v.el,
    html`<div class="mem">
      ${head(v)}
      ${v.available
        ? html`${bays(v)}
            <section class="mem-tray" id="mem-tray" role="tabpanel" aria-labelledby="mem-tab-${v.bay}">${bayBody(v)}</section>`
        : unavailable(v)}
    </div>`,
  );
  v.entering = false;
}

function head(v) {
  const c = v.data.counts || {};
  const bits = [
    ["episode", "episode"],
    ["reflection", "reflection"],
    ["lesson", "lesson"],
  ]
    .filter(([k]) => c[k] != null)
    .map(([k, word]) => html`<span><b>${compact(c[k])}</b> ${c[k] === 1 ? word : `${word}s`}</span>`);
  return html`<header class="viewhead">
    <div>
      <h1>Memory</h1>
      <p class="sub">What the harness believes now, the evidence behind each belief, and when it last held up.</p>
    </div>
    ${v.available && (bits.length || v.data.store)
      ? html`<p class="actions mem-counts">
          ${bits}${v.data.store ? html`<span class="mem-store" title="The memory store this server reads">${v.data.store}</span>` : ""}
        </p>`
      : ""}
  </header>`;
}

function bays(v) {
  const n = {
    lessons: v.lessons.length,
    reflections: v.reflections.length,
    edits: v.procedures.reduce((s, p) => s + arr(p.pairs).length, 0),
    outcomes: v.outcomes.length,
  };
  return html`<div class="bays mem-bays" role="tablist" aria-label="Memory">
    ${BAYS.map((b) => {
      const on = b.id === v.bay;
      const attn = b.id === "lessons" && dueCount > 0;
      return html`<button
        class="bay ${attn ? "attn" : ""}"
        type="button"
        role="tab"
        id="mem-tab-${b.id}"
        data-bay="${b.id}"
        aria-selected="${String(on)}"
        aria-controls="mem-tray"
        tabindex="${on ? "0" : "-1"}"
      >
        <span>${b.label}</span><span class="n" ${attn ? raw(`title="${dueCount} due for a retest"`) : ""}>${compact(n[b.id])}</span>
      </button>`;
    })}
  </div>`;
}

function bayBody(v) {
  if (v.bay === "reflections") return reflectionsBay(v);
  if (v.bay === "edits") return editsBay(v);
  if (v.bay === "outcomes") return outcomesBay(v);
  return lessonsBay(v);
}

const bayHead = (title, aside, extra = "") =>
  html`<div class="section-head mem-bayhead"><h2>${title}</h2><p class="aside">${aside}</p>${extra}</div>`;

const emptyState = (title, body) => html`<div class="state mem-empty"><h3>${title}</h3><p>${body}</p></div>`;

const listAttrs = (v) => (v.entering ? raw("data-enter") : "");
const LONG_EDIT = 560; // characters; longer pairs start clamped so one edit can't fill the screen

// --- Lessons --------------------------------------------------------------------------------

function lessonsBay(v) {
  const head = bayHead(
    "Lessons",
    "Advice the drafting minds read. Each began as a reflection that held up on held-out posts from the same accounts.",
  );
  if (!v.lessons.length) {
    return html`${head}
      ${emptyState(
        "No lessons yet",
        html`A lesson is a reflection that held up on held-out posts. Load the team's dataset with
          <code>python -m harness ingest-dataset</code>, then run <code>python -m harness reflect</code>.`,
      )}
      ${mdBox(v)}`;
  }
  const due = v.lessons.filter(isDue).length;
  return html`${head}
    ${due
      ? html`<p class="notice warn mem-due">
          <b>${plural(due, "lesson")} due for a retest.</b>
          <span>Run <code>python -m harness revalidate</code> to test ${due === 1 ? "it" : "them"} on new held-out posts.</span>
        </p>`
      : ""}
    <ol class="mem-list" ${listAttrs(v)}>
      ${v.lessons.map((l, i) => html`<li>${lessonStrip(v, l, i)}</li>`)}
    </ol>
    ${mdBox(v)}`;
}

function lessonStrip(v, l, i) {
  const key = `lesson:${l.id}`;
  const open = v.open.has(key);
  const retired = l.status === "retired";
  const due = isDue(l);
  const evidence = arr(l.evidence);
  const refs = arr(l.reflections);
  const polarity = l.polarity === "do" || l.polarity === "avoid" ? l.polarity : null;
  const status = retired ? "Retired" : due ? "Retest due" : l.status === "active" || !l.status ? "Active" : capFirst(human(l.status));
  const scope = l.scope === "general" ? "General" : l.domain ? `${l.domain} only` : l.scope ? human(l.scope) : "";
  return html`<article
    class="strip mem-strip mem-lesson"
    data-key="${key}"
    data-state="${retired ? "retired" : due ? "stale" : "active"}"
    ${retired ? "data-held" : ""}
    ${open ? "data-open" : ""}
    tabindex="-1"
    aria-labelledby="mem-l${i}"
  >
    <div class="mem-main">
      <div class="mem-top">
        ${polarity
          ? html`<span class="stamp ${polarity === "do" ? "cleared" : "struck"}">${polarity === "do" ? "Do" : "Avoid"}</span>`
          : html`<span class="stamp held">${l.polarity || "No polarity"}</span>`}
        <span class="mem-scope">${scope}</span>
        <span class="mem-status">${status}</span>
      </div>
      <h3 class="mem-text" id="mem-l${i}" dir="auto">${l.text || "(no advice text)"}</h3>
      <div class="mem-proof">${measure(l.evidence_sentence)}${testedAs(l.tested_as)}</div>
      ${caveats(l.caveats)}
    </div>
    <dl class="mem-boxes">
      <div class="mem-box mem-tally">
        <dt class="label">Held-out tests</dt>
        <dd>
          <span><b>${count(l.support)}</b>supporting</span>
          <span ${+l.contradict > 0 ? "data-bad" : ""}><b>${count(l.contradict)}</b>contradicting</span>
          <span><b>${count(l.inconclusive)}</b>inconclusive</span>
        </dd>
      </div>
      <div class="mem-box">
        <dt class="label">${retired ? "Last tested" : "Last held up"}</dt>
        <dd><b>${fmtDay(l.updated)}</b><small>${agoDays(l.updated)}</small></dd>
      </div>
      <div class="mem-box" ${due ? "data-due" : ""}>
        <dt class="label">Retest by</dt>
        <dd><b>${retired ? "–" : fmtDay(l.valid_until)}</b><small>${untilText(l)}</small></dd>
      </div>
    </dl>
    <div class="mem-foot">
      ${evidenceButton(open, `mem-ev-l${i}`, evidence.length ? `${plural(evidence.length, "evidence post")}` : "")}
      ${refs.length
        ? html`<p class="mem-refs">
            <span class="label">Tested in</span>
            ${refs.map((id) =>
              v.reflIds.has(id)
                ? html`<button class="mem-ref" type="button" data-ref="${id}" data-to="reflections" title="Open this reflection">${id}</button>`
                : html`<span class="mem-ref">${id}</span>`,
            )}
          </p>`
        : ""}
    </div>
    <div class="mem-ev" id="mem-ev-l${i}" ${open ? "" : "hidden"}>${open ? evidencePanel(v, key) : ""}</div>
  </article>`;
}

/** The held-out result as written by kinds.heldout_sentence. Each "N of M" is set in mono so the two
 *  shares line up when scanning; the wording stays exactly as the memory worker wrote it. */
function measure(sentence, extra = "") {
  if (!sentence) return html`<p class="mem-measure" data-untested>No held-out result recorded.</p>`;
  const untested = /^not tested/i.test(sentence);
  const parts = capFirst(String(sentence)).split(/(\d[\d,]*) of (\d[\d,]*)/);
  // split() with two groups yields [text, n, m, text, n, m, ...]
  const out = [];
  for (let k = 0; k < parts.length; k += 3) {
    out.push(parts[k]);
    if (k + 2 < parts.length) out.push(html`<b>${parts[k + 1]}</b> of <b>${parts[k + 2]}</b>`);
  }
  return html`<p class="mem-measure" ${untested ? "data-untested" : ""}>
    ${out}${extra ? html`<span class="mem-p">${extra}</span>` : ""}
  </p>`;
}

function testedAs(t) {
  if (!t) return "";
  // A rule like "media is video" reads as code; a yes/no question reads as prose.
  const question = /\?\s*$/.test(t);
  return html`<p class="mem-tested"><span class="label">Tested as</span><span class="${question ? "" : "mem-rule"}">${t}</span></p>`;
}

function caveats(list) {
  const items = arr(list).filter(Boolean);
  if (!items.length) return "";
  return html`<ul class="mem-caveats">
    ${items.map((c) => html`<li><span class="label">Caveat</span><span>${capFirst(String(c))}</span></li>`)}
  </ul>`;
}

function evidenceButton(open, controls, what) {
  if (!what) return html`<span class="mem-none">No evidence posts recorded</span>`;
  return html`<button
    class="btn btn-quiet btn-small mem-evbtn"
    type="button"
    data-act="evidence"
    aria-expanded="${String(open)}"
    aria-controls="${controls}"
  >
    ${open ? "Hide" : "Show"} ${what}
  </button>`;
}

function mdBox(v) {
  if (!v.data.lessons_md) return "";
  return html`<details class="mem-mdbox">
    <summary><span class="label">What the minds read</span><span class="mem-store">memory/lessons.md</span></summary>
    <div class="mem-md">${markdown(v.data.lessons_md)}</div>
  </details>`;
}

// --- Reflections ----------------------------------------------------------------------------

function reflectionsBay(v) {
  const all = v.reflections;
  const seg = all.length
    ? html`<div class="mem-seg" role="group" aria-label="Show reflections">
        ${FILTERS.map(
          ([id, label, fn]) =>
            html`<button type="button" data-filter="${id}" aria-pressed="${String(v.filter === id)}">
              ${label}<span class="n">${compact(all.filter(fn).length)}</span>
            </button>`,
        )}
      </div>`
    : "";
  const head = bayHead(
    "Reflections",
    "Every reason the harness proposed for why posts failed or succeeded, tested on held-out posts from the same accounts. Rejected and inconclusive ones were tried and not kept.",
    seg,
  );
  if (!all.length) {
    return html`${head}${emptyState(
      "No reflections yet",
      html`<code>python -m harness reflect</code> proposes reasons same-account posts failed or succeeded, then tests each
        one on posts it wasn't drawn from.`,
    )}`;
  }
  const fn = FILTERS.find(([id]) => id === v.filter)[2];
  const rows = all.filter(fn);
  if (!rows.length) {
    const which = v.filter === "supported" ? "No reflection has held up yet." : "Every reflection held up.";
    return html`${head}${emptyState(which, "Switch the filter to All to see every reflection.")}`;
  }
  return html`${head}
    <ol class="mem-list" ${listAttrs(v)}>
      ${rows.map((r, i) => html`<li>${reflectionStrip(v, r, i)}</li>`)}
    </ol>`;
}

function reflectionStrip(v, r, i) {
  const key = `refl:${r.id}`;
  const open = v.open.has(key);
  const status = r.status || "untested";
  const stamp = status === "supported" ? "cleared" : status === "rejected" ? "struck" : "held";
  const explains = r.outcome === "failure" ? "Explains failures" : r.outcome === "success" ? "Explains successes" : "";
  const nEv = arr(r.evidence).length;
  const nCon = arr(r.contrast).length;
  const what = [nEv ? plural(nEv, "evidence post") : "", nCon ? plural(nCon, "contrast post") : ""].filter(Boolean).join(" and ");
  // A lesson's `reflections` lists every test of it, so only a supported one was kept as that lesson.
  const lesson = v.lessonOf.get(r.id);
  return html`<article
    class="strip mem-strip mem-refl"
    data-key="${key}"
    data-state="${status}"
    ${status === "supported" ? "" : "data-held"}
    ${open ? "data-open" : ""}
    tabindex="-1"
    aria-labelledby="mem-r${i}"
  >
    <div class="mem-top">
      <span class="stamp ${stamp}">${capFirst(human(status))}</span>
      <span class="mem-scope">${[explains, r.domain].filter(Boolean).join(" · ")}</span>
      <span class="mem-status mem-date">${fmtDay(r.created)}</span>
    </div>
    <h3 class="mem-text" id="mem-r${i}" dir="auto">${r.claim || "(no claim text)"}</h3>
    <div class="mem-proof">${measure(r.evidence_sentence, pValue(r.p_value))}${testedAs(r.tested_as)}</div>
    ${caveats(r.caveats)}
    <div class="mem-foot">
      ${evidenceButton(open, `mem-ev-r${i}`, what)}
      ${lesson
        ? html`<p class="mem-refs">
            <span class="label">${status === "supported" ? "Kept as" : "Retested"}</span>
            <button class="mem-ref" type="button" data-ref="${lesson}" data-to="lessons" title="Open the lesson">${lesson}</button>
          </p>`
        : ""}
    </div>
    <div class="mem-ev" id="mem-ev-r${i}" ${open ? "" : "hidden"}>${open ? evidencePanel(v, key) : ""}</div>
  </article>`;
}

// --- Evidence posts -------------------------------------------------------------------------

function evidenceGroups(v, key) {
  const [kind, id] = [key.slice(0, key.indexOf(":")), key.slice(key.indexOf(":") + 1)];
  if (kind === "lesson") {
    const l = v.lessons.find((x) => x.id === id);
    return l ? { groups: [["Posts behind this lesson", arr(l.evidence)]], extra: [] } : null;
  }
  const r = v.reflections.find((x) => x.id === id);
  if (!r) return null;
  const other = r.outcome === "failure" ? "successes" : "failures";
  return {
    groups: [
      [r.outcome === "failure" ? "Failures the claim explains" : r.outcome === "success" ? "Successes the claim explains" : "Evidence", arr(r.evidence)],
      [`Contrast: ${other} from the same accounts, without the trait`, arr(r.contrast)],
    ],
    extra: [
      r.predicate && r.predicate !== r.tested_as ? ["Asked as", r.predicate] : null,
      r.group ? ["Batch", r.group] : null,
    ].filter(Boolean),
  };
}

function evidencePanel(v, key) {
  const spec = evidenceGroups(v, key);
  if (!spec) return "";
  const err = v.errors.get(key);
  const extra = spec.extra.length
    ? html`<dl class="kv mem-kv">${spec.extra.map(([k, val]) => html`<dt>${k}</dt><dd>${val}</dd>`)}</dl>`
    : "";
  const groups = spec.groups.filter(([, ids]) => ids.length);
  return html`${extra}
    ${groups.map(([title, ids]) => {
      const found = ids.map((id) => v.notes.get(id)).filter(Boolean);
      const missing = ids.filter((id) => v.missing.has(id));
      const waiting = ids.length - found.length - missing.length;
      return html`<section class="mem-evgroup">
        <h4 class="label">${title} <span class="mem-n">${compact(ids.length)}</span></h4>
        ${found.length ? html`<ol class="mem-eps">${found.map(episodeRow)}</ol>` : ""}
        ${waiting && !err
          ? html`<ol class="mem-eps" aria-busy="true" aria-label="Loading posts">
              ${Array.from({ length: Math.min(waiting, 3) }, () => html`<li class="mem-ep"><span class="skeleton w40"></span><span class="skeleton w90"></span><span class="skeleton w60"></span></li>`)}
            </ol>`
          : ""}
        ${missing.length
          ? html`<p class="mem-missing">
              ${plural(missing.length, "post")} not in the store:
              <span class="mem-store">${missing.slice(0, 5).join(", ")}${missing.length > 5 ? ` and ${compact(missing.length - 5)} more` : ""}</span>
            </p>`
          : ""}
      </section>`;
    })}
    ${err
      ? html`<div class="notice warn mem-everr" role="alert">
          <span><b>Couldn't load evidence posts.</b> ${sentence(err.message || String(err))}</span>
          <button class="btn btn-quiet btn-small" type="button" data-act="retry-evidence">Try again</button>
        </div>`
      : ""}`;
}

function episodeRow(n) {
  const f = n.features || {};
  const outcomes = arr(n.outcomes);
  const facts = [
    f.media ? (f.media === "none" ? "no media" : human(f.media)) : "",
    f.n_chars != null ? `${compact(f.n_chars)} chars` : "",
    f.is_launch ? "launch" : "",
  ].filter(Boolean);
  const when = asDay(n.created_at) || asDay(n.created);
  return html`<li class="mem-ep">
    <div class="mem-ep-top">
      <span class="mem-ep-who">${n.author_username ? `@${n.author_username}` : n.id}</span>
      ${when ? html`<span class="mem-ep-when">${fmtDay(when)}</span>` : ""}
      ${outcomes.filter((o) => o.label || o.raw).map(outcomeChip)}
      ${isHttp(n.url) ? html`<a class="mem-ep-link" href="${n.url}" target="_blank" rel="noopener noreferrer">Open on X<span aria-hidden="true"> ↗</span></a>` : ""}
    </div>
    <p class="mem-ep-text" dir="auto">${n.text || "(no text)"}</p>
    ${outcomes
      .filter((o) => o.metrics && Object.keys(o.metrics).length)
      .map((o) => html`<p class="mem-ep-meta">${metricsLine(o.metrics)}${facts.length ? ` · ${facts.join(" · ")}` : ""}</p>`)}
    ${!outcomes.some((o) => o.metrics && Object.keys(o.metrics).length) && facts.length
      ? html`<p class="mem-ep-meta">${facts.join(" · ")}</p>`
      : ""}
  </li>`;
}

function outcomeChip(o) {
  const cls = o.label === "success" ? "ok" : o.label === "failure" ? "bad" : "";
  const text = `${capFirst(human(o.source || "outcome"))}: ${o.label || "unlabeled"}${o.raw ? ` (${o.raw})` : ""}`;
  return html`<span class="chip ${cls}" title="${o.note || ""}">${text}</span>`;
}

function metricsLine(m) {
  const known = new Set(METRICS.flatMap(([, keys]) => keys));
  const parts = METRICS.map(([, keys, one, many]) => {
    const val = metricValue(m, keys);
    return val == null ? "" : `${count(val)} ${+val === 1 ? one : many}`;
  }).filter(Boolean);
  for (const [k, val] of Object.entries(m)) if (!known.has(k)) parts.push(`${human(k)} ${count(val)}`);
  return parts.join(" · ");
}

// --- Reviewer edits -------------------------------------------------------------------------

function editsBay(v) {
  const procs = v.procedures.filter((p) => arr(p.pairs).length);
  const head = bayHead(
    "Reviewer edits",
    "How reviewers rewrote each mind's drafts before clearing or striking them. The minds learn from these pairs.",
  );
  if (!procs.length) {
    return html`${head}${emptyState(
      "No reviewer edits yet",
      html`When a reviewer edits a draft in the Queue before clearing or striking it, <code>python -m harness learn</code>
        records the draft and the kept text here, grouped by mind.`,
    )}`;
  }
  return html`${head}
    ${procs.map((p, pi) => {
      const pairs = arr(p.pairs);
      return html`<section class="mem-mind" aria-labelledby="mem-m${pi}">
        <div class="section-head">
          <h3 id="mem-m${pi}">${p.mind || "unknown mind"}</h3>
          <span class="aside">${plural(pairs.length, "edit")}${asDay(p.updated) ? ` · last ${fmtDay(p.updated)}` : ""}</span>
        </div>
        <ol class="mem-list" ${listAttrs(v)}>
          ${pairs.map((e) => html`<li>${editStrip(e)}</li>`)}
        </ol>
      </section>`;
    })}`;
}

function editStrip(e) {
  const verdict = e.verdict === "approved" ? ["cleared", "Cleared"] : e.verdict === "killed" ? ["struck", "Struck"] : ["held", e.verdict ? human(e.verdict) : "No verdict"];
  const d = wordDiff(String(e.original ?? ""), String(e.edited ?? ""));
  const long = Math.max(String(e.original ?? "").length, String(e.edited ?? "").length) > LONG_EDIT;
  return html`<article class="strip mem-strip mem-edit" tabindex="-1" ${long ? "data-long" : ""}>
    <div class="mem-top">
      <span class="stamp ${verdict[0]}">${verdict[1]}</span>
      <span class="mem-scope mem-id">${e.action_id || ""}</span>
      <span class="mem-status mem-date">${e.at ? clock(e.at, true) : ""}</span>
    </div>
    <div class="mem-diff">
      <p class="mem-drow"><span class="label">Draft</span><span class="mem-dtext" dir="auto">${d ? marked(d.a, d.keepA, "del") : e.original || "(empty)"}</span></p>
      <p class="mem-drow"><span class="label">Kept</span><span class="mem-dtext" dir="auto">${d ? marked(d.b, d.keepB, "ins") : e.edited || "(empty)"}</span></p>
    </div>
    ${long ? html`<button class="btn btn-quiet btn-small mem-whole" type="button" data-act="whole" aria-expanded="false">Show the whole edit</button>` : ""}
    ${e.note ? html`<p class="mem-note"><span class="label">Note</span><span>${e.note}</span></p>` : ""}
    ${diffStats(e.diff)}
  </article>`;
}

/** Word-level LCS diff: which words of each side survive into the other. Null when too long to diff. */
function wordDiff(a, b) {
  const A = a.split(/\s+/).filter(Boolean);
  const B = b.split(/\s+/).filter(Boolean);
  if (!A.length || !B.length || A.length * B.length > 250_000) return null;
  const m = B.length;
  const dp = Array.from({ length: A.length + 1 }, () => new Uint16Array(m + 1));
  for (let i = A.length - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  }
  const keepA = new Array(A.length).fill(false);
  const keepB = new Array(m).fill(false);
  for (let i = 0, j = 0; i < A.length && j < m; ) {
    if (A[i] === B[j]) {
      keepA[i++] = true;
      keepB[j++] = true;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) i++;
    else j++;
  }
  return { a: A, b: B, keepA, keepB };
}

function marked(words, keep, tag) {
  const out = [];
  let run = [];
  const flush = () => {
    if (!run.length) return;
    out.push(tag === "del" ? html`<del>${run.join(" ")}</del>` : html`<ins>${run.join(" ")}</ins>`);
    run = [];
  };
  words.forEach((w, k) => {
    if (keep[k]) {
      flush();
      out.push(html`${w}`);
    } else run.push(w);
  });
  flush();
  return raw(out.join(" "));
}

function diffStats(d) {
  if (!d || typeof d !== "object" || !Object.keys(d).length) return "";
  const bits = [
    d.chars_before != null && d.chars_after != null ? `${compact(d.chars_before)} → ${compact(d.chars_after)} chars` : "",
    d.similarity != null ? `${Math.round(d.similarity * 100)}% of words kept in place` : "",
    arr(d.hashtags_removed).length ? `removed ${d.hashtags_removed.join(" ")}` : "",
    arr(d.hashtags_added).length ? `added ${d.hashtags_added.join(" ")}` : "",
    arr(d.mentions_removed).length ? `removed ${d.mentions_removed.join(" ")}` : "",
    arr(d.mentions_added).length ? `added ${d.mentions_added.join(" ")}` : "",
    d.verify_removed ? "resolved a [VERIFY] placeholder" : "",
    d.verify_added ? "added a [VERIFY] placeholder" : "",
  ].filter(Boolean);
  return bits.length ? html`<p class="mem-dstats">${bits.join(" · ")}</p>` : "";
}

// --- Outcomes -------------------------------------------------------------------------------

function outcomesBay(v) {
  const rows = v.outcomes;
  const head = bayHead("Outcomes", "Each action's predicted score next to the real metrics recorded for it.");
  if (!rows.length) {
    return html`${head}${emptyState(
      "Nobody has recorded real metrics yet",
      html`After a post goes out, record its numbers with <code>python -m harness record &lt;file&gt;</code>, where the file is
        a JSON list of <code>{action_id, metrics}</code>. Each action then shows its predicted score next to what happened.`,
    )}`;
  }
  const known = new Set(METRICS.flatMap(([, keys]) => keys));
  const cols = METRICS.filter(([, keys]) => rows.some((r) => metricValue(r.actual, keys) != null));
  const extra = [...new Set(rows.flatMap((r) => Object.keys(r.actual || {}).filter((k) => !known.has(k))))].sort();
  for (const k of extra) cols.push([capFirst(human(k)), [k]]);
  return html`${head}
    <div class="mem-tablewrap" role="region" aria-label="Predicted score against actual metrics" tabindex="0">
      <table class="mem-table">
        <thead>
          <tr>
            <th scope="col">Action</th>
            <th scope="col" class="mem-numcol" title="The audience panel's score, 0 to 1">Predicted</th>
            ${cols.map(([label]) => html`<th scope="col" class="mem-numcol">${label}</th>`)}
          </tr>
        </thead>
        <tbody>
          ${rows.map((r) => {
            const a = r.action || {};
            return html`<tr>
              <th scope="row">
                <div class="mem-act">
                  <span class="mem-act-top">${[a.type ? human(a.type) : "", a.mind || ""].filter(Boolean).join(" · ") || a.id || "unknown action"}</span>
                  <span class="mem-act-text" dir="auto">${a.text || "(no text)"}</span>
                </div>
              </th>
              <td class="mem-numcol">${a.predicted_score == null ? "–" : fixed(a.predicted_score, 2)}</td>
              ${cols.map(([, keys]) => {
                const val = metricValue(r.actual, keys);
                return html`<td class="mem-numcol">${val == null ? "–" : count(val)}</td>`;
              })}
            </tr>`;
          })}
        </tbody>
      </table>
    </div>`;
}

// --- Unavailable: harness.learning isn't on this branch -------------------------------------

function unavailable(v) {
  return html`<div class="mem-unavail">
    <p class="notice">
      <span class="label">Unavailable</span>
      <span>${sentence(v.data.reason || "Structured memory isn't on this branch")} Showing <code>memory/lessons.md</code>, the text the drafting minds read, and recorded outcomes.</span>
    </p>
    <section class="mem-sec" aria-labelledby="mem-md-h">
      <div class="section-head mem-bayhead"><h2 id="mem-md-h">What the minds read</h2><p class="aside mem-store">memory/lessons.md</p></div>
      <div class="panel mem-md">${v.data.lessons_md ? markdown(v.data.lessons_md) : html`<p>memory/lessons.md is missing or empty.</p>`}</div>
    </section>
    <section class="mem-sec">${outcomesBay(v)}</section>
  </div>`;
}

// --- Markdown: the subset lessons.md uses. Every value is escaped before any markup is added. ----

function inline(text) {
  const codes = [];
  let s = html`${text}`.s.replace(/`([^`]+)`/g, (_, c) => {
    codes.push(c);
    return `\u0000${codes.length - 1}\u0000`;
  });
  s = s
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^\w*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/(^|[^\w])_([^_\s][^_]*)_(?!\w)/g, "$1<em>$2</em>")
    .replace(/\[\[([^\]]+)\]\]/g, '<span class="mem-store">$1</span>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) =>
      /^https?:\/\//i.test(url) ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>` : label,
    );
  return raw(s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`));
}

function markdown(src) {
  const lines = String(src).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let para = [];
  let list = null; // { tag, items }
  let quote = [];
  const flushPara = () => {
    if (para.length) out.push(html`<p>${inline(para.join(" "))}</p>`);
    para = [];
  };
  const flushList = () => {
    if (list) out.push(list.tag === "ol" ? html`<ol>${list.items}</ol>` : html`<ul>${list.items}</ul>`);
    list = null;
  };
  const flushQuote = () => {
    if (quote.length) out.push(html`<blockquote>${inline(quote.join(" "))}</blockquote>`);
    quote = [];
  };
  const flush = () => {
    flushPara();
    flushList();
    flushQuote();
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m;
    if (/^```/.test(line)) {
      flush();
      const code = [];
      while (++i < lines.length && !/^```/.test(lines[i])) code.push(lines[i]);
      out.push(html`<pre><code>${code.join("\n")}</code></pre>`);
    } else if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) {
      flush();
      // The page owns h1 and h2; a lessons.md "# Lessons" becomes h3.
      const level = Math.min(6, m[1].length + 2);
      out.push(raw(`<h${level}>${inline(m[2])}</h${level}>`));
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flush();
      out.push(html`<hr>`);
    } else if ((m = /^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$/.exec(line))) {
      flushPara();
      flushQuote();
      const tag = m[1] ? "ul" : "ol";
      if (list && list.tag !== tag) flushList();
      list ??= { tag, items: [] };
      list.items.push(html`<li>${inline(m[3])}</li>`);
    } else if ((m = /^>\s?(.*)$/.exec(line))) {
      flushPara();
      flushList();
      quote.push(m[1]);
    } else if (!line.trim()) {
      flush();
    } else if (list && /^\s+\S/.test(line)) {
      const last = list.items.pop();
      list.items.push(raw(last.s.replace(/<\/li>$/, ` ${inline(line.trim())}</li>`)));
    } else {
      flushList();
      flushQuote();
      para.push(line.trim());
    }
  }
  flush();
  return raw(out.join(""));
}

// --- Loading --------------------------------------------------------------------------------

function skeleton() {
  return html`<div class="mem" aria-busy="true">
    <header class="viewhead">
      <div>
        <h1>Memory</h1>
        <p class="sub">What the harness believes now, the evidence behind each belief, and when it last held up.</p>
      </div>
    </header>
    <div class="bays mem-bays" aria-hidden="true">${BAYS.map((b) => html`<span class="bay"><span>${b.label}</span></span>`)}</div>
    <div class="mem-tray">
      <ol class="mem-list" aria-label="Loading memory">
        ${[0, 1, 2].map(
          () => html`<li class="strip mem-strip mem-lesson mem-skel">
            <div class="mem-main">
              <span class="skeleton w-stamp"></span>
              <span class="skeleton w90 tall"></span>
              <span class="skeleton w60 tall"></span>
              <span class="skeleton w75"></span>
            </div>
            <div class="mem-boxes"><span class="skeleton w60"></span><span class="skeleton w40"></span></div>
          </li>`,
        )}
      </ol>
    </div>
  </div>`;
}

// --- Interaction ----------------------------------------------------------------------------

function bind(v) {
  const el = v.el;
  v.off.push(
    on(el, "click", "[data-bay]", (e, b) => switchBay(b.dataset.bay)),
    on(el, "click", "[data-filter]", (e, b) => {
      view.filter = b.dataset.filter;
      view.ctx.setParams({ filter: view.filter === "all" ? null : view.filter });
      renderTray();
    }),
    on(el, "click", "[data-act=evidence]", (e, b) => toggleEvidence(b.closest(".mem-strip"))),
    on(el, "click", "[data-act=retry-evidence]", (e, b) => {
      const strip = b.closest(".mem-strip");
      view.errors.delete(strip.dataset.key);
      loadEvidence(strip.dataset.key);
    }),
    on(el, "click", "[data-ref]", (e, b) => jumpTo(b.dataset.to, b.dataset.ref)),
    on(el, "click", "[data-act=whole]", (e, b) => {
      const open = b.closest(".mem-strip").toggleAttribute("data-open");
      b.setAttribute("aria-expanded", String(open));
      b.textContent = open ? "Show less" : "Show the whole edit";
    }),
    on(el, "keydown", "[role=tab]", (e, tab) => {
      const tabs = $$("[role=tab]", el);
      const i = tabs.indexOf(tab);
      const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
      if (next == null) return;
      e.preventDefault();
      switchBay(tabs[(next + tabs.length) % tabs.length].dataset.bay, true);
    }),
  );
}

function switchBay(id, focusTab = false) {
  const v = view;
  if (!v) return;
  if (!v.available || v.bay === id) {
    if (focusTab) $(`#mem-tab-${id}`, v.el)?.focus();
    return;
  }
  v.bay = id;
  v.ctx.setParams({ bay: id === "lessons" ? null : id });
  render();
  const main = $("#main");
  const tabs = $(".mem-bays", v.el);
  // A new bay starts at its top, as a new view does; keep the bays in view if they were.
  if (main && tabs && main.scrollTop > tabs.offsetTop) main.scrollTop = tabs.offsetTop;
  if (focusTab) $(`#mem-tab-${id}`, v.el)?.focus();
}

function renderTray() {
  const tray = $("#mem-tray", view.el);
  if (tray) setHTML(tray, bayBody(view));
  else render();
}

function strips() {
  return $$(".mem-tray .mem-strip:not(.mem-skel)", view.el);
}

function select(strip, { scroll = "nearest" } = {}) {
  for (const s of $$(".mem-strip[data-cur]", view.el)) s.removeAttribute("data-cur");
  strip.setAttribute("data-cur", "");
  strip.focus({ preventScroll: true });
  strip.scrollIntoView({ block: scroll });
}

function moveStrip(dir) {
  const list = strips();
  if (!list.length) return false;
  const here = document.activeElement?.closest?.(".mem-strip") || $(".mem-strip[data-cur]", view.el);
  const i = list.indexOf(here);
  const next = i < 0 ? (dir > 0 ? 0 : list.length - 1) : Math.min(list.length - 1, Math.max(0, i + dir));
  select(list[next]);
  return true;
}

function jumpTo(bay, id) {
  const v = view;
  const prefix = bay === "lessons" ? "lesson" : "refl";
  if (bay === "reflections" && v.filter !== "all") {
    const r = v.reflections.find((x) => x.id === id);
    const fn = FILTERS.find(([f]) => f === v.filter)[2];
    if (!r || !fn(r)) {
      v.filter = "all";
      v.ctx.setParams({ filter: null });
    }
  }
  if (v.bay !== bay) switchBay(bay);
  else renderTray();
  const strip = $(`.mem-strip[data-key="${CSS.escape(`${prefix}:${id}`)}"]`, v.el);
  if (strip) select(strip, { scroll: "center" });
}

function updateStrip(key) {
  const strip = $(`.mem-strip[data-key="${CSS.escape(key)}"]`, view.el);
  if (!strip) return;
  const open = view.open.has(key);
  const btn = $("[data-act=evidence]", strip);
  const region = $(".mem-ev", strip);
  strip.toggleAttribute("data-open", open);
  if (btn) {
    btn.setAttribute("aria-expanded", String(open));
    btn.textContent = btn.textContent.trim().replace(/^(Show|Hide)/, open ? "Hide" : "Show");
  }
  if (region) {
    region.hidden = !open;
    setHTML(region, open ? evidencePanel(view, key) : "");
  }
}

function toggleEvidence(strip) {
  const key = strip?.dataset.key;
  if (!key) return;
  if (view.open.has(key)) {
    view.open.delete(key);
    updateStrip(key);
    return;
  }
  view.open.add(key);
  loadEvidence(key);
}

async function loadEvidence(key) {
  const v = view;
  updateStrip(key); // shows what's cached, skeletons for the rest
  const spec = evidenceGroups(v, key);
  const ids = [...new Set((spec?.groups || []).flatMap(([, list]) => list))].filter((id) => !v.notes.has(id) && !v.missing.has(id));
  if (!ids.length) return;
  try {
    for (let i = 0; i < ids.length; i += NOTES_PER_REQUEST) {
      const chunk = ids.slice(i, i + NOTES_PER_REQUEST);
      const res = await v.ctx.api.get(`/api/memory/notes?ids=${encodeURIComponent(chunk.join(","))}`);
      if (view !== v) return;
      for (const n of arr(res?.notes)) if (n?.id) v.notes.set(n.id, n);
      // Fixture mode answers every id at once, so decide what's missing from what came back.
      for (const id of chunk) if (!v.notes.has(id)) v.missing.add(id);
    }
  } catch (e) {
    if (view !== v) return;
    v.errors.set(key, e);
  }
  if (v.open.has(key)) updateStrip(key);
}
