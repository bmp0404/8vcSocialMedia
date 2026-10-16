// Eval reports: backtests and fault verdicts from sam/eval (eval/reports/).
//
// A stand-in until Will's eval views land; the API reader is evals_api.py. A backtest asks how well
// each predictor tells a viral post from a Deadweight post on the same account (an AUC, 0.5 is
// chance). The headline and its controls are within-account; the cross-account measures are
// confounded by account size and sit in their own panel so nobody reads them as accuracy. Fault
// verdicts say which stage to fix when a post underperformed.

import { registerView } from "../shell.js";
import { $, html, on, setHTML } from "../lib/dom.js";
import { clock, compact, duration, handleFromUrl, human, signed } from "../lib/format.js";
import { ciAxis, ciRow, domainOf, ticks } from "../components/ci.js";

const HEADLINE = "headline_viral_vs_deadweight_auc";
const CONTROLS = [
  ["same_media_status", "Same media status", "both posts have media, or neither does"],
  ["same_launch_type", "Same launch type", "both posts announce the same kind of thing"],
  ["viral_vs_viral_by_engagement", "Viral vs viral", "ranks two viral posts by engagement"],
  ["deadweight_vs_deadweight_noise_check", "Deadweight vs Deadweight", "noise check: should sit near 0.5"],
];
const SEGMENTS = [
  ["all", "All posts"],
  ["without_boost_suspect", "Without boost-suspect posts"],
];
const GROUPS = [
  ["base", "Baselines"],
  ["model", "Model predictors"],
  ["avg", "Averages across models"],
];
// Order and words follow harness/evals/fault.py on sam/eval: the first four name a stage to fix.
const VERDICT = {
  action_fault: { word: "Action fault", stage: true, gist: "story alive, draft bad" },
  observe_fault: { word: "Observe fault", stage: true, gist: "story dead, draft good" },
  both: { word: "Both", stage: true, gist: "story dead and draft bad; fix the input first" },
  unexplained: { word: "Unexplained", stage: true, gist: "story alive, draft good, still flopped" },
  no_fault: { word: "No fault", gist: "above the account's usual band" },
  variance: { word: "Variance", gist: "inside the account's usual band" },
  uncertain: { word: "Uncertain", gist: "story or draft evidence borderline" },
  insufficient_evidence: { word: "Insufficient evidence", gist: "a field the verdict needs is missing" },
};
const FAULT_COMMAND = "python -m harness eval fault --stories --scores eval/reports/<backtest>.json:panel@mean --out eval/reports/fault-<date>.json";

let S = null;

const finite = (v) => typeof v === "number" && Number.isFinite(v);
const num = (v, d = 3) => (finite(v) ? v.toFixed(d).replace("-", "−") : "–");
const ci2 = (ci, d = 2, f = num) => (Array.isArray(ci) && finite(ci[0]) && finite(ci[1]) ? `[${f(ci[0], d)}, ${f(ci[1], d)}]` : "");
const sgn = (v, d = 3) => (finite(v) ? signed(Math.abs(v) < 0.5 * 10 ** -d ? 0 : v, d) : "–");
const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
const groupOf = (name, res) => (res?.derived || /@mean$/.test(name) ? "avg" : name.includes("@") ? "model" : "base");

function stat(m) {
  if (!isObj(m) || !finite(m.value)) return null;
  const ci = Array.isArray(m.ci95) && finite(m.ci95[0]) && finite(m.ci95[1]) ? m.ci95 : null;
  return { value: m.value, ci };
}

function predictors() {
  return Object.entries(isObj(S.report?.results) ? S.report.results : {}).filter(([, r]) => isObj(r));
}

/** The predictor's numbers for the chosen segment, or a string saying why the segment is missing. */
function seg(res) {
  return res?.[S.seg];
}

// --- Skeleton and page head -------------------------------------------------------------------

function skeletonRows(n) {
  return html`${Array.from({ length: n }, (_, i) => html`<div class="ev-skel-row"><span class="skeleton" style="width:${60 + ((i * 37) % 30)}%"></span><span class="skeleton"></span><span class="skeleton" style="width:70%"></span></div>`)}`;
}

function skeleton() {
  return html`<header class="viewhead"><div><h1>Eval reports</h1><p class="sub">Loading reports…</p></div></header>
    <div class="ev" aria-busy="true">
      <div class="panel"><div class="section"><span class="skeleton" style="width:40%"></span></div><div class="section">${skeletonRows(6)}</div></div>
    </div>`;
}

