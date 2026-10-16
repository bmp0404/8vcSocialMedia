// The post preview: how a draft and the post it answers look on X. It uses X's own palette,
// type stack and number formats inside an otherwise Clearance page, so the operator judges the
// draft the way a reader will meet it.

import { compact, handleFromUrl, hostOf, hueOf, minutesSince, xAge, X_LIMIT } from "../lib/format.js";
import { esc, html, raw } from "../lib/dom.js";

const ICON = {
  reply: "M4 5.6A2.6 2.6 0 0 1 6.6 3h6.8A2.6 2.6 0 0 1 16 5.6v4.8a2.6 2.6 0 0 1-2.6 2.6H9.2L6 15.8V13a2.6 2.6 0 0 1-2-2.6z",
  repost: "M5.5 8 8 5.5 10.5 8M8 6v6.2c0 .8.6 1.3 1.3 1.3H12M14.5 12 12 14.5 9.5 12M12 14V7.8c0-.8-.6-1.3-1.3-1.3H8",
  like: "M10 16s-6-3.5-6-8.1A3.2 3.2 0 0 1 10 6.3a3.2 3.2 0 0 1 6 1.6C16 12.5 10 16 10 16z",
  views: "M5 15.5V10M10 15.5V4.5M15 15.5V8",
};

const icon = (name) =>
  raw(`<svg class="xi" viewBox="0 0 20 20" aria-hidden="true"><path d="${ICON[name]}"/></svg>`);

export function avatar(handle, { ours = false } = {}) {
  if (ours) return html`<span class="xav ours" aria-hidden="true">${(handle || "Our")[0].toUpperCase()}</span>`;
  const h = hueOf(handle);
  return html`<span class="xav" style="--h:${h}" aria-hidden="true">${(handle || "?")[0].toUpperCase()}</span>`;
}

