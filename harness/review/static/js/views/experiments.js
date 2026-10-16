// Experiments: the self-iteration ledger sam/iterate writes to eval/experiments/.
//
// Answers, in the order an operator asks them: which config is in force and since which
// experiment; what was tried and how the gate decided each one; for one experiment, what changed,
// how champion and challenger compare on every metric (effect and 95% interval against the noise
// floor), how the multiple-comparison correction treated it, and what it cost.
//
// Effects are plotted as improvement, so right is always better: the gate decides on improvement
// (stats.decide in harness/iterate), and its reasons quote improvement bounds. For a metric where
// lower is better, improvement is the drop. Zero sits mid-plot on every strip so a column of
// strips can be scanned for whiskers that clear the shaded noise floor.

import { registerView } from "../shell.js";
import { $, $$, html, isTyping, on, setHTML } from "../lib/dom.js";
import { clock, compact, duration, human, signed } from "../lib/format.js";
import { ciAxis, ciRow, domainOf, ticks } from "../components/ci.js";

const STATUS = {
  promoted: { stamp: "cleared", word: "Promoted", gist: "The challenger cleared the gate and was promoted into the champion." },
  rejected: { stamp: "struck", word: "Rejected", gist: "The challenger made the primary metric worse; the champion stays." },
  inconclusive: { stamp: "held", word: "Inconclusive", gist: "Not enough evidence to promote or reject; the champion stays." },
  pending: { stamp: "held", word: "Pending", gist: "Not evaluated: the run skipped this arm or an arm failed." },
  superseded: { stamp: "held", word: "Superseded", gist: "A person withdrew this result; it no longer counts toward the champion." },
};
// Bays follow the stamps: cleared, struck, and held for everything the gate didn't decide
// (inconclusive, pending, superseded, or a status this view doesn't know). The exact status is on
// each strip's stamp and tallied in the page subtitle.
const BAYS = [
  ["all", "All"],
  ["promoted", "Promoted"],
  ["rejected", "Rejected"],
  ["held", "Held"],
];
const TALLY = ["promoted", "rejected", "inconclusive", "pending", "superseded"];
const EMPTY_BAY = {
  promoted: "Nothing promoted yet. The gate promotes a challenger when the lower bound of its improvement clears the noise floor after correction.",
  rejected: "Nothing rejected. The gate rejects a challenger when the whole 95% interval of its improvement is below zero.",
  held: "Nothing held. Inconclusive, pending and superseded experiments wait here.",
};
const ARM = { champion: "Champion", champion_repeat: "Champion rerun" };

let S = null; // view state while mounted

const finite = (v) => typeof v === "number" && Number.isFinite(v);
const statusOf = (x) => (typeof x.decision?.status === "string" && x.decision.status) || "unknown";
const bucketOf = (x) => (statusOf(x) === "promoted" || statusOf(x) === "rejected" ? statusOf(x) : "held");
const metricsOf = (x) => (Array.isArray(x.eval?.metrics) ? x.eval.metrics.filter((m) => m && typeof m === "object") : []);
const primaryOf = (x) => metricsOf(x).find((m) => m.primary) || metricsOf(x)[0] || null;
const paramsOf = (x) => Object.entries(x.change?.params && typeof x.change.params === "object" ? x.change.params : {});
const short = (ref) => (typeof ref === "string" ? ref : "–");

/** Zero tiny float noise so -1e-17 doesn't print as −0.000. */
const clean = (v, d) => (Math.abs(v) < 0.5 * 10 ** -d ? 0 : v);
const num = (v, d = 3) => (finite(v) ? clean(v, d).toFixed(d).replace("-", "−") : "–");
const sgn = (v, d = 3) => (finite(v) ? signed(clean(v, d), d) : "–");
const interval = (ci, d = 3, f = num) => (Array.isArray(ci) && finite(ci[0]) && finite(ci[1]) ? `[${f(ci[0], d)}, ${f(ci[1], d)}]` : "");
const pval = (p) => (finite(p) ? (p < 0.0001 ? "< 0.0001" : p.toFixed(4)) : "–");