function reportPicker() {
  const reports = S.list?.reports || [];
  if (reports.length < 2) return "";
  return html`<label class="ev-pick"><span class="label">Report</span>
    <select class="select" data-pick="report">
      ${reports.map((r) => html`<option value="${r.file}" ${r.file === S.reportFile ? html`selected` : ""}>${r.at ? clock(r.at, true) : r.file} · ${(r.models || []).join(", ") || r.provider || "unknown models"}</option>`)}
    </select></label>`;
}

function head() {
  return html`<header class="viewhead">
    <div><h1>Eval reports</h1><p class="sub">Backtests from sam/eval: how well each predictor tells a viral post from a Deadweight post on the same account, and which stage to fix when a post underperforms.</p></div>
    <div class="actions">${reportPicker()}</div>
  </header>`;
}

function problems(list, where) {
  const p = Array.isArray(list) ? list : [];
  if (!p.length) return "";
  return html`<div class="notice warn ev-problems" role="status"><b>${p.length} file${p.length === 1 ? "" : "s"} in ${where} couldn't be read.</b>
    <ul>${p.slice(0, 5).map((e) => html`<li><span class="mono">${e.file}</span>: ${e.error}</li>`)}${p.length > 5 ? html`<li>and ${p.length - 5} more</li>` : ""}</ul></div>`;
}

// --- Report -----------------------------------------------------------------------------------

function reportFields(r) {
  const preds = predictors();
  const any = preds.map(([, res]) => seg(res)).find(isObj) || preds.map(([, res]) => res.all).find(isObj) || {};
  const pairs = any[HEADLINE]?.n_pairs;
  const models = Array.isArray(r.models) ? r.models : r.model ? [r.model] : [];
  const cats = isObj(r.sampled_categories) ? Object.entries(r.sampled_categories) : [];
  return html`<div class="fields ev-fields">
    <div class="field"><span class="label">Run</span><span class="v">${r.at ? clock(r.at, true) : "–"}</span></div>
    <div class="field"><span class="label">Provider</span><span class="v">${r.provider || "–"}</span></div>
    <div class="field"><span class="label">Judge models</span><span class="v ui ev-wrap">${models.join(", ") || "–"}</span></div>
    <div class="field"><span class="label">Sample</span><span class="v">${finite(any.n_posts) ? `${any.n_posts} posts` : finite(r.n_sampled) ? `${r.n_sampled} posts` : "–"}${finite(any.n_accounts) ? ` · ${any.n_accounts} accounts` : ""}</span></div>
    <div class="field"><span class="label">Headline pairs</span><span class="v">${finite(pairs) ? pairs : "–"}</span></div>
    <div class="field"><span class="label">Judge memory</span><span class="v">${r.memory || "–"}</span></div>
    ${cats.length ? html`<div class="field"><span class="label">Tiers</span><span class="v ui">${cats.map(([k, v]) => `${k} ${v}`).join(" · ")}</span></div>` : ""}
  </div>`;
}

function segmentBays() {
  const preds = predictors();
  const objs = preds.filter(([, res]) => isObj(res.without_boost_suspect));
  const reason = preds.map(([, res]) => res.without_boost_suspect).find((v) => typeof v === "string");
  const n = (k) => preds.map(([, res]) => res[k]).find(isObj)?.n_posts;
  return html`<div class="ev-segs">
    <div class="bays ev-bays" role="tablist" aria-label="Posts in the sample">
      ${SEGMENTS.map(([k, label]) => {
        const off = k !== "all" && !objs.length;
        return html`<button class="bay" type="button" role="tab" data-seg="${k}" aria-selected="${S.seg === k ? "true" : "false"}" tabindex="${S.seg === k ? 0 : -1}" ${off ? html`disabled aria-disabled="true"` : ""}>
          <span>${label}</span><span class="n">${finite(n(k)) ? n(k) : "–"}</span></button>`;
      })}
    </div>
    ${!objs.length && reason ? html`<p class="ev-segnote">Boost-suspect split unavailable: ${reason}</p>` : ""}
  </div>`;
}

function aucText(s, d = 3) {
  return s ? html`${num(s.value, d)}<small>${ci2(s.ci)}</small>` : "–";
}

