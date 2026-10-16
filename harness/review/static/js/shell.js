// The app shell: view registry, hash router, navigation rail, keyboard, readback bar, toasts.
//
// A view is one ES module that calls registerView() and is imported from views/index.js:
//
//   registerView({
//     id: "signals",              // route: #/signals
//     title: "Signals",
//     group: "loop",              // rail group: checkpoint | loop | learn
//     css: "/static/css/views/signals.css",   // optional, loaded on first mount
//     count: () => 3,             // optional badge in the rail
//     keys: [["j / k", "Move"]],  // optional, listed in the shortcut sheet
//     mount(el, ctx, params) {},  // render into el; params come from the hash query
//     unmount() {},               // stop timers
//     onKey(e) { return false },  // return true when the view handled the key
//   });

import * as api from "./lib/api.js";
import { $, esc, html, isTyping, loadCSS, setHTML } from "./lib/dom.js";

const views = new Map();
const GROUPS = [
  ["checkpoint", "Checkpoint"],
  ["loop", "Loop"],
  ["learn", "Learn"],
];
// Pages that live outside the shell for now, opened in place.
const EXTERNAL = [{ group: "loop", title: "Console", href: "/console", note: "Will's observability console" }];

let current = null; // { def, el, params }
let meta = null;
let gPending = 0;

export function registerView(def) {
  if (!def?.id || typeof def.mount !== "function") throw new Error(`registerView needs an id and a mount(); got ${def?.id}`);
  if (views.has(def.id)) throw new Error(`a view with id "${def.id}" is already registered`);
  views.set(def.id, { group: "loop", keys: [], ...def });
}

export const ctx = {
  api,
  get meta() {
    return meta;
  },
  navigate,
  setParams,
  toast,
  readback,
  refreshNav: renderNav,
};

// --- Routing --------------------------------------------------------------------------------

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [id, query = ""] = raw.split("?");
  return { id: id || "queue", params: Object.fromEntries(new URLSearchParams(query)) };
}

export function navigate(id, params = {}) {
  const q = new URLSearchParams(params).toString();
  location.hash = `#/${id}${q ? `?${q}` : ""}`;
}

/** Update the hash's query without remounting the view (for selection, filters, bays). */
export function setParams(params) {
  const { id } = parseHash();
  const merged = { ...parseHash().params, ...params };
  for (const k of Object.keys(merged)) if (merged[k] == null || merged[k] === "") delete merged[k];
  const q = new URLSearchParams(merged).toString();
  history.replaceState(null, "", `#/${id}${q ? `?${q}` : ""}`);
}

async function route() {
  const { id, params } = parseHash();
  const def = views.get(id) || views.get("queue");
  if (current?.def === def) return;
  current?.def.unmount?.();
  const el = $("#view");
  el.replaceChildren();
  el.dataset.view = def.id;
  if (def.css) loadCSS(def.css);
  document.title = `${def.title} · Clearance`;
  current = { def, el, params };
  renderNav();
  $("#main").scrollTop = 0;
  try {
    await def.mount(el, ctx, params);
  } catch (e) {
    showError(el, e);
  }
}

export function showError(el, e) {
  setHTML(
    el,
    html`<div class="state error" role="alert">
      <span class="label">${e?.status ? `Error ${e.status}` : "Error"}</span>
      <h2>${e?.message || String(e)}</h2>
      ${e?.path ? html`<p>While loading <code>${e.path}</code>.</p>` : ""}
      <button class="btn btn-quiet" type="button" data-retry>Try again</button>
    </div>`,
  );
  el.querySelector("[data-retry]")?.addEventListener("click", () => {
    const def = current?.def;
    current = null;
    if (def) route();
  });
}

// --- Rail -----------------------------------------------------------------------------------

function renderNav() {
  const active = current?.def.id;
  const groups = GROUPS.map(([g, label]) => {
    const items = [...views.values()].filter((v) => v.group === g && v.nav !== false);
    const ext = EXTERNAL.filter((x) => x.group === g);
    if (!items.length && !ext.length) return "";
    return html`<div class="nav-group" role="group" aria-label="${label}">
      <span class="label">${label}</span>
      ${items.map((v) => {
        const n = v.count?.();
        return html`<a class="nav-link" href="#/${v.id}" ${v.id === active ? html`aria-current="page"` : ""}>
          <span>${v.title}</span>
          ${n ? html`<span class="count ${v.hot?.() ? "hot" : ""}" aria-label="${n} waiting">${n}</span>` : ""}
        </a>`;
      })}
      ${ext.map((x) => html`<a class="nav-link" href="${x.href}" title="${x.note}"><span>${x.title}</span><span class="ext" aria-hidden="true">↗</span></a>`)}
    </div>`;
  });
  setHTML($("#nav"), html`${groups}`);
}