function fmtVal(v) {
  if (v === null) return "null";
  if (v === undefined) return "–";
  if (Array.isArray(v)) return v.length ? v.map(fmtVal).join(", ") : "(none)";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** Digits for a metric: rates and AUCs get three, counts like reply_chars get one. */
function digitsFor(m) {
  const vals = [m.champion?.value, m.challenger?.value, m.delta?.value].filter(finite);
  return vals.some((v) => Math.abs(v) > 1.5) ? 1 : 3;
}

/** A stat re-expressed so that positive means better. */
function improvement(stat, higherIsBetter) {
  if (!stat || !finite(stat.value)) return null;
  const s = higherIsBetter === false ? -1 : 1;
  const c = stat.ci95;
  const ci = Array.isArray(c) && finite(c[0]) && finite(c[1]) ? (s > 0 ? [c[0], c[1]] : [-c[1], -c[0]]) : null;
  return { value: s * stat.value, ci };
}

/** Fewest decimals that print every tick exactly (0.25 needs two, 0.5 one). */
function tickDigits(tv) {
  for (let d = 0; d < 4; d++) if (tv.every((t) => Math.abs(t * 10 ** d - Math.round(t * 10 ** d)) < 1e-6)) return d;
  return 4;
}

/** A domain centred on zero, so zero sits in the same place on every plot that uses it.
 *  With `nice`, the ends round out to tick values for a labelled axis. */
function centred(values, nice = false) {
  const m = Math.max(0, ...values.filter(finite).map(Math.abs)) || 0.05;
  if (!nice) return [-m * 1.12, m * 1.12];
  const [lo, hi] = domainOf([-m, m], { pad: 0.04, count: 6 });
  const r = Math.max(Math.abs(lo), Math.abs(hi));
  return [-r, r];
}

/** "6 experiments: 0 promoted, 0 rejected, 5 inconclusive, 1 superseded." */
function tally() {
  const by = {};
  for (const x of S.list) by[statusOf(x)] = (by[statusOf(x)] || 0) + 1;
  const extra = Object.keys(by).filter((k) => !TALLY.includes(k));
  const parts = [...TALLY, ...extra].filter((k) => by[k] || k === "promoted" || k === "rejected").map((k) => `${by[k] || 0} ${k === "unknown" ? "without a decision" : human(k)}`);
  return `${S.list.length} experiment${S.list.length === 1 ? "" : "s"}: ${parts.join(", ")}.`;
}

function changeLine(x) {
  const params = paramsOf(x);
  if (params.length === 1) {
    const [k, v] = params[0];
    return html`<span class="mono xp-knob">${k}</span> ${fmtVal(v?.from)} → ${fmtVal(v?.to)}`;
  }
  if (params.length > 1) return html`${params.length} knobs: <span class="mono xp-knob">${params.map(([k]) => k).join(", ")}</span>`;
  return x.change?.summary || "No knob change recorded";
}

function itemsLine(ev) {
  if (!ev || !finite(ev.n) || ev.n <= 0) return "";
  const unit = ev.unit || "item";
  const cu = ev.cluster_unit;
  return cu && cu !== unit ? `${ev.n} ${unit}s from ${ev.clusters} ${cu}s` : `${ev.n} ${unit}s`;
}

const isRevert = (x) => x.proposer?.kind === "revert";

function stamp(x) {
  const s = statusOf(x);
  const k = STATUS[s];
  const word = s === "promoted" && isRevert(x) ? "Reverted" : k?.word || (s === "unknown" ? "No decision" : human(s));
  return html`<span class="stamp ${k?.stamp || "held"}">${word}</span>`;
}

/** What the decision means for the champion, in words. */
function gist(x) {
  const s = statusOf(x);
  if (s === "promoted" && isRevert(x)) return `This revert undid ${x.proposer?.source || "an earlier promotion"}; the champion no longer carries its knob changes.`;
  const by = S.revertedBy.get(x.id);
  if (s === "promoted" && by) return `Promoted, then reverted by ${by}; the champion no longer carries its changes.`;
  return STATUS[s]?.gist || (s === "unknown" ? "This record has no decision." : `Status “${s}” isn't one this view knows; read the reason.`);
}

// --- Loading and unavailable ------------------------------------------------------------------

function skeleton() {
  const strip = html`<div class="strip xp-skel"><span class="skeleton" style="width:38%"></span><span class="skeleton" style="width:86%"></span><span class="skeleton" style="width:60%"></span></div>`;
  return html`<header class="viewhead"><div><h1>Experiments</h1><p class="sub">Loading the ledger…</p></div></header>
    <div class="xp" aria-busy="true">
      <div class="panel xp-champ"><div class="xp-skel-row"><span class="skeleton" style="width:24%"></span><span class="skeleton" style="width:18%"></span><span class="skeleton" style="width:12%"></span></div></div>
      <div class="xp-body">
        <div class="xp-side"><div class="xp-list">${[0, 1, 2, 3].map(() => strip)}</div></div>
        <div class="panel xp-detail"><div class="section"><span class="skeleton" style="width:30%"></span><span class="skeleton" style="width:90%"></span><span class="skeleton" style="width:72%"></span></div>
          <div class="section"><span class="skeleton" style="width:22%"></span><span class="skeleton" style="height:64px"></span></div></div>
      </div>
    </div>`;
}

function head(sub) {
  return html`<header class="viewhead"><div><h1>Experiments</h1><p class="sub">${sub}</p></div></header>`;
}

function unavailable(data) {
  return html`${head("The self-iteration ledger: challenger configs tested against the champion, and what the gate decided.")}
    <div class="xp">
      <p class="notice"><b>Unavailable.</b> <span>${data.reason || "The server has no experiment ledger on this branch."}</span></p>
      <div class="state">
        <span class="label">No ledger here</span>
        <h2>Experiments appear once eval/experiments/ exists</h2>
        <p>The iterate worker writes one record per experiment there when it runs <code>python -m harness iterate run --provider anthropic</code>, or when its branch (sam/iterate) is merged into this one. Mock runs write to <code>out/iterate/</code> on purpose and never show here.</p>
      </div>
    </div>`;
}

// --- Champion ---------------------------------------------------------------------------------

function championBlock() {
  const champ = S.data.champion;
  if (!champ || typeof champ !== "object") {
    return html`<p class="notice"><b>No champion.json.</b> <span>The ledger hasn't derived the config in force. iterate rewrites it after every run.</span></p>`;
  }
  const history = Array.isArray(champ.history) ? champ.history : [];
  const lastId = history.at(-1);
  const last = S.byId.get(lastId);
  const config = champ.config && typeof champ.config === "object" ? champ.config : {};
  const knobs = Object.entries(config);
  // The net effect of the history: a knob counts as changed only when it now differs from the value
  // the first record touching it started from, so a promotion undone by a revert drops out.
  const startedAt = new Map();
  const lastSet = new Map();
  for (const id of history) {
    for (const [k, v] of paramsOf(S.byId.get(id) || {})) {
      if (!startedAt.has(k)) startedAt.set(k, v?.from);
      lastSet.set(k, id);
    }
  }
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const changed = [...lastSet.entries()].filter(([k]) => k in config && !same(config[k], startedAt.get(k)));
  const restored = new Set([...lastSet.keys()].filter((k) => k in config && same(config[k], startedAt.get(k))));
  const nRev = history.filter((id) => isRevert(S.byId.get(id) || {})).length;
  const nProm = history.length - nRev;
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const groups = new Map();
  for (const [k, v] of knobs) {
    const g = k.includes(".") ? k.slice(0, k.indexOf(".")) : "other";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push([k, v]);
  }
  const since = lastId
    ? html`<span class="v">${last ? html`<button class="xp-link mono" type="button" data-goto="${lastId}">${lastId}</button>` : lastId}</span>
        <span class="xp-fsub">${last && isRevert(last) ? "reverted" : "promoted"}${last?.decision?.at ? ` ${clock(last.decision.at, true)}` : ""}</span>`
    : html`<span class="v ui">Defaults</span><span class="xp-fsub">nothing promoted yet</span>`;
  const note = changed.length
    ? html`<ul class="xp-changed" aria-label="Knobs that differ from their defaults">
        ${changed.slice(0, 3).map(([k, id]) => html`<li><span class="mono xp-knob">${k}</span> <span>${fmtVal(config[k])}</span> <span class="muted">set by</span> <button class="xp-link mono" type="button" data-goto="${id}">${id}</button></li>`)}
        ${changed.length > 3 ? html`<li class="muted">${changed.length - 3} more knobs differ from their defaults; open All knobs below.</li>` : ""}
      </ul>`
    : html`<p class="xp-champ-note">${restored.size ? "Every knob is back at its registry default: later records undid what earlier ones promoted." : "Every knob is at its registry default."}</p>`;
  return html`<section class="panel xp-champ" aria-labelledby="xp-champ-h">
    <h2 id="xp-champ-h" class="sr-only">Config in force</h2>
    <div class="fields xp-fields">
      <div class="field"><span class="label">Config in force</span><span class="v mono" title="${champ.config_hash || ""}">${short(champ.ref)}</span></div>
      <div class="field"><span class="label">Since</span>${since}</div>
      <div class="field"><span class="label">Applied</span><span class="v ui">${history.length ? `${plural(nProm, "promotion")}${nRev ? `, ${plural(nRev, "revert")}` : ""}` : "None"}</span></div>
      <div class="field"><span class="label">Knobs</span><span class="v">${knobs.length}</span></div>
    </div>
    ${history.length
      ? html`<ol class="xp-history" aria-label="How the champion got here, oldest first">
          ${history.map((id) => {
            const r = S.byId.get(id);
            if (!r) return html`<li><span class="mono">${id}</span> <span class="muted">isn't in this ledger</span></li>`;
            return html`<li>
              <span class="xp-hdate num">${clock(r.decision?.at || r.created_at, true)}</span>
              ${stamp(r)}
              <button class="xp-link mono" type="button" data-goto="${id}">${id}</button>
              <span class="xp-hchange">${changeLine(r)}</span>
              ${isRevert(r) && r.decision?.reason ? html`<span class="xp-hwhy">${r.decision.reason}</span>` : ""}
            </li>`;
          })}
        </ol>`
      : ""}
    ${note}
    ${knobs.length
      ? html`<details class="xp-config">
          <summary>All ${knobs.length} knobs</summary>
          <div class="xp-tablewrap"><table class="xp-table">
            <thead><tr><th scope="col">Knob</th><th scope="col">Value</th><th scope="col">Set by</th></tr></thead>
            ${[...groups.entries()].map(([g, rows]) => html`<tbody>
              <tr class="grp"><th scope="colgroup" colspan="3">${g}</th></tr>
              ${rows.map(([k, v]) => html`<tr><th scope="row" class="mono">${k}</th><td class="mono">${fmtVal(v)}</td><td>${lastSet.has(k)
                ? html`${restored.has(k) ? html`<span class="muted">default, restored by </span>` : ""}<button class="xp-link mono" type="button" data-goto="${lastSet.get(k)}">${lastSet.get(k)}</button>`
                : html`<span class="muted">default</span>`}</td></tr>`)}
            </tbody>`)}
          </table></div>
        </details>`
      : ""}
  </section>`;
}

// --- Ledger list ------------------------------------------------------------------------------

function counts() {
  const c = { all: S.list.length };
  for (const x of S.list) c[bucketOf(x)] = (c[bucketOf(x)] || 0) + 1;
  return c;
}

function bays() {
  const c = counts();
  return html`<div class="bays xp-bays" role="tablist" aria-label="Filter by decision">
    ${BAYS.map(([k, label]) => html`<button class="bay" type="button" role="tab" data-bay="${k}" aria-selected="${S.bay === k ? "true" : "false"}" aria-controls="xp-list" tabindex="${S.bay === k ? 0 : -1}">
      <span>${label}</span><span class="n">${c[k] || 0}</span></button>`)}
  </div>`;
}

function visible() {
  return S.bay === "all" ? S.list : S.list.filter((x) => bucketOf(x) === S.bay);
}

function miniGauge(x) {
  const m = primaryOf(x);
  if (!m) return "";
  const imp = improvement(m.delta, m.higher_is_better);
  if (!imp) return "";
  const nf = finite(x.eval?.noise_floor?.value) ? Math.abs(x.eval.noise_floor.value) : null;
  const d = digitsFor(m);
  const domain = centred([...(imp.ci || [imp.value]), nf ?? 0]);
  return html`<div class="xp-gauge">
    <span class="xp-gauge-name mono">${m.name || "primary"}</span>
    ${ciRow({ domain, value: imp.value, ci: imp.ci, refs: 0, band: nf != null ? [-nf, nf] : null, size: "mini" })}
    <span class="xp-gauge-v num">${sgn(imp.value, d)}</span>
  </div>`;
}

function strip(x, selected, i = 10) {
  const s = statusOf(x);
  const m = primaryOf(x);
  const held = s === "pending" || s === "superseded";
  return html`<div class="strip xp-strip" role="option" id="xp-opt-${x.id}" data-exp="${x.id}" aria-selected="${selected ? "true" : "false"}" tabindex="${selected ? 0 : -1}" style="--i:${Math.min(i, 10)}"
      ${held ? html`data-held` : ""} ${s === "rejected" ? html`data-status="killed"` : ""}>
    <div class="strip-top">
      <span class="label">${x.stage || "stage?"}</span>
      <span class="where mono">${x.id}</span>
      ${stamp(x)}
    </div>
    <div class="strip-text">${changeLine(x)}</div>
    ${m ? miniGauge(x) : html`<p class="xp-strip-why">${x.decision?.reason || "No evaluation recorded."}</p>`}
    <div class="strip-foot">
      <span>${x.proposer?.kind || "proposer?"}</span>
      ${S.revertedBy.has(x.id) ? html`<span>reverted</span>` : ""}
      <span>${clock(x.decision?.at || x.created_at, true)}</span>
      ${itemsLine(x.eval) ? html`<span>${itemsLine(x.eval)}</span>` : ""}
    </div>
  </div>`;
}

function runLabel(runId, n) {
  if (!runId) return html`<span class="label">Outside a run</span><span class="xp-run-meta">reverts and hand-made records</span>`;
  const r = S.data.runs?.[runId];
  const when = r?.started_at ? clock(r.started_at, true) : runId;
  const budget = r && finite(r.llm_calls) ? `${compact(r.llm_calls)}${finite(r.max_calls) ? ` of ${compact(r.max_calls)}` : ""} calls` : "";
  const fam = finite(r?.bh?.family_size) ? `family of ${r.bh.family_size}` : `${n} shown`;
  return html`<span class="label" title="${runId}">Run</span><span class="xp-run-when">${when}</span>
    <span class="xp-run-meta">${[fam, budget].filter(Boolean).join(" · ")}</span>
    ${r?.note ? html`<span class="xp-run-note">${r.note}</span>` : ""}`;
}

function listMarkup() {
  const items = visible();
  if (!items.length) return html`<p class="xp-empty">${EMPTY_BAY[S.bay] || "Nothing here."}</p>`;
  const groups = [];
  let k = 0;
  for (const x of items) {
    const g = groups.at(-1);
    if (g && g.run === (x.run_id || null)) g.items.push(x);
    else groups.push({ run: x.run_id || null, items: [x] });
  }
  return html`${groups.map((g, i) => html`<div class="xp-group" role="group" aria-labelledby="xp-run-${i}">
    <div class="xp-run" id="xp-run-${i}">${runLabel(g.run, g.items.length)}</div>
    ${g.items.map((x) => strip(x, x.id === S.sel, k++))}
  </div>`)}`;
}

// --- Detail -----------------------------------------------------------------------------------

function detailHead(x) {
  const s = statusOf(x);
  const d = x.decision || {};
  const by = d.by === "human" ? "Decided by a person" : d.by === "auto" ? "Decided by the gate" : d.by ? `Decided by ${d.by}` : "";
  const rerunOf = S.list.filter((o) => o.decision?.superseded_by === x.id);
  const revertOf = x.proposer?.kind === "revert" ? x.proposer.source : null;
  return html`<header class="section xp-dhead">
    <div class="xp-dtitle">
      <span class="label">${x.stage || "stage?"}</span>
      <h2 class="xp-dname mono">${x.id}</h2>
      ${stamp(x)}
    </div>
    <p class="xp-gist">${gist(x)}</p>
    ${d.reason ? html`<div class="well xp-reason"><span class="label">Reason</span><p>${d.reason}</p></div>` : ""}
    ${s === "superseded" && (d.original_status || d.original_reason)
      ? html`<p class="xp-was">Originally <b>${d.original_status || "decided"}</b>${d.original_at ? ` at ${clock(d.original_at, true)}` : ""}: ${d.original_reason || "no reason recorded"}</p>`
      : ""}
    <p class="xp-meta">
      ${[by, d.at ? clock(d.at, true) : ""].filter(Boolean).join(" · ")}
      ${d.superseded_by ? html` · replaced by <button class="xp-link mono" type="button" data-goto="${d.superseded_by}">${d.superseded_by}</button>` : ""}
      ${rerunOf.map((o) => html` · reruns <button class="xp-link mono" type="button" data-goto="${o.id}">${o.id}</button>`)}
      ${revertOf ? html` · reverts <button class="xp-link mono" type="button" data-goto="${revertOf}">${revertOf}</button>` : ""}
      ${S.revertedBy.has(x.id) ? html` · reverted by <button class="xp-link mono" type="button" data-goto="${S.revertedBy.get(x.id)}">${S.revertedBy.get(x.id)}</button>` : ""}
    </p>
    ${x.hypothesis ? html`<div class="xp-hyp"><span class="label">Hypothesis</span><p class="prose">${x.hypothesis}</p></div>` : ""}
    <p class="xp-meta">Proposed by ${x.proposer?.kind || "unknown"}${x.proposer?.source ? html` · <span class="xp-src">${x.proposer.source}</span>` : ""}</p>
  </header>`;
}

function diffBlock(diff) {
  if (typeof diff !== "string" || !diff.trim()) return "";
  const lines = diff.split("\n");
  const cls = (l) => (l.startsWith("+++") || l.startsWith("---") ? "hd" : l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "ctx");
  return html`<details class="xp-diff" ${lines.length <= 14 ? html`open` : ""}>
    <summary>Config diff · ${lines.length} lines</summary>
    <pre class="well">${lines.map((l) => html`<span class="${cls(l)}">${l || " "}</span>`)}</pre>
  </details>`;
}

function changeSection(x) {
  const params = paramsOf(x);
  const files = Array.isArray(x.change?.files) ? x.change.files : [];
  const now = S.data.champion?.ref;
  const champRef = x.champion?.ref;
  return html`<section class="section" aria-labelledby="xp-h-change">
    <div class="section-head"><h2 id="xp-h-change">What changed</h2>${x.change?.summary ? html`<span class="aside">${x.change.summary}</span>` : ""}</div>
    <p class="xp-refs">
      <span class="mono" title="${x.champion?.config_hash || ""}">${short(champRef)}</span> <span class="muted">${x.champion?.label || "champion"}</span>
      <span aria-hidden="true">→</span><span class="sr-only">to</span>
      <span class="mono" title="${x.challenger?.config_hash || ""}">${short(x.challenger?.ref)}</span> <span class="muted">${x.challenger?.label || "challenger"}</span>
    </p>
    ${now && champRef && now !== champRef && now !== x.challenger?.ref ? html`<p class="xp-note">Neither config is in force now: this ran against <span class="mono">${champRef}</span>, an earlier champion, and the config in force is <span class="mono">${now}</span>.</p>` : ""}
    ${params.length
      ? html`<div class="xp-tablewrap"><table class="xp-table">
          <thead><tr><th scope="col">Knob</th><th scope="col">From</th><th scope="col">To</th></tr></thead>
          <tbody>${params.map(([k, v]) => html`<tr><th scope="row" class="mono">${k}</th><td class="mono">${fmtVal(v?.from)}</td><td class="mono xp-to">${fmtVal(v?.to)}</td></tr>`)}</tbody>
        </table></div>`
      : html`<p class="xp-note">No knob changes recorded.</p>`}
    ${files.length ? html`<p class="xp-files"><span class="label">Reads</span> ${files.map((f) => html`<code>${f}</code>`)}</p>` : ""}
    ${diffBlock(x.change?.diff)}
  </section>`;
}

function primaryVerdict(imp, nf) {
  if (!imp?.ci) return "No interval recorded, so the effect can't be compared with the noise floor.";
  const [lo, hi] = imp.ci;
  const floor = nf ?? 0;
  if (lo > floor) return html`<b>Clears the noise floor.</b> The interval's lower end, ${sgn(lo)}, is above the floor of ${num(floor)}.`;
  if (hi < 0) return html`<b>Worse.</b> The whole interval is below zero; it ends at ${sgn(hi)}.`;
  if (lo > 0) return html`<b>Above zero, inside the noise floor.</b> The lower end, ${sgn(lo)}, doesn't clear the floor of ${num(floor)}.`;
  return html`<b>No clear change.</b> The interval runs from ${sgn(lo)} to ${sgn(hi)}, across zero.`;
}

function direction(m) {
  return m.higher_is_better === true ? "higher is better" : m.higher_is_better === false ? "lower is better" : "direction not recorded";
}

function primaryBlock(x, m) {
  const d = digitsFor(m);
  const imp = improvement(m.delta, m.higher_is_better);
  const nfRec = x.eval?.noise_floor;
  const nf = finite(nfRec?.value) ? Math.abs(nfRec.value) : null;
  const rep = improvement(nfRec?.delta, m.higher_is_better);
  const domain = centred([...(imp?.ci || [imp?.value]), ...(rep?.ci || [rep?.value]), nf ?? 0], true);
  const grid = ticks(domain, 6);
  const band = nf != null ? [-nf, nf] : null;
  const row = (label, sub, stat, tone, aria) => html`<div class="xp-frow">
      <span class="xp-flabel">${label}${sub ? html`<small>${sub}</small>` : ""}</span>
      ${ciRow({ domain, value: stat?.value ?? null, ci: stat?.ci, refs: 0, band, grid, tone, label: aria })}
      <span class="xp-fval num">${stat ? sgn(stat.value, d) : "–"} <small>${interval(stat?.ci, d, sgn)}</small></span>
    </div>`;
  return html`<div class="xp-primary">
    <div class="xp-mhead">
      <h3 class="mono">${m.name || "primary metric"}</h3>
      <span class="chip">Primary</span>
      <span class="xp-dir">${direction(m)}</span>
    </div>
    <dl class="xp-arms">
      <div><dt class="label">Champion</dt><dd class="num">${num(m.champion?.value, d)} <small>${interval(m.champion?.ci95, d)}</small></dd></div>
      <div><dt class="label">Challenger</dt><dd class="num">${num(m.challenger?.value, d)} <small>${interval(m.challenger?.ci95, d)}</small></dd></div>
    </dl>
    <div class="xp-forest">
      ${row("Challenger against champion", "improvement", imp, "ink",
        imp ? `Improvement ${sgn(imp.value, d)}${imp.ci ? `, 95% interval ${sgn(imp.ci[0], d)} to ${sgn(imp.ci[1], d)}` : ""}${nf != null ? `; noise floor ${num(nf, d)}` : ""}` : "No improvement recorded")}
      ${rep ? row("Champion against a rerun of itself", "the noise floor", rep, "muted", `Champion rerun moved ${sgn(rep.value, d)}${rep.ci ? `, 95% interval ${sgn(rep.ci[0], d)} to ${sgn(rep.ci[1], d)}` : ""}`) : ""}
      <div class="xp-frow xp-faxis"><span></span>${ciAxis({ domain, ticks: grid, format: (v) => sgn(v, tickDigits(grid)), low: "worse", high: "better", max: 5 })}<span></span></div>
    </div>
    <p class="xp-verdict">${primaryVerdict(imp, nf)}</p>
    <p class="xp-legend">${nf != null ? html`Shaded: the noise floor, ±${num(nf, d)}, how far the champion moved against a rerun of itself with new lineups or seeds. ` : html`No noise floor recorded for this experiment. `}Filled point: the interval clears ${nf != null ? "the floor" : "zero"}; hollow: it doesn't.</p>
  </div>`;
}

function secondaryRows(x, ms) {
  if (!ms.length) return "";
  return html`<div class="xp-tablewrap"><table class="xp-table xp-mtable">
    <caption class="sr-only">Other metrics, champion against challenger</caption>
    <thead><tr><th scope="col">Metric</th><th scope="col" class="r">Champion</th><th scope="col" class="r">Challenger</th><th scope="col" class="xp-plotcol"><span class="sr-only">Improvement plot</span></th><th scope="col" class="r">Improvement</th></tr></thead>
    <tbody>${ms.map((m) => {
      const d = digitsFor(m);
      const imp = improvement(m.delta, m.higher_is_better);
      const margin = m.guardrail && finite(m.margin) ? Math.abs(m.margin) : null;
      const domain = centred([...(imp?.ci || [imp?.value]), margin != null ? margin : 0]);
      const nonInferior = margin != null && imp?.ci ? imp.ci[0] > -margin : null;
      return html`<tr>
        <th scope="row"><span class="mono xp-mname">${m.name || "metric"}</span>
          <span class="xp-msub">${m.guardrail ? html`<span class="chip">Guardrail</span> ` : ""}${direction(m)}${margin != null ? `; may fall at most ${num(margin, d)}` : ""}</span>
          ${margin != null ? html`<span class="xp-msub ${nonInferior ? "" : "xp-fail"}">${nonInferior == null ? "No interval, so non-inferiority is unknown" : nonInferior ? "Shown non-inferior" : `Not shown non-inferior: lower end ${sgn(imp.ci[0], d)} is past −${num(margin, d)}`}</span>` : ""}
        </th>
        <td class="r num">${num(m.champion?.value, d)}<small>${interval(m.champion?.ci95, d)}</small></td>
        <td class="r num">${num(m.challenger?.value, d)}<small>${interval(m.challenger?.ci95, d)}</small></td>
        <td class="xp-plotcol">${ciRow({
          domain, value: imp?.value ?? null, ci: imp?.ci, size: "mini",
          refs: margin != null ? [0, { at: -margin, kind: "threshold" }] : 0,
          filled: margin != null ? !!nonInferior : undefined,
          label: imp ? `${m.name} improvement ${sgn(imp.value, d)}${imp.ci ? `, 95% interval ${sgn(imp.ci[0], d)} to ${sgn(imp.ci[1], d)}` : ""}${margin != null ? `; margin −${num(margin, d)}` : ""}` : "",
        })}</td>
        <td class="r num">${imp ? sgn(imp.value, d) : "–"}<small>${interval(imp?.ci, d, sgn)}</small></td>
      </tr>`;
    })}</tbody>
  </table></div>
  <p class="xp-legend">Each row has its own scale with zero in the middle; right is better. A dashed line marks a guardrail's margin.</p>`;
}

function metricsSection(x) {
  const ms = metricsOf(x);
  const ev = x.eval || {};
  const s = statusOf(x);
  const aside = [itemsLine(ev), finite(ev.seed) ? `seed ${ev.seed}` : ""].filter(Boolean).join(" · ");
  let body;
  if (!ms.length) {
    const why = isRevert(x)
      ? "A revert runs no comparison of its own; the reason above says why it was made."
      : s === "pending" ? "Not evaluated. The reason above says whether the budget skipped it or an arm failed." : "No metrics recorded for this experiment.";
    body = html`<p class="xp-note">${why}</p>`;
  } else {
    const p = primaryOf(x);
    body = html`${primaryBlock(x, p)}${secondaryRows(x, ms.filter((m) => m !== p))}`;
  }
  return html`<section class="section" aria-labelledby="xp-h-metrics">
    <div class="section-head"><h2 id="xp-h-metrics">Champion against challenger</h2>${aside ? html`<span class="aside">${aside}</span>` : ""}</div>
    ${body}
  </section>`;
}

function correctionSection(x) {
  const c = x.eval?.correction;
  const run = x.run_id ? S.data.runs?.[x.run_id] : null;
  const family = x.run_id ? S.list.filter((o) => o.run_id === x.run_id) : [];
  if (!c || typeof c !== "object") {
    if (family.length < 2) return "";
    return html`<section class="section" aria-labelledby="xp-h-corr"><div class="section-head"><h2 id="xp-h-corr">Multiple-comparison correction</h2></div>
      <p class="xp-note">No correction recorded for this experiment.</p>${familyList(x, family)}</section>`;
  }
  const method = c.method === "benjamini-hochberg" ? "Benjamini–Hochberg" : human(c.method || "unknown method");
  const sig = c.rejected === true ? "Significant after correction." : c.rejected === false ? "Not significant after correction." : "";
  return html`<section class="section" aria-labelledby="xp-h-corr">
    <div class="section-head"><h2 id="xp-h-corr">Multiple-comparison correction</h2>${run?.note ? html`<span class="aside">${run.note}</span>` : ""}</div>
    <div class="fields">
      <div class="field"><span class="label">Method</span><span class="v ui">${method}</span></div>
      <div class="field"><span class="label">q</span><span class="v">${finite(c.q) ? c.q : "–"}</span></div>
      <div class="field"><span class="label">Family</span><span class="v">${finite(c.family_size) ? c.family_size : "–"}</span></div>
      <div class="field"><span class="label">p</span><span class="v">${pval(c.p)}</span></div>
      <div class="field"><span class="label">Adjusted p</span><span class="v">${pval(c.p_adjusted)}</span></div>
      <div class="field"><span class="label">Significant</span><span class="v">${c.rejected === true ? "Yes" : c.rejected === false ? "No" : "–"}</span></div>
      <div class="field"><span class="label">Gate bound</span><span class="v">${finite(c.interval_level) ? `${+(c.interval_level * 100).toFixed(2)}%` : "–"}</span></div>
    </div>
    <p class="xp-note">${sig} ${method} at q = ${finite(c.q) ? c.q : "?"} across ${finite(c.family_size) ? c.family_size : "?"} comparison${c.family_size === 1 ? "" : "s"} in this run: p ${pval(c.p)}, adjusted ${pval(c.p_adjusted)}.${finite(c.interval_level) ? ` The gate compared a ${+(c.interval_level * 100).toFixed(2)}% one-sided lower bound with the noise floor.` : ""}</p>
    ${familyList(x, family)}
  </section>`;
}

function familyList(x, family) {
  if (family.length < 2) return "";
  return html`<div class="xp-family">
    <p class="xp-famhead"><span class="label">Same family</span> ${family.length} experiments in <span class="mono">${x.run_id}</span></p>
    <ul>${family.map((o) => {
      const m = primaryOf(o);
      const imp = m ? improvement(m.delta, m.higher_is_better) : null;
      const adj = o.eval?.correction?.p_adjusted;
      return html`<li ${o.id === x.id ? html`aria-current="true"` : ""}>
        ${o.id === x.id ? html`<span class="mono xp-self">${o.id}</span>` : html`<button class="xp-link mono" type="button" data-goto="${o.id}">${o.id}</button>`}
        <span class="xp-fam-change">${changeLine(o)}</span>
        <span class="num xp-fam-num">${imp ? sgn(imp.value, m ? digitsFor(m) : 3) : "–"}${finite(adj) ? ` · adj p ${pval(adj)}` : ""}</span>
        ${stamp(o)}
      </li>`;
    })}</ul>
  </div>`;
}

function costSection(x) {
  const c = x.cost;
  if (!c || typeof c !== "object") {
    return html`<section class="section" aria-labelledby="xp-h-cost"><div class="section-head"><h2 id="xp-h-cost">Cost</h2></div><p class="xp-note">No cost recorded.</p></section>`;
  }
  const arms = Object.entries(c.by_arm && typeof c.by_arm === "object" ? c.by_arm : {});
  const usd = c.usd === null || c.usd === undefined ? html`<span class="v ui">No trusted pricing</span>` : html`<span class="v">$${finite(c.usd) ? c.usd.toFixed(2) : c.usd}</span>`;
  return html`<section class="section" aria-labelledby="xp-h-cost">
    <div class="section-head"><h2 id="xp-h-cost">Cost</h2><span class="aside">The challenger's own arm; champion arms are shared by every challenger in the run</span></div>
    <div class="fields">
      <div class="field"><span class="label">LLM calls</span><span class="v">${compact(c.llm_calls)}</span></div>
      ${finite(c.shared_llm_calls) ? html`<div class="field"><span class="label">Shared calls</span><span class="v">${compact(c.shared_llm_calls)}</span></div>` : ""}
      <div class="field"><span class="label">Tokens in</span><span class="v">${compact(c.input_tokens)}</span></div>
      <div class="field"><span class="label">Tokens out</span><span class="v">${compact(c.output_tokens)}</span></div>
      <div class="field"><span class="label">Time</span><span class="v">${finite(c.seconds) ? duration(c.seconds * 1000) : "–"}</span></div>
      <div class="field"><span class="label">Price</span>${usd}</div>
    </div>
    ${arms.length
      ? html`<div class="xp-tablewrap"><table class="xp-table xp-arms-table">
          <thead><tr><th scope="col">Arm</th><th scope="col" class="r">Calls</th><th scope="col" class="r">Tokens in</th><th scope="col" class="r">Tokens out</th><th scope="col" class="r">Errors</th><th scope="col" class="r">Time</th></tr></thead>
          <tbody>${arms.map(([k, a]) => html`<tr>
            <th scope="row">${ARM[k] || "Challenger"}${!ARM[k] ? html` <span class="mono muted">${k}</span>` : ""}${a?.shared ? html` <span class="muted">shared</span>` : ""}</th>
            <td class="r num">${compact(a?.llm_calls)}</td><td class="r num">${compact(a?.input_tokens)}</td><td class="r num">${compact(a?.output_tokens)}</td>
            <td class="r num">${finite(a?.errors) && a.errors > 0 ? html`<span class="chip flag">${a.errors}</span>` : compact(a?.errors ?? null)}</td>
            <td class="r num">${finite(a?.seconds) ? duration(a.seconds * 1000) : "–"}</td>
          </tr>`)}</tbody>
        </table></div>`
      : ""}
  </section>`;
}

function evalSection(x) {
  const ev = x.eval;
  if (!ev || typeof ev !== "object" || (!ev.dataset && !ev.fitness && !ev.report)) return "";
  const inst = ev.instrument && typeof ev.instrument === "object" ? ev.instrument : {};
  const scalars = Object.entries(inst).filter(([k, v]) => k !== "dataset" && k !== "dataset_sha256" && (typeof v !== "string" || v.length <= 90) && (v === null || typeof v !== "object"));
  const longs = Object.entries(inst).filter(([, v]) => typeof v === "string" && v.length > 90);
  const objs = Object.entries(inst).filter(([, v]) => v && typeof v === "object");
  return html`<section class="section" aria-labelledby="xp-h-eval">
    <div class="section-head"><h2 id="xp-h-eval">Evaluation set</h2></div>
    <dl class="kv">
      ${ev.fitness ? html`<dt>Fitness</dt><dd class="mono">${ev.fitness}</dd>` : ""}
      ${ev.dataset ? html`<dt>Dataset</dt><dd><span class="mono">${ev.dataset}</span>${ev.dataset_sha256 ? html` <span class="muted mono" title="${ev.dataset_sha256}">sha ${String(ev.dataset_sha256).slice(0, 12)}</span>` : ""}</dd>` : ""}
      ${itemsLine(ev) ? html`<dt>Items</dt><dd>${itemsLine(ev)}, resampled by ${ev.cluster_unit || "item"}</dd>` : ""}
      ${ev.report ? html`<dt>Per-item report</dt><dd class="mono">${ev.report}</dd>` : ""}
      ${x.file ? html`<dt>Record</dt><dd class="mono">${x.file}</dd>` : ""}
      ${scalars.map(([k, v]) => html`<dt>${human(k)}</dt><dd class="mono">${fmtVal(v)}</dd>`)}
    </dl>
    ${longs.length || objs.length
      ? html`<details class="xp-inst"><summary>Instrument details</summary>
          ${longs.map(([k, v]) => html`<div class="xp-prompt"><span class="label">${human(k)}</span><p class="well">${v}</p></div>`)}
          ${objs.map(([k, v]) => html`<div class="xp-prompt"><span class="label">${human(k)}</span><pre class="well">${JSON.stringify(v, null, 1)}</pre></div>`)}
        </details>`
      : ""}
  </section>`;
}

function detailMarkup() {
  const x = S.byId.get(S.sel);
  if (!x || !visible().includes(x)) {
    if (visible().length) return html`<div class="state"><p>Pick an experiment from the ledger.</p></div>`;
    return html`<div class="state"><span class="label">${BAYS.find(([k]) => k === S.bay)?.[1] || ""} · 0</span>
      <p>Nothing in this bay to show. Every experiment is under All.</p>
      <button class="btn btn-quiet btn-small" type="button" data-bay="all">Show all ${S.list.length}</button></div>`;
  }
  return html`${detailHead(x)}${changeSection(x)}${metricsSection(x)}${correctionSection(x)}${costSection(x)}${evalSection(x)}`;
}

// --- Render and interaction -------------------------------------------------------------------

function problemsNotice() {
  const p = Array.isArray(S.data.problems) ? S.data.problems : [];
  if (!p.length) return "";
  return html`<div class="notice warn xp-problems" role="status">
    <b>${p.length} file${p.length === 1 ? "" : "s"} in eval/experiments/ couldn't be read.</b>
    <ul>${p.slice(0, 5).map((e) => html`<li><span class="mono">${e.file}</span>: ${e.error}</li>`)}${p.length > 5 ? html`<li>and ${p.length - 5} more</li>` : ""}</ul>
  </div>`;
}

function render() {
  const n = S.list.length;
  const sub = n ? tally() : "The self-iteration ledger: challenger configs tested against the champion, and what the gate decided.";
  setHTML(
    S.el,
    html`${head(sub)}
    <div class="xp">
      ${n || S.data.champion ? championBlock() : ""}
      ${problemsNotice()}
      ${n
        ? html`<div class="xp-body">
            <div class="xp-side">
              ${bays()}
              <div class="xp-list ${S.first ? "entering" : ""}" id="xp-list" role="listbox" aria-label="Experiments, newest first">${listMarkup()}</div>
            </div>
            <article class="panel xp-detail" id="xp-detail" aria-label="Selected experiment">${detailMarkup()}</article>
          </div>`
        : html`<div class="state">
            <span class="label">Empty ledger</span>
            <h2>No experiments yet</h2>
            <p>eval/experiments/ exists but holds no records. <code>python -m harness iterate run --provider anthropic</code> runs a batch and writes one record per experiment; mock runs go to <code>out/iterate/</code> instead.</p>
          </div>`}
    </div>`,
  );
  if (S.first) {
    S.first = false;
    setTimeout(() => $(".xp-list.entering", S.el)?.classList.remove("entering"), 700);
  }
}

function renderList() {
  const list = $("#xp-list", S.el);
  if (!list) return;
  setHTML(list, listMarkup());
  const bayEl = $(".xp-bays", S.el);
  if (bayEl) bayEl.outerHTML = String(bays());
}

function renderDetail() {
  const d = $("#xp-detail", S.el);
  if (d) setHTML(d, detailMarkup());
}

function select(id, { focus = false, scroll = true } = {}) {
  if (!S.byId.has(id)) return;
  S.sel = id;
  S.ctx.setParams({ exp: id });
  for (const el of $$("[data-exp]", S.el)) {
    const on = el.dataset.exp === id;
    el.setAttribute("aria-selected", on ? "true" : "false");
    el.tabIndex = on ? 0 : -1;
  }
  const opt = $(`[data-exp="${CSS.escape(id)}"]`, S.el);
  if (opt) {
    if (scroll) opt.scrollIntoView({ block: "nearest" });
    if (focus) opt.focus({ preventScroll: true });
  }
  renderDetail();
}

function setBay(bay) {
  const hadFocus = !!document.activeElement?.matches?.("[data-bay]");
  S.bay = bay;
  const vis = visible();
  if (!vis.some((x) => x.id === S.sel) && vis.length) S.sel = vis[0].id;
  S.ctx.setParams({ bay: bay === "all" ? null : bay, exp: S.sel });
  renderList();
  renderDetail();
  if (hadFocus) $(`[data-bay="${bay}"]`, S.el)?.focus();
}

/** Go to an experiment from a link in the detail or the champion block, widening the filter if it's hidden. */
function goTo(id) {
  if (!S.byId.has(id)) return S.ctx.toast(`${id} isn't in this ledger.`);
  if (!visible().some((x) => x.id === id)) {
    S.bay = "all";
    S.ctx.setParams({ bay: null });
    renderList();
  }
  select(id);
  $("#xp-detail", S.el)?.scrollIntoView({ block: "nearest" });
}

function move(step) {
  const vis = visible();
  if (!vis.length) return false;
  const i = vis.findIndex((x) => x.id === S.sel);
  const next = vis[Math.min(vis.length - 1, Math.max(0, (i < 0 ? -step : i) + step))];
  if (next && next.id !== S.sel) select(next.id, { focus: S.el.contains(document.activeElement) && !!document.activeElement.closest?.("#xp-list") });
  return true;
}

registerView({
  id: "experiments",
  title: "Experiments",
  group: "learn",
  css: "/static/css/views/experiments.css",
  keys: [["j / k", "Next or previous experiment"]],
  async mount(el, ctx, params) {
    const token = {};
    S = { el, ctx, token, data: null, list: [], byId: new Map(), revertedBy: new Map(), bay: "all", sel: null, first: true, off: [] };
    setHTML(el, skeleton());
    const data = await ctx.api.get("/api/experiments");
    if (S?.token !== token) return;
    if (!data || data.available === false) {
      setHTML(el, unavailable(data || {}));
      return;
    }
    S.data = data;
    S.list = (Array.isArray(data.experiments) ? data.experiments : []).filter((x) => x && typeof x === "object" && x.id);
    S.byId = new Map(S.list.map((x) => [x.id, x]));
    for (const x of S.list) if (isRevert(x) && statusOf(x) === "promoted" && x.proposer?.source) S.revertedBy.set(x.proposer.source, x.id);
    S.bay = BAYS.some(([k]) => k === params.bay) ? params.bay : "all";
    const vis = visible();
    S.sel = vis.some((x) => x.id === params.exp) ? params.exp : vis[0]?.id ?? null;
    render();
    if (params.exp && S.sel === params.exp) $(`[data-exp="${CSS.escape(S.sel)}"]`, el)?.scrollIntoView({ block: "nearest" });
    S.off.push(
      on(el, "click", "[data-exp]", (e, opt) => select(opt.dataset.exp, { scroll: false })),
      on(el, "click", "[data-bay]", (e, b) => setBay(b.dataset.bay)),
      on(el, "click", "[data-goto]", (e, b) => goTo(b.dataset.goto)),
      on(el, "keydown", "[role=tab]", (e, b) => {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        const tabs = $$("[role=tab]", b.parentElement);
        const next = tabs[(tabs.indexOf(b) + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        e.preventDefault();
        setBay(next.dataset.bay);
      }),
    );
  },
  unmount() {
    for (const off of S?.off || []) off();
    S = null;
  },
  onKey(e) {
    if (!S?.list.length || isTyping(e.target) || e.ctrlKey) return false;
    const inList = !!e.target.closest?.("#xp-list");
    if (e.key === "j" || (inList && e.key === "ArrowDown")) return move(1);
    if (e.key === "k" || (inList && e.key === "ArrowUp")) return move(-1);
    return false;
  },
});