/** Post text with @mentions, #tags and links coloured the way X shows them. */
export function xText(text) {
  const parts = String(text ?? "").split(/(https?:\/\/[^\s]+|@\w{1,15}|#\w+)/g);
  return raw(
    parts
      .map((p, i) => {
        if (i % 2 === 0) return esc(p);
        if (p.startsWith("http")) return `<span class="xlink">${esc(hostOf(p) || p)}${p.length > 30 ? "…" : ""}</span>`;
        return `<span class="xlink">${esc(p)}</span>`;
      })
      .join(""),
  );
}

export function metricsRow(m = {}) {
  const cell = (name, value, label) =>
    html`<span class="xm" title="${label}">${icon(name)}<span>${value == null ? "" : compact(value)}</span><span class="sr-only">${label}</span></span>`;
  return html`<div class="xmetrics">
    ${cell("reply", m.replies, "replies")} ${cell("repost", m.reposts, "reposts")} ${cell("like", m.likes, "likes")}
    ${cell("views", m.views, "views")}
  </div>`;
}

// Some recorders write counts as strings ("152"); the preview does arithmetic on them.
const n = (v) => (v == null || v === "" || Number.isNaN(+v) ? null : +v);

/** A normalised target: whatever the run recorded about the post a reply or quote answers. */
export function targetOf(action, context) {
  const meta = action.draft_meta?.target;
  const url = meta?.url || action.target_url;
  if (!url) return null;
  const fromNarrative = context?.targets?.get(url);
  const sig = context?.signals?.get(url);
  const base = { ...(sig ? { author: sig.author, text: sig.text, created_at: sig.created_at, ...sig.metrics } : {}), ...fromNarrative, ...meta };
  const text = base.text || stripPosted(action.target_text);
  return {
    url,
    author: (base.author || action.target_handle || handleFromUrl(url) || "").replace(/^@/, ""),
    text: text || null,
    created_at: base.created_at || null,
    age_minutes: n(base.age_minutes),
    followers: n(base.author_followers),
    metrics: { likes: n(base.likes), reposts: n(base.reposts), replies: n(base.replies), quotes: n(base.quotes), views: n(base.views) },
    engagement_per_hour: n(base.engagement_per_hour),
    views_per_hour: n(base.views_per_hour),
    thread: base.thread || [],
    thread_attached: base.thread_attached ?? null,
    verified: base.metrics_verified !== false && base.metrics_unverified !== 1,
    isX: handleFromUrl(url) != null,
  };
}

function stripPosted(t) {
  if (!t) return "";
  return String(t).replace(/^@\w+ posted:\n/, "").replace(/\n\(quoting https?:\/\/\S+\)$/, "");
}

/** Age now: from created_at if known, else the age observe saw plus the time since the run. */
export function liveAge(t, runStartedAt) {
  if (t.created_at) return minutesSince(t.created_at);
  if (t.age_minutes != null && runStartedAt) return t.age_minutes + (minutesSince(runStartedAt) ?? 0);
  return t.age_minutes;
}

export function targetCard(t, { runStartedAt, quoted = false } = {}) {
  const age = liveAge(t, runStartedAt);
  const ageText = xAge(age, t.created_at);
  const rate = t.views_per_hour != null ? `${compact(Math.round(t.views_per_hour))} views/h` : t.engagement_per_hour != null ? `${compact(Math.round(t.engagement_per_hour))} engagements/h` : null;
  return html`<article class="xpost ${quoted ? "quoted" : ""}" aria-label="Post by @${t.author}">
    ${quoted ? "" : avatar(t.author)}
    <div class="xbody">
      <header class="xhead">
        ${quoted ? avatar(t.author) : ""}
        <span class="xname">@${t.author}</span>
        ${t.followers != null ? html`<span class="xmuted">${compact(t.followers)} followers</span>` : ""}
        ${ageText ? html`<span class="xmuted" data-age-from="${t.created_at || ""}">· ${ageText}</span>` : ""}
        ${t.isX && !quoted ? html`<a class="xopen" href="${t.url}" target="_blank" rel="noopener noreferrer" title="Open on X">Open on X<span aria-hidden="true"> ↗</span></a>` : ""}
      </header>
      ${t.text ? html`<p class="xtext">${xText(t.text)}</p>` : html`<p class="xtext xmissing">The run didn't capture this post's text. <a href="${t.url}" target="_blank" rel="noopener noreferrer">Read it on ${t.isX ? "X" : hostOf(t.url)}</a>.</p>`}
      ${quoted ? "" : metricsRow(t.metrics)}
      ${rate && !quoted ? html`<p class="xvel"><span class="label">Velocity</span> ${rate} when observed${t.verified ? "" : " (counts read by a model, not X)"}</p>` : ""}
    </div>
  </article>`;
}

export function threadReplies(thread, limit = 3) {
  if (!thread?.length) return "";
  const shown = thread.slice(0, limit);
  return html`<div class="xthread" aria-label="Replies already on the post">
    <p class="label">Already in the replies · top ${shown.length} of ${thread.length}</p>
    ${shown.map(
      (r) => html`<div class="xreply">
        ${avatar(r.author || "r")}
        <div class="xbody">
          <header class="xhead">
            <span class="xname">${r.author ? `@${r.author}` : "reply"}</span>
            ${n(r.followers) != null ? html`<span class="xmuted">${compact(n(r.followers))} followers</span>` : ""}
          </header>
          <p class="xtext">${xText(r.text)}</p>
          <div class="xmetrics small"><span class="xm">${icon("like")}<span>${compact(n(r.likes) ?? 0)}</span><span class="sr-only">likes</span></span></div>
        </div>
      </div>`,
    )}
  </div>`;
}

/** X's character ring: fills toward the limit, turns amber with 20 left and red past it. */
export function charRing(n, limit = X_LIMIT) {
  const left = limit - n;
  const r = 9;
  const c = 2 * Math.PI * r;
  const frac = Math.min(1, Math.max(0, n / limit));
  const state = left < 0 ? "over" : left <= 20 ? "near" : "ok";
  return html`<span class="xring ${state}" role="img" aria-label="${n} of ${limit} characters">
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="${r}" class="track"/>
      <circle cx="12" cy="12" r="${r}" class="fill" stroke-dasharray="${(c * frac).toFixed(2)} ${c.toFixed(2)}" transform="rotate(-90 12 12)"/>
    </svg>
    ${left <= 20 ? html`<span class="xring-left">${left}</span>` : ""}
  </span>`;
}

export function profileCard(handle, { followers = null, note = "" } = {}) {
  return html`<article class="xprofile" aria-label="Profile @${handle}">
    ${avatar(handle)}
    <div class="xbody">
      <header class="xhead"><span class="xname">@${handle}</span>${followers != null ? html`<span class="xmuted">${compact(followers)} followers</span>` : ""}</header>
      ${note ? html`<p class="xtext xmuted">${note}</p>` : ""}
    </div>
    <span class="xfollow" aria-hidden="true">Follow</span>
  </article>`;
}