function headlineSection() {
  const preds = predictors();
  const rows = preds.map(([name, res]) => ({ name, res, s: stat(seg(res)?.[HEADLINE]), pairs: seg(res)?.[HEADLINE]?.n_pairs }));
  const domain = domainOf(rows.flatMap((r) => (r.s ? [r.s.value, ...(r.s.ci || [])] : [])), { include: [0.5], clamp: [0, 1], count: 6 });
  const grid = ticks(domain, 6);
  const plot = (r) =>
    ciRow({ domain, value: r.s?.value ?? null, ci: r.s?.ci, refs: 0.5, grid, label: r.s ? `${r.name}: AUC ${num(r.s.value)}${r.s.ci ? `, 95% interval ${num(r.s.ci[0])} to ${num(r.s.ci[1])}` : ", no interval"}` : "" });
  return html`<section class="section" aria-labelledby="ev-h-head">
    <div class="section-head"><h2 id="ev-h-head">Headline · viral vs Deadweight, same account</h2><span class="aside">AUC with a 95% interval from resampling accounts</span></div>
    <div class="ev-tablewrap"><table class="ev-table ev-forest">
      <caption class="sr-only">Headline AUC by predictor; 0.5 is chance</caption>
      <thead><tr><th scope="col">Predictor</th><th scope="col" class="ev-plot"><span class="sr-only">Interval plot</span></th><th scope="col" class="r">AUC</th><th scope="col" class="r ev-pairs">Pairs</th></tr></thead>
      ${GROUPS.map(([g, label]) => {
        const rs = rows.filter((r) => groupOf(r.name, r.res) === g);
        if (!rs.length) return "";
        return html`<tbody>
          <tr class="grp"><th scope="colgroup" colspan="4">${label}</th></tr>
          ${rs.map((r) => html`<tr>
            <th scope="row"><span class="mono ev-pname">${r.name}</span></th>
            ${r.s
              ? html`<td class="ev-plot">${plot(r)}</td><td class="r num">${aucText(r.s)}</td>`
              : html`<td class="ev-plot ev-noresult" colspan="2">${failureText(r.res)}</td>`}
            <td class="r num ev-pairs">${finite(r.pairs) ? r.pairs : "–"}</td>
          </tr>`)}
        </tbody>`;
      })}
      <tfoot><tr><td></td><td class="ev-plot">${ciAxis({ domain, ticks: grid, format: (v) => v.toFixed(1), max: 7 })}</td><td></td><td class="ev-pairs"></td></tr></tfoot>
    </table></div>
    <p class="ev-legend">The line at 0.5 is chance. Filled point: the interval clears chance; hollow: it includes it.</p>
  </section>`;
}

function failureText(res) {
  const f = Array.isArray(res?.failures) ? res.failures : [];
  if (typeof seg(res) === "string") return seg(res);
  return f.length ? html`No result. <span class="ev-fail">${String(f[0]).slice(0, 240)}${String(f[0]).length > 240 ? "…" : ""}</span>` : "No result for this split.";
}

function controlsSection() {
  const preds = predictors().filter(([, res]) => isObj(seg(res)));
  if (!preds.length) return "";
  const vals = preds.flatMap(([, res]) => CONTROLS.flatMap(([k]) => {
    const s = stat(seg(res)[k]);
    return s ? [s.value, ...(s.ci || [])] : [];
  }));
  const domain = domainOf(vals, { include: [0.5], clamp: [0, 1], count: 4 });
  return html`<section class="section" aria-labelledby="ev-h-ctl">
    <div class="section-head"><h2 id="ev-h-ctl">Controls and splits</h2><span class="aside">The same AUC on harder or sanity-check pairs; every plot shares one scale with 0.5 marked</span></div>
    <div class="ev-tablewrap"><table class="ev-table ev-controls">
      <caption class="sr-only">AUC on control pairs by predictor</caption>
      <thead><tr><th scope="col">Predictor</th>
        ${CONTROLS.map(([, label, note]) => html`<th scope="col"><span>${label}</span><small>${note}</small></th>`)}
        <th scope="col" class="r"><span>Top pick is viral</span><small>share, against chance</small></th></tr></thead>
      <tbody>${preds.map(([name, res]) => {
        const d = seg(res);
        const top = d.top_pick_is_viral;
        return html`<tr><th scope="row"><span class="mono ev-pname">${name}</span></th>
          ${CONTROLS.map(([k, label]) => {
            const s = stat(d[k]);
            return html`<td><div class="ev-cell">
              <span class="num">${s ? num(s.value) : "–"}<small>${s ? ci2(s.ci) : ""}</small></span>
              ${s ? ciRow({ domain, value: s.value, ci: s.ci, refs: 0.5, size: "mini", label: `${name}, ${label}: ${num(s.value)}${s.ci ? `, 95% interval ${num(s.ci[0])} to ${num(s.ci[1])}` : ""}` }) : ""}
            </div></td>`;
          })}
          <td class="r num">${isObj(top) && finite(top.value) ? html`${num(top.value)}<small>chance ${num(top.chance)}</small>` : "–"}</td>
        </tr>`;
      })}</tbody>
    </table></div>
  </section>`;
}

