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
import { $, esc, html, isTyping, loadCSS, raw, setHTML } from "./lib/dom.js";

const views = new Map();
const GROUPS = [
  ["checkpoint", "Checkpoint"],
  ["loop", "Loop"],
  ["learn", "Learn"],
];
// Pages that live outside the shell, opened in place: { group, title, href, note }.
// Will's /console (PR #11) can add itself here.
const EXTERNAL = [];

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
  refreshNav,
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
  if (current) current.query = q;
}

async function route() {
  const { id, params } = parseHash();
  const def = views.get(id) || views.get("queue");
  // setParams() uses replaceState, which fires no hashchange, so reaching here with the same view
  // means someone followed a link or edited the URL: remount with the new params.
  const query = location.hash.split("?")[1] || "";
  if (current?.def === def && current.query === query) return;
  current?.def.unmount?.();
  const el = $("#view");
  el.replaceChildren();
  el.dataset.view = def.id;
  if (def.css) loadCSS(def.css);
  document.title = `${def.title} · Clearance`;
  const navigated = current != null; // not the first load
  current = { def, el, params, query };
  renderNav();
  $("#main").scrollTop = 0;
  try {
    await def.mount(el, ctx, params);
  } catch (e) {
    showError(el, e);
  }
  // After an in-app route change, move focus to the new view's heading so screen readers announce
  // it and Tab starts inside the view (better-accessibility: SPA route changes).
  if (navigated && current?.def === def) {
    const h1 = el.querySelector("h1");
    if (h1) {
      h1.tabIndex = -1;
      h1.focus({ preventScroll: true });
    }
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
          ${n ? html`<span class="count ${v.hot?.() ? "hot" : ""}">${n}<span class="sr-only"> waiting</span></span>` : ""}
        </a>`;
      })}
      ${ext.map((x) => html`<a class="nav-link" href="${x.href}" title="${x.note}"><span>${x.title}</span><span class="ext" aria-hidden="true">↗</span></a>`)}
    </div>`;
  });
  setHTML($("#nav"), html`${groups}`);
}

/** Update rail counts in place; rebuilding the rail would destroy a focused link. */
function refreshNav() {
  const nav = $("#nav");
  if (!nav.children.length) return renderNav();
  for (const v of views.values()) {
    const link = nav.querySelector(`a[href="#/${v.id}"]`);
    if (!link) continue;
    const n = v.count?.() || 0;
    let badge = link.querySelector(".count");
    if (!n) {
      badge?.remove();
      continue;
    }
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "count";
      link.append(badge);
    }
    badge.classList.toggle("hot", !!v.hot?.());
    badge.innerHTML = String(html`${n}<span class="sr-only"> waiting</span>`);
  }
}

function renderRailFoot() {
  if (!meta) return;
  setHTML(
    $("#rail-foot"),
    html`<div class="row" title="HARNESS_PROVIDER on this server: the provider a round started here uses">
        <span class="lamp ${meta.provider === "mock" ? "busy" : "on"}" aria-hidden="true"></span>
        <span>${meta.provider}${meta.model ? ` · ${meta.model}` : ""}</span>
      </div>
      <div class="row" title="HARNESS_SOURCES on this server: what a round started here reads. Each run's own sources are in the Queue header."><span class="label">Default sources</span><span>${(meta.sources || []).join(", ")}</span></div>
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
  const text = document.createElement("span");
  text.textContent = message;
  el.append(text);
  const leave = () => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 200);
  };
  if (kind === "error") {
    // Errors stay until the person closes them (motion-and-zoom.md: an error toast stays).
    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.textContent = "Close";
    close.addEventListener("click", leave);
    el.append(close);
  } else {
    setTimeout(leave, ms);
  }
  $("#toasts").append(el);
}

const GLOBAL_KEYS = [
  ["g q", "Queue"],
  ["g l", "Loop"],
  ["g e", "Experiments"],
  ["g m", "Memory"],
  ["?", "This sheet"],
];

function keyList(title, keys) {
  return html`<section class="keys-group"><h3>${title}</h3><dl class="keys">
    ${keys.map(([k, label]) => html`<dt>${k.split(/ \/ | /).map((p) => html`<kbd>${p}</kbd>`)}</dt><dd>${label}</dd>`)}
  </dl></section>`;
}

function openKeys() {
  const dlg = $("#keys-sheet");
  const viewKeys = current?.def.keys || [];
  setHTML(
    $("#keys-body"),
    html`${viewKeys.length ? keyList(current.def.title, viewKeys) : ""}${keyList("Anywhere", GLOBAL_KEYS)}
      <label class="keys-switch"><input type="checkbox" id="keys-on" ${shortcutsOn() ? raw("checked") : ""}> Single-key shortcuts (turn off for speech input; <kbd>?</kbd> still opens this sheet)</label>`,
  );
  $("#keys-on").addEventListener("change", (e) => {
    localStorage.setItem("clearance.keys", e.target.checked ? "on" : "off");
    readback(e.target.checked ? "Single-key shortcuts on." : "Single-key shortcuts off. Press ? to turn them back on.");
  });
  dlg.showModal();
}

// WCAG 2.1.4: single-character shortcuts need an off switch (speech input can fire them).
const shortcutsOn = () => localStorage.getItem("clearance.keys") !== "off";

// --- Keyboard -------------------------------------------------------------------------------

const GO = { q: "queue", l: "loop", e: "experiments", m: "memory" };

function onKey(e) {
  // ⌘↵ / Ctrl+↵ reach the view (clear from the editor); other ⌘ chords stay the browser's.
  if (e.defaultPrevented || e.altKey || (e.metaKey && e.key !== "Enter")) return;
  if ($("#keys-sheet").open) return;
  if (!isTyping(e.target) && !e.ctrlKey) {
    if (shortcutsOn() && gPending && Date.now() - gPending < 1200 && GO[e.key]) {
      gPending = 0;
      e.preventDefault();
      return navigate(GO[e.key]);
    }
    gPending = 0;
    if (e.key === "g" && shortcutsOn()) {
      gPending = Date.now();
      return;
    }
    if (e.key === "?") {
      e.preventDefault();
      return openKeys();
    }
    if (!shortcutsOn() && e.key.length === 1) return;
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
      setHTML(banner, html`Fixture: ${fx.name} <a href="${location.pathname}${location.hash}" title="Fixture data: verdicts stay in this tab. Switch to the live store.">Live</a>`);
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
    ["g", "then q l e m to switch"],
  ]);
  await route();
}

export { esc };
