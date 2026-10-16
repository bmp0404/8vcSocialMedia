// Interval plots: a point estimate, its 95% interval as a whisker, reference lines (0, chance at
// 0.5, a guardrail margin) and an optional shaded band (a noise floor, an account's usual range).
//
// Drawn as positioned spans on a percentage scale rather than SVG, so the plot stretches with its
// column without distorting the point, text stays crisp, and colours come from the tokens in both
// themes. Rows that share a `domain` line up under each other, and one `ciAxis` labels them all.
//
//   ciRow({ domain: [-0.2, 1], value: 0.59, ci: [0.29, 0.82], refs: 0, band: [-0.06, 0.06],
//           label: "Improvement +0.588, 95% interval +0.294 to +0.824" })
//   ciAxis({ domain, ticks: ticks(domain), format: (v) => v.toFixed(1), low: "worse", high: "better" })
//
// The point is filled when the interval clears the band (or, with no band, the first reference
// line) and hollow when it overlaps it, so a column of rows can be scanned for the ones that clear.
// Pass `filled` to decide that yourself. The styles are in components.css.

import { html } from "../lib/dom.js";

const finite = (v) => typeof v === "number" && Number.isFinite(v);

/** A nice step for about `count` intervals across the span: 1, 2 or 5 times a power of ten. */
function stepFor(span, count) {
  const raw = span / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
}

/** Tick values inside the domain, on a nice step. */
export function ticks([lo, hi], count = 5) {
  if (!(hi > lo)) return [lo];
  const step = stepFor(hi - lo, count);
  const out = [];
  for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : +v.toPrecision(12));
  return out;
}

/** A domain covering every finite value (and `include`), padded and rounded out to a nice step,
 *  then clamped, so the axis ends on round numbers. */
export function domainOf(values, { include = [], pad = 0.06, clamp = [-Infinity, Infinity], count = 5 } = {}) {
  const nums = [...values, ...include].filter(finite);
  if (!nums.length) return [0, 1];
  let lo = Math.min(...nums);
  let hi = Math.max(...nums);
  if (hi - lo < 1e-9) {
    const w = Math.abs(lo) > 1 ? Math.abs(lo) * 0.1 : 0.05;
    lo -= w;
    hi += w;
  }
  const p = (hi - lo) * pad;
  lo -= p;
  hi += p;
  const step = stepFor(hi - lo, count);
  lo = Math.floor(lo / step + 1e-9) * step;
  hi = Math.ceil(hi / step - 1e-9) * step;
  return [Math.max(clamp[0], +lo.toPrecision(12)), Math.min(clamp[1], +hi.toPrecision(12))];
}

/** Position of v on the domain as a percentage, clamped to the plot. */
export function at([lo, hi], v) {
  if (!(hi > lo)) return 50;
  return Math.min(100, Math.max(0, ((v - lo) / (hi - lo)) * 100));
}

const pctStyle = (n) => `${+n.toFixed(3)}%`;

/** True when the interval lies wholly outside the band, or wholly to one side of `ref`. */
export function clears(ci, { band = null, ref = null } = {}) {
  if (!ci || !finite(ci[0]) || !finite(ci[1])) return false;
  if (band) return ci[0] > band[1] || ci[1] < band[0];
  if (finite(ref)) return ci[0] > ref || ci[1] < ref;
  return false;
}

function normRefs(refs) {
  if (refs == null) return [];
  const list = Array.isArray(refs) ? refs : [refs];
  return list.map((r) => (finite(r) ? { at: r, kind: "ref" } : r)).filter((r) => r && finite(r.at));
}