function comparisonsSection() {
  const comps = Object.entries(isObj(S.report?.comparisons) ? S.report.comparisons : {}).filter(([, c]) => isObj(c));
  if (!comps.length) return "";
  const rows = comps.map(([name, c]) => [name, Object.entries(c).map(([k, v]) => ({ other: k.replace(/^minus_/, ""), s: stat(v) }))]);
  const domain = domainOf(rows.flatMap(([, rs]) => rs.flatMap((r) => (r.s ? [r.s.value, ...(r.s.ci || [])] : []))), { include: [0], count: 6 });
  const grid = ticks(domain, 6);
  return html`<section class="section" aria-labelledby="ev-h-cmp">
    <div class="section-head"><h2 id="ev-h-cmp">Paired differences</h2><span class="aside">Headline AUC of the predictor named in each group minus another predictor's, on the same posts (all posts)</span></div>
    <div class="ev-tablewrap"><table class="ev-table ev-forest ev-compact">
      <caption class="sr-only">Paired differences in headline AUC</caption>
      <thead><tr><th scope="col">Against</th><th scope="col" class="ev-plot"><span class="sr-only">Interval plot</span></th><th scope="col" class="r">Difference</th></tr></thead>
      ${rows.map(([name, rs]) => html`<tbody>
        <tr class="grp"><th scope="colgroup" colspan="3"><span class="ev-grpname">${name}</span> minus</th></tr>
        ${rs.map((r) => html`<tr>
          <th scope="row"><span class="mono ev-pname">${r.other}</span></th>
          <td class="ev-plot">${r.s ? ciRow({ domain, value: r.s.value, ci: r.s.ci, refs: 0, grid, label: `${name} minus ${r.other}: ${sgn(r.s.value)}${r.s.ci ? `, 95% interval ${sgn(r.s.ci[0])} to ${sgn(r.s.ci[1])}` : ", no interval"}` }) : ""}</td>
          <td class="r num">${r.s ? html`${sgn(r.s.value)}<small>${ci2(r.s.ci, 2, sgn)}</small>` : "–"}</td>
        </tr>`)}
      </tbody>`)}
      <tfoot><tr><td></td><td class="ev-plot">${ciAxis({ domain, ticks: grid, format: (v) => sgn(v, 2), low: "the other wins", high: "the grouped predictor wins", max: 7 })}</td><td></td></tr></tfoot>
    </table></div>
    <p class="ev-legend">Filled point: the interval clears zero, so one predictor beats the other on these posts.</p>
  </section>`;
}

function notesSection() {
  const r = S.report;
  const rows = predictors().filter(([, res]) => finite(res.calls) || isObj(res.retest) || (Array.isArray(res.failures) && res.failures.length) || res.seconds > 0);
  const dropped = isObj(r.dropped) ? Object.entries(r.dropped) : [];
  return html`<section class="section" aria-labelledby="ev-h-notes">
    <div class="section-head"><h2 id="ev-h-notes">Run notes</h2>${r.protocol ? html`<span class="aside">${r.protocol}</span>` : ""}</div>
    ${finite(r.n_eligible) || dropped.length
      ? html`<p class="ev-note">${finite(r.n_eligible) ? `${compact(r.n_eligible)} posts were eligible. ` : ""}${dropped.length ? `Dropped before sampling: ${dropped.map(([k, v]) => `${compact(v)} ${human(k)}`).join(", ")}.` : ""}${r.rescored_at ? ` Rescored ${clock(r.rescored_at, true)}.` : ""}</p>`
      : ""}
    ${rows.length
      ? html`<div class="ev-tablewrap"><table class="ev-table ev-notes">
          <thead><tr><th scope="col">Predictor</th><th scope="col" class="r">Model calls</th><th scope="col" class="r">Time</th><th scope="col" class="r">Retest</th><th scope="col">Failures</th></tr></thead>
          <tbody>${rows.map(([name, res]) => html`<tr>
            <th scope="row"><span class="mono ev-pname">${name}</span></th>
            <td class="r num">${finite(res.calls) ? compact(res.calls) : "–"}</td>
            <td class="r num">${finite(res.seconds) ? duration(res.seconds * 1000) : "–"}</td>
            <td class="r num">${isObj(res.retest) ? html`${num(res.retest.spearman_between_runs)}<small>Spearman, ${res.retest.n_posts ?? "?"} posts</small>` : "–"}</td>
            <td class="ev-failcell">${Array.isArray(res.failures) && res.failures.length ? html`<span class="chip flag">${res.failures.length}</span> <span class="ev-fail">${String(res.failures[0]).slice(0, 200)}</span>` : html`<span class="muted">none</span>`}</td>
          </tr>`)}</tbody>
        </table></div>`
      : ""}
  </section>`;
}

