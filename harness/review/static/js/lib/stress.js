// ?fixture=stress: the realistic fixture pushed to its limits, generated in the tab so the repo
// carries no megabyte fixture. It covers what breaks layouts: long and unbreakable text, mixed
// scripts and emoji, 160 strips, missing optional fields and unknown codes.

const LONG =
  "This draft keeps going so the layout has to wrap it, with a URL " +
  "https://www.espn.com/wnba/story/_/id/12345678/a-very-long-path-that-counts-as-twenty-three and an unbreakable token " +
  "ABCDEFGHIJKLMNOPQRSTUVWXYZ".repeat(5);
const UNI = "日本のファンもクラークを応援してる 🏀🔥 العربية أيضاً — Ünïcödé, emoji 👩🏽‍🏫🇺🇸 and RTL mixed in.";
const STATUSES = ["pending", "pending", "pending", "approved", "killed"];

export function stressFrom(routes) {
  const out = structuredClone(routes);
  const runKey = Object.keys(out).find((k) => /^GET \/api\/runs\/\d+$/.test(k));
  if (runKey) {
    const base = out[runKey];
    out[runKey] = Array.from({ length: 160 }, (_, i) => {
      const a = structuredClone(base[i % base.length]);
      a.id = `${a.id}s${i}`;
      if (a.type !== "follow") a.text = [a.text, LONG, UNI, "ok"][i % 4];
      if (i % 3 === 0) a.rationale = LONG;
      if (i % 5 === 0) {
        a.panel = {};
        delete a.draft_meta;
      }
      if (i % 7 === 0) a.narrative_id = "a-narrative-slug-nobody-wrote-a-title-for-".repeat(3);
      a.review = { status: STATUSES[i % 5], text: i % 9 === 0 ? `edited ${UNI}` : null, note: i % 4 === 0 ? UNI : "", reasons: [], reviewed_at: new Date().toISOString() };
      return a;
    });
    const runs = out["GET /api/runs"];
    if (runs?.[0]) runs[0] = { ...runs[0], n_actions: 160, sources: "espn_news,google_news,x_targets,bluesky,attention,espn_scores,espn_injuries" };
    const dec = out[`${runKey}/decisions`];
    if (dec?.decisions) {
      dec.decisions.push(
        { lane: "reply", opportunity: "", tool: "abstain", reason: "a_code_nobody_documented", detail: LONG, question: "", action_id: null, candidates: [] },
        { lane: "post", opportunity: "no-such-narrative", tool: "ask_human", reason: "", detail: "", question: `${UNI} ${LONG}? The harness picks the timing.`, action_id: null, candidates: [{ mind: "x".repeat(60), type: "post", text: LONG, gate: [] }] },
      );
    }
  }
  return out;
}
