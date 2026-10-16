// Formatting shared by every view. Numbers and times follow X's own conventions where the
// value is something X shows (counts, post age), so the preview reads like the real thing.

const compactFmt = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });

/** 1.8K, 41.2K, 1.2M; exact below 10,000 like X. */
export function compact(n) {
  if (n == null || Number.isNaN(+n)) return "–";
  n = +n;
  if (Math.abs(n) < 10_000) return n.toLocaleString("en");
  return compactFmt.format(n);
}

/** A signed number for effect sizes: +0.035, −0.012. */
export function signed(x, digits = 3) {
  if (x == null || Number.isNaN(+x)) return "–";
  const s = Math.abs(x).toFixed(digits);
  return x > 0 ? `+${s}` : x < 0 ? `−${s}` : s;
}

export function fixed(x, digits = 2) {
  if (x == null || Number.isNaN(+x)) return "–";
  return (+x).toFixed(digits);
}

export const minutesSince = (iso, now = Date.now()) => {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : (now - t) / 60000;
};

/** X's post age: 45s, 14m, 3h, then "Oct 14". */
export function xAge(minutes, iso) {
  if (minutes == null) return null;
  if (minutes < 1) return `${Math.max(1, Math.round(minutes * 60))}s`;
  if (minutes < 60) return `${Math.floor(minutes)}m`;
  if (minutes < 60 * 24) return `${Math.floor(minutes / 60)}h`;
  const d = iso ? new Date(iso) : new Date(Date.now() - minutes * 60000);
  return d.toLocaleDateString("en", { month: "short", day: "numeric" });
}

/** "3 min ago", "2 h ago", "Oct 14, 9:12 PM" for timestamps in the console's own voice. */
export function ago(iso, now = Date.now()) {
  const m = minutesSince(iso, now);
  if (m == null) return "–";
  if (m < 1) return "just now";
  if (m < 60) return `${Math.floor(m)} min ago`;
  if (m < 60 * 24) return `${Math.floor(m / 60)} h ago`;
  return clock(iso, true);
}

export function clock(iso, withDate = false) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "–";
  const time = d.toLocaleTimeString("en", { hour: "numeric", minute: "2-digit" });
  return withDate ? `${d.toLocaleDateString("en", { month: "short", day: "numeric" })}, ${time}` : time;
}

/** "2137": the 24-hour clock controllers write on strips, used on stamps. */
export function stampTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${String(d.getHours()).padStart(2, "0")}${String(d.getMinutes()).padStart(2, "0")}`;
}

/** "640 ms", "4.2 s", "38 s", "2 min 5 s". Rounds once, so 119.6 s reads "2 min 0 s", not "1 min 60 s". */
export function duration(ms) {
  if (ms == null) return "–";
  if (ms < 999.5) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 9.95) return `${s.toFixed(1)} s`;
  const t = Math.round(s);
  return t < 60 ? `${t} s` : `${Math.floor(t / 60)} min ${t % 60} s`;
}

export const human = (s) => String(s ?? "").replaceAll("_", " ");

// X counts characters by weight, not code points (twitter-text v3): most Latin, punctuation
// and symbols weigh 1, CJK and other scripts weigh 2, every emoji weighs 2, and a link counts
// as 23 however long it is. The limit is 280 weighted characters.
//
// Links are http(s) URLs and bare domains such as espn.com/wnba. twitter-text recognises bare
// domains against the full TLD list; this list has only the common ones, so a bare domain on a
// rare TLD counts as its own length here and up to 23 on X. Trailing punctuation isn't part of
// the link, as on X.
const LIGHT = [
  [0, 4351],
  [8192, 8205],
  [8208, 8223],
  [8242, 8247],
];
const TLDS = "com|org|net|edu|gov|io|co|ai|app|dev|gg|tv|ly|me|news|info|us|uk|ca|au|de|fr";
const LINK_RE = new RegExp(
  String.raw`(?:https?:\/\/\S+?|(?<![\w@#$.\/-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:${TLDS})\b(?:\/\S*?)?)` +
    String.raw`(?=[.,:;!?)\]"']*(?:\s|$))`,
  "gi",
);
const EMOJI_RE = /\p{Extended_Pictographic}|\p{Regional_Indicator}|\u20E3/u;
const segmenter = typeof Intl.Segmenter === "function" ? new Intl.Segmenter("en", { granularity: "grapheme" }) : null;

export const X_LIMIT = 280;

export function xLength(text) {
  let n = 0;
  const withoutUrls = String(text ?? "").replace(LINK_RE, () => {
    n += 23;
    return "";
  });
  const graphemes = segmenter ? [...segmenter.segment(withoutUrls)].map((s) => s.segment) : [...withoutUrls];
  for (const g of graphemes) {
    if (EMOJI_RE.test(g)) {
      n += 2;
      continue;
    }
    for (const ch of g) {
      const cp = ch.codePointAt(0);
      n += LIGHT.some(([a, b]) => cp >= a && cp <= b) ? 1 : 2;
    }
  }
  return n;
}

/** A stable hue from a handle, for monogram avatars. */
export function hueOf(s) {
  let h = 0;
  for (const ch of String(s ?? "")) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

export function handleFromUrl(url) {
  const m = /^https?:\/\/(?:www\.)?(?:x|twitter)\.com\/(\w{1,15})\/status\//.exec(url || "");
  return m ? m[1] : null;
}

export const isXPost = (url) => handleFromUrl(url) != null;

export function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return null;
  }
}