function confoundedPanel() {
  const rows = predictors().map(([name, res]) => ({ name, c: seg(res)?.confounded_by_account_size, cb: res.category_balanced })).filter((r) => isObj(r.c) || isObj(r.cb));
  if (!rows.length) return "";
  return html`<section class="panel ev-confounded" aria-labelledby="ev-h-conf">
    <div class="section">
      <div class="section-head"><span class="chip flag">Confounded</span><h2 id="ev-h-conf">Confounded by account size</h2></div>
      <p class="ev-note">These compare posts across accounts, so a model that knows which brands are big scores well here without judging the writing. They are not a measure of the predictor, and they are kept out of the headline on purpose.</p>
      <div class="ev-tablewrap"><table class="ev-table">
        <thead><tr><th scope="col">Predictor</th><th scope="col" class="r">Spearman vs log engagement</th><th scope="col" class="r">Super Viral vs rest AUC</th><th scope="col" class="r">Category-balanced Spearman<small>median, 95% range</small></th></tr></thead>
        <tbody>${rows.map((r) => html`<tr>
          <th scope="row"><span class="mono ev-pname">${r.name}</span></th>
          <td class="r num">${num(r.c?.spearman_vs_log_engagement)}</td>
          <td class="r num">${num(r.c?.super_viral_vs_rest_auc)}</td>
          <td class="r num">${isObj(r.cb) ? html`${num(r.cb.cross_account_spearman_median)}<small>${ci2(r.cb.cross_account_spearman_range95)}</small>` : "–"}</td>
        </tr>`)}</tbody>
      </table></div>
    </div>
  </section>`;
}

function reportMarkup() {
  const r = S.report;
  const v2 = predictors().some(([, res]) => isObj(res.all) && isObj(res.all[HEADLINE]));
  if (!v2) {
    return html`<section class="panel ev-report">
      ${reportFields(r)}
      <div class="section"><p class="notice warn"><b>Older report layout.</b> <span>${S.reportFile} has no within-account headline (protocol v2), so this view can't plot it. Its summary is in <code>eval/reports/${String(S.reportFile).replace(/\.json$/, ".md")}</code>.</span></p></div>
    </section>`;
  }
  return html`<section class="panel ev-report" aria-label="Backtest ${S.reportFile}">
      ${reportFields(r)}
      ${segmentBays()}
      ${headlineSection()}
      ${controlsSection()}
      ${comparisonsSection()}
      ${notesSection()}
    </section>
    ${confoundedPanel()}`;
}

function renderReport() {
  const box = $("#ev-report", S.el);
  if (!box) return;
  if (S.reportError) {
    setHTML(box, html`<div class="notice warn ev-inline-error" role="alert"><b>Couldn't load ${S.reportFile}.</b> <span>${S.reportError.message}</span>
      <button class="btn btn-quiet btn-small" type="button" data-retry="report">Try again</button></div>`);
    return;
  }
  setHTML(box, S.report ? reportMarkup() : html`<div class="panel"><div class="section">${skeletonRows(8)}</div></div>`);
}

async function loadReport(file) {
  S.reportFile = file;
  S.report = null;
  S.reportError = null;
  renderReport();
  const token = S.token;
  try {
    const data = await S.ctx.api.get(`/api/evals/${encodeURIComponent(file)}`);
    if (S?.token !== token || S.reportFile !== file) return;
    S.report = isObj(data) ? data : {};
  } catch (e) {
    if (S?.token !== token || S.reportFile !== file) return;
    S.reportError = e;
  }
  renderReport();
}

// --- Fault verdicts ---------------------------------------------------------------------------

function actionCell(id) {
  const handle = handleFromUrl(id);
  if (handle) {
    const tail = String(id).match(/status\/(\d+)/)?.[1] || "";
    return html`<a class="ev-action" href="${id}" target="_blank" rel="noopener noreferrer"><span class="mono">@${handle}</span> <span class="muted mono">…${tail.slice(-6)}</span></a>`;
  }
  return html`<span class="mono ev-action">${id ?? "–"}</span>`;
}