function renderRailFoot() {
  if (!meta) return;
  setHTML(
    $("#rail-foot"),
    html`<div class="row" title="HARNESS_PROVIDER, the model provider rounds use">
        <span class="lamp ${meta.provider === "mock" ? "busy" : "on"}" aria-hidden="true"></span>
        <span>${meta.provider}${meta.model ? ` · ${meta.model}` : ""}</span>
      </div>
      <div class="row" title="HARNESS_SOURCES"><span class="label">Sources</span><span>${(meta.sources || []).join(", ")}</span></div>
      <div class="row"><kbd>?</kbd><span>Shortcuts</span></div>`,
  );
}

// --- Readback, toasts, shortcut sheet --------------------------------------------------------

/** Say what the console just did. `chips` are [{label, key}] offered for a few seconds. */
export function readback(markup, { chips = [] } = {}) {
  setHTML($("#readback-text"), markup);
  setHTML(
    $("#readback-chips"),
    html`${chips.map((c) => html`<button class="chip" type="button" data-chip="${c.key}" aria-pressed="${c.on ? "true" : "false"}"><kbd>${c.key}</kbd>${c.label}</button>`)}`,
  );
}

export function setHints(hints) {
  setHTML($("#readback-hints"), html`${hints.map(([k, label]) => html`<span><kbd>${k}</kbd>${label}</span>`)}`);
}

export function toast(message, { kind = "info", ms = 4200 } = {}) {
  const el = document.createElement("div");
  el.className = `toast ${kind === "error" ? "error" : ""}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 200);
  }, ms);
}

const GLOBAL_KEYS = [
  ["g q", "Queue"],
  ["g l", "Loop"],
  ["g e", "Experiments"],
  ["g m", "Memory"],
  ["g v", "Eval reports"],
  ["?", "This sheet"],
];

function openKeys() {
  const dlg = $("#keys-sheet");
  const viewKeys = current?.def.keys || [];
  setHTML(
    $("#keys-body"),
    html`<dl class="keys">
      ${viewKeys.length ? html`<h3>${current.def.title}</h3>` : ""}
      ${viewKeys.map(([k, label]) => html`<dt>${k.split(" / ").map((p, i) => html`${i ? " " : ""}<kbd>${p}</kbd>`)}</dt><dd>${label}</dd>`)}
      <h3>Anywhere</h3>
      ${GLOBAL_KEYS.map(([k, label]) => html`<dt>${k.split(" ").map((p) => html`<kbd>${p}</kbd>`)}</dt><dd>${label}</dd>`)}
    </dl>`,
  );
  dlg.showModal();
}

// --- Keyboard -------------------------------------------------------------------------------

const GO = { q: "queue", l: "loop", e: "experiments", m: "memory", v: "evals" };

function onKey(e) {
  if (e.defaultPrevented || e.metaKey || e.altKey) return;
  if ($("#keys-sheet").open) return;
  if (!isTyping(e.target) && !e.ctrlKey) {
    if (gPending && Date.now() - gPending < 1200 && GO[e.key]) {
      gPending = 0;
      e.preventDefault();
      return navigate(GO[e.key]);
    }
    gPending = 0;
    if (e.key === "g") {
      gPending = Date.now();
      return;
    }
    if (e.key === "?") {
      e.preventDefault();
      return openKeys();
    }
  }
  if (current?.def.onKey?.(e)) e.preventDefault();
}

// --- Boot -----------------------------------------------------------------------------------

export async function boot() {
  try {
    const fx = await api.initFixture();
    if (fx) {
      const banner = $("#fixture-banner");
      banner.hidden = false;
      setHTML(banner, html`Fixture: ${fx.name} <a href="${location.pathname}${location.hash}" title="Verdicts in fixture mode stay in this tab">Use live data</a>`);
    }
  } catch (e) {
    toast(e.message, { kind: "error" });
  }
  try {
    meta = await api.get("/api/meta");
  } catch (e) {
    meta = null;
  }
  renderRailFoot();
  window.addEventListener("hashchange", route);
  document.addEventListener("keydown", onKey);
  $("#keys-close").addEventListener("click", () => $("#keys-sheet").close());
  $("#keys-sheet").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) e.currentTarget.close();
  });
  setHints([
    ["?", "shortcuts"],
    ["g", "then q l e m v to switch"],
  ]);
  await route();
}

export { esc };