/**
 * One interval on a scale. Options:
 *   domain   [lo, hi], required; rows that should line up share it
 *   value    point estimate (null draws an empty track)
 *   ci       [low, high] 95% interval, or null for a point alone
 *   refs     a number, or [{at, kind: "ref" | "threshold"}]; "threshold" draws dashed (a margin, a limit)
 *   band     [low, high] shaded behind the interval
 *   grid     values for faint vertical gridlines (usually the axis ticks)
 *   size     "row" (default) or "mini"
 *   tone     "ink" (default) or "muted" for context rows
 *   filled   force the point filled or hollow; default: filled when the interval clears band/ref
 *   label    spoken and shown on hover; without one the plot is hidden from assistive tech
 */
export function ciRow({ domain, value = null, ci = null, refs = null, band = null, grid = [], size = "row", tone = "ink", filled, label = "" }) {
  const R = normRefs(refs);
  const hasCi = Array.isArray(ci) && finite(ci[0]) && finite(ci[1]);
  const fill = filled ?? clears(hasCi ? ci : null, { band, ref: R[0]?.at });
  const [lo, hi] = domain;
  const parts = [];
  for (const g of grid) if (finite(g)) parts.push(html`<span class="ci-grid" style="left:${pctStyle(at(domain, g))}"></span>`);
  if (band && finite(band[0]) && finite(band[1])) {
    const a = at(domain, Math.min(band[0], band[1]));
    const b = at(domain, Math.max(band[0], band[1]));
    parts.push(html`<span class="ci-band" style="left:${pctStyle(a)};width:${pctStyle(b - a)}"></span>`);
  }
  for (const r of R) parts.push(html`<span class="ci-ref ${r.kind === "threshold" ? "threshold" : ""}" style="left:${pctStyle(at(domain, r.at))}"></span>`);
  if (finite(value)) {
    if (hasCi) {
      const a0 = Math.min(ci[0], ci[1]);
      const b0 = Math.max(ci[0], ci[1]);
      const a = at(domain, a0);
      const b = at(domain, b0);
      const open = `${a0 < lo ? "open-lo" : ""} ${b0 > hi ? "open-hi" : ""}`;
      parts.push(html`<span class="ci-whisker ${open}" style="left:${pctStyle(a)};width:${pctStyle(b - a)}"></span>`);
    }
    parts.push(html`<span class="ci-pt ${fill ? "" : "hollow"}" style="left:${pctStyle(at(domain, value))}"></span>`);
  } else {
    parts.push(html`<span class="ci-none"></span>`);
  }
  const a11y = label ? html`role="img" aria-label="${label}" title="${label}"` : html`aria-hidden="true"`;
  return html`<span class="ci ${size === "mini" ? "mini" : ""} ${tone === "muted" ? "muted" : ""}" ${a11y}>${parts}</span>`;
}

/** Tick labels for rows drawn on the same domain, with optional direction words under them.
 *  At most `max` labels print, stepping out from `anchor` (zero by default; chance at 0.5 for an
 *  AUC) so the anchor keeps its label. Every other printed label is ranked lower and hides when the
 *  axis is narrower than 260px, so the same markup reads on a phone. Pass every tick as `grid` to
 *  the rows to keep a gridline at each one. */
export function ciAxis({ domain, ticks: tv = ticks(domain), format = (v) => String(v), low = "", high = "", max = 6, anchor = 0 }) {
  const stride = Math.max(1, Math.ceil(tv.length / max));
  let z = tv.findIndex((v) => Math.abs(v - anchor) < 1e-9);
  if (z < 0) z = 0;
  const labels = tv.map((v, i) => {
    const d = Math.abs(i - z);
    if (d % stride) return "";
    const p = at(domain, v);
    const edge = p < 6 ? "start" : p > 94 ? "end" : "";
    return html`<span class="tick ${edge}" data-l="${d % (stride * 2) === 0 ? 0 : 1}" style="left:${pctStyle(p)}">${format(v)}</span>`;
  });
  const dir = low || high;
  return html`<span class="ci-axis ${dir ? "has-dir" : ""}" aria-hidden="true">${labels}${low ? html`<span class="dir lo">← ${low}</span>` : ""}${high ? html`<span class="dir hi">${high} →</span>` : ""}</span>`;
}