function outcomeCell(v) {
  const e = isObj(v.evidence) ? v.evidence : {};
  const { engagement: eng, account_p10: p10, account_p90: p90, account_median: med } = e;
  if (!finite(eng) || !finite(p10) || !finite(p90)) {
    return html`<span class="muted">${finite(eng) ? `${compact(eng)} · no account band` : "no outcome"}</span>`;
  }
  const domain = [0, Math.max(p90, eng, finite(med) ? med : 0) * 1.08 || 1];
  return html`<div class="ev-outcome">
    ${ciRow({ domain, value: eng, band: [p10, p90], refs: finite(med) ? med : null, size: "mini", filled: eng < p10 || eng > p90,
      label: `Engagement ${compact(eng)}; account's usual band ${compact(p10)} to ${compact(p90)}${finite(med) ? `, median ${compact(med)}` : ""}` })}
    <span class="num">${compact(eng)} <small>band ${compact(Math.round(p10))} to ${compact(Math.round(p90))}${finite(v.outcome_ratio) ? ` · ${v.outcome_ratio.toFixed(2)}× median` : ""}</small></span>
  </div>`;
}

function evidenceLine(e) {
  if (!isObj(e)) return "";
  const parts = [];
  if (finite(e.story_peer_ratio)) parts.push(`story ${e.story_peer_ratio.toFixed(2)}× usual${finite(e.n_peer_posts) ? ` over ${e.n_peer_posts} peer posts` : ""}`);
  if (finite(e.draft_score)) parts.push(`draft score ${e.draft_score.toFixed(2)}`);
  if (Array.isArray(e.critic_flags) && e.critic_flags.length) parts.push(`critic: ${e.critic_flags.join(", ")}`);
  if (finite(e.n_baseline_posts)) parts.push(`${e.n_baseline_posts} baseline posts`);
  return parts.join(" · ");
}

// Faults that name a stage first, then underperformances nobody could attribute, then the rest.
const RANK = ["action_fault", "observe_fault", "both", "unexplained", "insufficient_evidence", "uncertain", "no_fault", "variance"];
const rank = (v) => (RANK.includes(v.verdict) ? RANK.indexOf(v.verdict) : RANK.length);

function faultCounts(verdicts) {
  const c = {};
  for (const v of verdicts) c[v.verdict] = (c[v.verdict] || 0) + 1;
  return c;
}

function faultsBody() {
  const f = S.faults;
  const verdicts = (Array.isArray(f?.verdicts) ? f.verdicts : []).filter(isObj);
  const counts = faultCounts(verdicts);
  const max = Math.max(1, ...Object.values(counts));
  const byStage = {};
  for (const v of verdicts) if (v.stage_to_fix) byStage[v.stage_to_fix] = (byStage[v.stage_to_fix] || 0) + 1;
  const fix = Array.isArray(f?.summary?.fix) ? f.summary.fix : null;
  const unknown = Object.keys(counts).filter((k) => !VERDICT[k]);
  const shown = (S.fbay === "all" ? verdicts : S.fbay === "stage" ? verdicts.filter((v) => v.stage_to_fix) : verdicts.filter((v) => v.verdict === S.fbay)).slice().sort((a, b) => rank(a) - rank(b));
  const lc = f?.liveness_check;
  const countRow = (k, word, gist) => html`<li><button class="ev-count" type="button" data-fbay="${k}" aria-pressed="${S.fbay === k ? "true" : "false"}" ${counts[k] ? "" : html`data-zero`}>
      <span class="ev-count-word">${word}</span><span class="ev-count-gist">${gist}</span>
      <span class="num ev-count-n">${counts[k] || 0}</span><span class="meter" aria-hidden="true"><i style="width:${((counts[k] || 0) / max) * 100}%"></i></span>
    </button></li>`;
  return html`
    <div class="ev-faulthead">
      <p class="ev-fix">${fix?.length
        ? html`<b>Fix ${fix.join(" and ")}.</b> ${fix.map((st) => `${byStage[st] || 0} verdicts point at ${st}`).join("; ")}.`
        : fix
          ? html`<b>No stage to fix yet.</b> No stage has enough matching verdicts. One underperformance proves little: a tenth of ordinary posts land under their account's 10th percentile.`
          : html`<b>No summary in this file,</b> so the repeat threshold for naming a stage isn't recorded. ${Object.entries(byStage).map(([st, n]) => `${n} verdicts point at ${st}.`).join(" ")}`}</p>
      ${isObj(lc) ? html`<p class="ev-note">Does a live story predict a post's own result? ${finite(lc.spearman_own_vs_peer) ? `Spearman ${num(lc.spearman_own_vs_peer, 2)} over ${lc.n_posts} posts with ${lc.min_peers}+ peer posts.` : `Not measurable: only ${lc.n_posts ?? 0} posts had ${lc.min_peers ?? "enough"} or more peer posts.`}</p>` : ""}
    </div>
    <div class="ev-faultgrid">
      <div class="ev-counts">
        <button class="ev-count ev-count-all" type="button" data-fbay="all" aria-pressed="${S.fbay === "all" ? "true" : "false"}"><span class="ev-count-word">All verdicts</span><span class="num ev-count-n">${verdicts.length}</span></button>
        <button class="ev-count ev-count-all" type="button" data-fbay="stage" aria-pressed="${S.fbay === "stage" ? "true" : "false"}"><span class="ev-count-word">Names a stage to fix</span><span class="num ev-count-n">${verdicts.filter((v) => v.stage_to_fix).length}</span></button>
        <ul>${Object.entries(VERDICT).filter(([, d]) => d.stage).map(([k, d]) => countRow(k, d.word, d.gist))}</ul>
        <span class="label">Nothing to fix</span>
        <ul>${Object.entries(VERDICT).filter(([, d]) => !d.stage).map(([k, d]) => countRow(k, d.word, d.gist))}${unknown.map((k) => countRow(k, human(k), "a verdict this view doesn't know"))}</ul>
      </div>
      <div class="ev-verdicts">
        ${shown.length
          ? html`<div class="ev-tablewrap"><table class="ev-table ev-vtable">
              <caption class="sr-only">Fault verdicts${S.fbay === "stage" ? ", only those naming a stage" : S.fbay !== "all" ? `, filtered to ${VERDICT[S.fbay]?.word || S.fbay}` : ""}</caption>
              <thead><tr><th scope="col">Action</th><th scope="col">Verdict</th><th scope="col">Outcome against the account</th><th scope="col">Why</th></tr></thead>
              <tbody>${shown.map((v) => html`<tr>
                <th scope="row">${actionCell(v.action_id)}</th>
                <td><span class="chip ${VERDICT[v.verdict]?.stage ? "flag" : ""}">${VERDICT[v.verdict]?.word || human(v.verdict || "no verdict")}</span>${v.stage_to_fix ? html`<small class="ev-stage">fix ${v.stage_to_fix}</small>` : ""}</td>
                <td>${outcomeCell(v)}</td>
                <td class="ev-why">
                  ${Array.isArray(v.reasons) && v.reasons.length ? html`<ul>${v.reasons.map((r) => html`<li>${r}</li>`)}</ul>` : ""}
                  ${Array.isArray(v.missing) && v.missing.length ? html`<p class="ev-missing">Missing: ${v.missing.join(", ")}</p>` : ""}
                  ${evidenceLine(v.evidence) ? html`<p class="ev-evid">${evidenceLine(v.evidence)}</p>` : ""}
                  ${v.evidence?.notes ? html`<p class="ev-evid">${v.evidence.notes}</p>` : ""}
                </td>
              </tr>`)}</tbody>
            </table></div>`
          : html`<p class="ev-note ev-none">${verdicts.length ? "No verdicts of this kind in this file." : "This file holds no verdicts."}</p>`}
      </div>
    </div>`;
}

function faultsPanel() {
  const L = S.faultsList;
  let body;
  if (L?.error) {
    body = html`<p class="notice warn" role="alert"><b>Couldn't load fault verdicts.</b> <span>${L.error.message}</span></p>`;
  } else if (!L || L.available === false) {
    body = html`<p class="ev-note">${L?.reason || "No eval/reports/ on this branch."} Fault verdicts come from <code>${L?.command || FAULT_COMMAND}</code>.</p>`;
  } else if (!(L.files || []).length) {
    body = html`<div class="ev-empty"><p><b>No fault verdicts yet.</b> Write a file with <code>${L.command || FAULT_COMMAND}</code>. The command has no default output path, so name it <code>eval/reports/fault-*.json</code> for this view to find it.</p></div>`;
  } else if (S.faultsError) {
    body = html`<div class="notice warn" role="alert"><b>Couldn't load ${S.faultFile}.</b> <span>${S.faultsError.message}</span> <button class="btn btn-quiet btn-small" type="button" data-retry="faults">Try again</button></div>`;
  } else if (!S.faults) {
    body = skeletonRows(4);
  } else {
    body = faultsBody();
  }
  const files = L?.files || [];
  return html`<section class="panel ev-faults" id="ev-faults" aria-labelledby="ev-h-fault">
    <div class="section">
      <div class="section-head ev-fhead">
        <h2 id="ev-h-fault">Fault verdicts</h2><span class="aside">Who was at fault when a draft underperformed, and which stage to fix</span>
        ${files.length > 1
          ? html`<label class="ev-pick"><span class="label">File</span><select class="select" data-pick="faults">${files.map((x) => html`<option value="${x.file}" ${x.file === S.faultFile ? html`selected` : ""}>${x.file} · ${x.n_verdicts} verdicts</option>`)}</select></label>`
          : files.length ? html`<span class="mono ev-file">${files[0].file}</span>` : ""}
      </div>
      ${problems(L?.problems, "eval/reports/")}
      ${body}
    </div>
  </section>`;
}

function renderFaults() {
  const box = $("#ev-faults", S.el);
  if (box) box.outerHTML = String(faultsPanel());
}

async function loadFaults(file) {
  S.faultFile = file;
  S.faults = null;
  S.faultsError = null;
  renderFaults();
  const token = S.token;
  try {
    const data = await S.ctx.api.get(`/api/evals/faults/${encodeURIComponent(file)}`);
    if (S?.token !== token || S.faultFile !== file) return;
    S.faults = isObj(data) ? data : {};
  } catch (e) {
    if (S?.token !== token || S.faultFile !== file) return;
    S.faultsError = e;
  }
  renderFaults();
}

// --- Mount ------------------------------------------------------------------------------------

function render() {
  const L = S.list;
  const reports = L.reports || [];
  let reportArea;
  if (L.available === false) {
    reportArea = html`<p class="notice"><b>Unavailable.</b> <span>${L.reason || "No eval reports on this branch."}</span></p>
      <div class="state"><span class="label">No reports here</span><h2>Backtests appear once eval/reports/ exists</h2>
        <p><code>python -m harness eval backtest --models claude-sonnet-5-5,gpt-6.1-sol</code> scores the sample with each predictor and writes <code>eval/reports/backtest-&lt;time&gt;-&lt;provider&gt;.json</code>; it calls the models, so it costs money. Merging sam/eval brings the reports already run.</p></div>`;
  } else if (!reports.length) {
    reportArea = html`<div class="state"><span class="label">No reports yet</span><h2>No backtest reports</h2>
      <p>eval/reports/ exists but holds no <code>backtest-*.json</code>. <code>python -m harness eval backtest</code> writes one; it calls the judge models.</p></div>`;
  } else {
    reportArea = html`<div id="ev-report"></div>`;
  }
  setHTML(
    S.el,
    html`${head()}
    <div class="ev">
      ${problems(L.problems, "eval/reports/")}
      ${reportArea}
      ${faultsPanel()}
    </div>`,
  );
}

registerView({
  id: "evals",
  title: "Eval reports",
  group: "learn",
  css: "/static/css/views/evals.css",
  async mount(el, ctx, params) {
    const token = {};
    S = { el, ctx, token, list: null, report: null, reportFile: null, reportError: null, seg: "all", faultsList: null, faults: null, faultFile: null, faultsError: null, fbay: "all", off: [] };
    setHTML(el, skeleton());
    const [list, faults] = await Promise.all([ctx.api.get("/api/evals"), ctx.api.get("/api/evals/faults").catch((error) => ({ error }))]);
    if (S?.token !== token) return;
    S.list = isObj(list) ? list : { available: false, reason: "The server sent no report list." };
    S.faultsList = faults;
    S.seg = SEGMENTS.some(([k]) => k === params.seg) ? params.seg : "all";
    S.fbay = /^\w+$/.test(params.fbay || "") ? params.fbay : "all";
    const reports = S.list.reports || [];
    S.reportFile = reports.some((r) => r.file === params.report) ? params.report : reports[0]?.file ?? null;
    const ffiles = faults?.files || [];
    S.faultFile = ffiles.some((x) => x.file === params.faults) ? params.faults : ffiles[0]?.file ?? null;
    render();
    if (S.reportFile) loadReport(S.reportFile);
    if (S.faultFile) loadFaults(S.faultFile);
    S.off.push(
      on(el, "change", "[data-pick]", (e, sel) => {
        if (sel.dataset.pick === "report") {
          ctx.setParams({ report: sel.value });
          loadReport(sel.value);
        } else {
          ctx.setParams({ faults: sel.value, fbay: null });
          S.fbay = "all";
          loadFaults(sel.value);
        }
      }),
      on(el, "click", "[data-seg]", (e, b) => {
        if (b.disabled || S.seg === b.dataset.seg) return;
        S.seg = b.dataset.seg;
        ctx.setParams({ seg: S.seg === "all" ? null : S.seg });
        renderReport();
        $(`[data-seg="${S.seg}"]`, el)?.focus();
      }),
      on(el, "click", "[data-fbay]", (e, b) => {
        S.fbay = S.fbay === b.dataset.fbay && b.dataset.fbay !== "all" ? "all" : b.dataset.fbay;
        ctx.setParams({ fbay: S.fbay === "all" ? null : S.fbay });
        renderFaults();
        $(`[data-fbay="${b.dataset.fbay}"]`, el)?.focus();
      }),
      on(el, "click", "[data-retry]", (e, b) => (b.dataset.retry === "report" ? loadReport(S.reportFile) : loadFaults(S.faultFile))),
    );
  },
  unmount() {
    for (const off of S?.off || []) off();
    S = null;
  },
});
