"""Learn: turn predicted vs. actual (and the bot's skip reasons) into lessons, weights and mission proposals.

One `learn(llm)` pass:
  1. evidence: the best and worst actions by actual score (feedback.actual_score), or by predicted score
     flagged `unverified` when nothing is measured yet, plus every bot skip / human kill with its reason,
     plus the calibration summary (feedback.calibration).
  2. lessons: the LLM returns keyed lessons; they are merged into a structured store
     (`<lessons>.json`, next to the lessons file), deduplicated by key and wording, newest evidence
     overwriting older lessons, capped at MAX_LESSONS, and rendered to the lessons markdown that
     `memory.recall()` reads (`## General` / `## Reply lane` / `## Post lane`).
  3. mission proposals: the mission is never edited. When SUSTAINED (3+) verified data points point the
     same way, a proposal is appended to out/mission-proposals.md for a human to approve.
  4. weights: per-mind and per-action-type multipliers (data/weights.json by default) the lanes can use
     to allocate drafts; read them with `get_weights(lane)`.

Env overrides: HARNESS_WEIGHTS (weights file), HARNESS_PROPOSALS (proposals file).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from statistics import mean, median

from harness import config, feedback, memory
from harness.llm import LLM

MAX_LESSONS = 12
MAX_NEW_PER_RUN = 8
SUSTAINED = 3  # agreeing verified data points needed before proposing a mission change
SHRINK = 3  # weights: pseudo-count pulling small groups toward the overall mean
SECTIONS = {"general": "## General", "reply": "## Reply lane", "post": "## Post lane"}


def lessons_store_path():
    return config.LESSONS_PATH.with_suffix(".json")


def weights_path():
    return config._path("HARNESS_WEIGHTS", str(config.DB_PATH.with_name("weights.json")))


def proposals_path():
    return config._path("HARNESS_PROPOSALS", "out/mission-proposals.md")


SYSTEM = """You are the performance analyst for the brand account in the MISSION. You get the best and
worst recent actions (with predicted vs. actual scores), the posting bot's skip reasons, and a
calibration table of the audience panel. Distill concise, actionable lessons for the content minds.

Rules:
- At most 8 lessons this run. Each is one imperative sentence, under 200 characters, specific
  (name the mind, action type, target kind, topic, or wording pattern), and cites evidence ids.
- Reuse a previous lesson's `key` when you restate, refine or contradict it; the newest evidence wins.
  Put keys of previous lessons the new evidence disproves in `drop`. Never write near-duplicates.
- Evidence flagged "verified": false is only a prediction: phrase those lessons tentatively.
- `direction`: "do" (more of this), "avoid" (stop this), or "calibrate" (the panel is off here).
- `lane`: "reply", "post", or "general" if it applies to both.
- Lessons must stay inside the MISSION's red lines; never suggest breaking them.
- mission_proposals: only for a change to the MISSION text itself (a new Do/Don't, voice or subject
  change), and only when 3+ verified data points agree. A human approves them; you never edit it."""

PROMPT = """MISSION:
{mission}

PREVIOUS LESSONS (key, lane, text):
{lessons}

CALIBRATION (score 0-1; bias = actual - predicted, negative = panel over-predicted):
{calibration}

BEST ACTIONS:
{best}

WORST ACTIONS:
{worst}

SKIPPED BY THE POSTING BOT / KILLED IN REVIEW (with reasons):
{rejected}

MISSION PROPOSALS ALREADY PENDING HUMAN APPROVAL (don't re-propose these):
{pending}

Return JSON:
{{"lessons": [{{"key": "kebab-case-slug", "lane": "reply|post|general", "direction": "do|avoid|calibrate",
   "text": "...", "evidence": ["<action id>"]}}],
 "drop": ["<previous key>"],
 "mission_proposals": [{{"key": "kebab-case-slug", "section": "Do|Don't|Voice and tone|Core subjects|Goals",
   "direction": "do|avoid", "change": "exact text to add or change in the mission", "why": "...",
   "evidence": ["<action id>"]}}]}}"""


# --------------------------------------------------------------------------- helpers


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) > 3}


def _similar(a: str, b: str, threshold: float = 0.6) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= threshold


def _slug(text: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", text.lower())[:6]) or "lesson"


def _evidence(r: dict, verified: bool) -> dict:
    return {
        "id": r["id"], "lane": r["lane"], "mind": r["mind"], "type": r["type"],
        "relationship": r["relationship"], "target": r.get("target_url") or r.get("target_handle"),
        "text": (r.get("text") or "")[:280], "predicted": r.get("predicted_score"),
        "actual": r.get("actual_score"), "delta": r.get("delta"), "metrics": r.get("metrics") or None,
        "verdict": r.get("verdict"), "reason": r.get("reason"), "reason_code": r.get("reason_code"),
        "verified": verified,
    }


def gather(rows: list[dict], k: int = 5) -> dict:
    """Best/worst/rejected evidence for the LLM; falls back to predicted (unverified) without actuals."""
    measured = [r for r in rows if r["actual_score"] is not None]
    rejected = [_evidence(r, True) for r in rows if r.get("verdict") in ("skipped", "killed")]
    if measured:
        ranked = sorted(measured, key=lambda r: r["actual_score"], reverse=True)
        verified = True
    else:
        pool = [r for r in rows if r.get("predicted_score") is not None and r.get("type") != "follow"
                and r.get("verdict") not in ("skipped", "killed") and not r.get("vetoed")]  # follow score isn't the panel's
        ranked = sorted(pool, key=lambda r: r["predicted_score"], reverse=True)
        verified = False
    best = ranked[:k]
    worst = [r for r in reversed(ranked) if r not in best][:k]
    return {
        "verified": verified,
        "best": [_evidence(r, verified) for r in best],
        "worst": [_evidence(r, verified) for r in worst],
        "rejected": rejected[:15],
    }


def _agreeing(ids: list[str], direction: str, by_id: dict[str, dict]) -> list[str]:
    """Evidence ids that are verified and actually point in `direction`."""
    scores = [r["actual_score"] for r in by_id.values() if r["actual_score"] is not None]
    mid = median(scores) if scores else None
    deltas = [by_id[i]["delta"] for i in ids if i in by_id and by_id[i]["delta"] is not None]
    sign = 1 if deltas and mean(deltas) >= 0 else -1
    out = []
    for i in dict.fromkeys(ids):
        r = by_id.get(i)
        if not r:
            continue
        rejected, act = r.get("verdict") in ("skipped", "killed"), r["actual_score"]
        if direction == "avoid":
            ok = rejected or (act is not None and mid is not None and act < mid)
        elif direction == "do":
            ok = act is not None and mid is not None and act >= mid and not rejected
        else:  # calibrate: a consistent, material miss
            ok = r["delta"] is not None and abs(r["delta"]) >= 0.15 and (r["delta"] > 0) == (sign > 0)
        if ok:
            out.append(i)
    return out


# --------------------------------------------------------------------------- lessons store


def _render(store: dict) -> str:
    lessons = store["lessons"]
    meta = store.get("basis", {})
    head = (f"# Lessons\n\n_Maintained by `harness learn` from {meta.get('measured', 0)} measured and "
            f"{meta.get('rejected', 0)} rejected actions (updated {store.get('updated_at', '-')}). "
            f"`n` = agreeing verified data points._\n")
    parts = [head]
    for lane, title in SECTIONS.items():
        items = [ls for ls in lessons if ls.get("lane", "general") == lane]
        if items:
            bullets = "\n".join(f"- {ls['text']} _(n={ls.get('support', 0)})_" for ls in items)
            parts.append(f"{title}\n{bullets}\n")
    if len(parts) == 1:
        parts.append("(no lessons yet)\n")
    return "\n".join(parts)


def _parse_md(text: str) -> list[dict]:
    out, lane = [], "general"
    for line in text.splitlines():
        if line.startswith("## "):
            low = line.lower()
            lane = "reply" if "reply" in low else "post" if "post" in low else "general"
        elif m := re.match(r"\s*[-*]\s+(.+)", line):
            body = re.sub(r"\s*_\(n=[^)]*\)_\s*$", "", m.group(1)).strip()
            if body:
                out.append({"lane": lane, "text": body})
    return out


def load_store() -> dict:
    """The structured lessons store, re-synced from the markdown if a human edited it."""
    path = lessons_store_path()
    store = {"lessons": []}
    if path.exists():
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    md = config.LESSONS_PATH.read_text(encoding="utf-8") if config.LESSONS_PATH.exists() else ""
    if md.strip() and md.strip() != _render(store).strip():  # hand-edited or legacy file: markdown wins
        known = {ls["text"]: ls for ls in store["lessons"]}
        store["lessons"] = [
            known.get(p["text"]) or {"key": _slug(p["text"]), "lane": p["lane"], "text": p["text"],
                                     "direction": "do", "evidence": [], "support": 0, "verified": False,
                                     "source": "human", "updated_at": ""}
            for p in _parse_md(md)
        ]
    return store


def merge(store: dict, new: list[dict], drop: list[str], by_id: dict[str, dict], cap: int = MAX_LESSONS) -> dict:
    """Newest lessons overwrite older ones with the same key or near-identical wording; capped."""
    now = memory.now()
    lessons = [ls for ls in store["lessons"] if ls.get("key") not in set(drop)]
    for raw in new[:MAX_NEW_PER_RUN]:
        text = re.sub(r"\s+", " ", str(raw.get("text") or "")).strip()
        if not text:
            continue
        lane = raw.get("lane") if raw.get("lane") in SECTIONS else "general"
        direction = raw.get("direction") if raw.get("direction") in ("do", "avoid", "calibrate") else "do"
        ids = [i for i in dict.fromkeys(map(str, raw.get("evidence") or [])) if i in by_id]
        key = _slug(str(raw.get("key") or text))
        old = next((ls for ls in lessons if ls["key"] == key), None) or next(
            (ls for ls in lessons if ls.get("lane") == lane and _similar(ls["text"], text)), None)
        if old:
            lessons.remove(old)
            if old.get("direction") == direction:  # same claim, refreshed: evidence accumulates
                ids = list(dict.fromkeys([*old.get("evidence", []), *ids]))
            key = old["key"]
        support = _agreeing(ids, direction, by_id)
        verified = bool(support)
        if not verified and not text.lower().startswith("(unverified)"):
            text = f"(unverified) {text}"
        lessons.insert(0, {
            "key": key, "lane": lane, "direction": direction, "text": text, "evidence": ids,
            "support": len(support), "verified": verified, "source": "learn",
            "created_at": (old or {}).get("created_at", now), "updated_at": now,
        })
    lessons.sort(key=lambda ls: (ls.get("updated_at") or "", ls.get("support", 0)), reverse=True)
    return {**store, "lessons": lessons[:cap], "updated_at": now}


def save_store(store: dict) -> str:
    md = _render(store)
    config.LESSONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.LESSONS_PATH.write_text(md, encoding="utf-8")
    lessons_store_path().write_text(json.dumps(store, indent=1), encoding="utf-8")
    return md


# --------------------------------------------------------------------------- mission proposals


def pending_proposals() -> list[dict]:
    """[{key, change, evidence: set[id]}] already in the proposals file."""
    path = proposals_path()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    out = []
    for block in re.split(r"(?m)^(?=## Proposal: )", text)[1:]:
        key = re.search(r"proposal-key: (\S+) -->", block)
        change = re.search(r"(?m)^- Proposed change: (.+)$", block)
        out.append({"key": key.group(1) if key else "", "change": change.group(1) if change else "",
                    "evidence": set(re.findall(r"(?m)^  - `([^`]+)`", block))})
    return out


def propose(proposals: list[dict], store: dict, by_id: dict[str, dict]) -> list[dict]:
    """Append sustained proposals to out/mission-proposals.md (never touches the mission). Returns new ones."""
    path = proposals_path()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    prior = pending_proposals()
    lesson_ev = {ls["key"]: ls.get("evidence", []) for ls in store["lessons"]}
    added, blocks = [], []
    for p in proposals:
        change = re.sub(r"\s+", " ", str(p.get("change") or "")).strip()
        key = _slug(str(p.get("key") or change))
        direction = p.get("direction") if p.get("direction") in ("do", "avoid") else "avoid"
        if not change or any(key == q["key"] or _similar(change, q["change"]) for q in prior):
            continue
        ids = [i for i in dict.fromkeys([*map(str, p.get("evidence") or []), *lesson_ev.get(key, [])])]
        support = _agreeing(ids, direction, by_id)
        if len(support) < SUSTAINED:
            print(f"[learn] proposal '{key}' held back: {len(support)}/{SUSTAINED} agreeing verified data points")
            continue
        if any(set(support) <= q["evidence"] for q in prior):  # same evidence, reworded: already pending
            continue
        ev = "\n".join(
            f"  - `{i}` {by_id[i]['lane']}/{by_id[i]['type']}/{by_id[i]['mind']}: "
            + (f"{by_id[i]['verdict']} ({by_id[i]['reason']})" if by_id[i].get("verdict") in ("skipped", "killed")
               else f"actual {by_id[i]['actual_score']:.2f} vs predicted {by_id[i]['predicted_score']:.2f}")
            + f" | \"{(by_id[i].get('text') or '')[:90]}\""
            for i in support
        )
        section = p.get("section") or "Do / Don't"
        blocks.append(
            f"\n## Proposal: {key} (pending human approval)\n<!-- proposal-key: {key} -->\n"
            f"- Date: {memory.now()}\n- Mission: {config.MISSION_PATH.name}, section **{section}**\n"
            f"- Direction: {direction}\n- Proposed change: {change}\n- Why: {p.get('why') or '-'}\n"
            f"- Evidence ({len(support)} agreeing data points):\n{ev}\n"
            f"- To approve: edit {config.MISSION_PATH.name} by hand. The harness never edits the mission.\n"
        )
        added.append({**p, "key": key, "change": change, "support": support})
        prior.append({"key": key, "change": change, "evidence": set(support)})
    if blocks:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = "" if existing else (
            "# Mission change proposals\n\nAppended by `harness learn` when 3+ verified data points agree. "
            "Nothing here is applied automatically.\n")
        with path.open("a", encoding="utf-8") as f:
            f.write(header + "".join(blocks))
        print(f"[learn] {len(blocks)} mission proposal(s) -> {path}")
    return added


# --------------------------------------------------------------------------- weights


def compute_weights(rows: list[dict]) -> dict:
    """Multipliers around 1.0 per mind and action type, overall and per lane.

    perf  = shrunk mean actual / overall mean actual, shrunk = (n*mean_g + 3*mean_all) / (n + 3)
    w     = clamp(perf * (1 - 0.5 * rejected / (dispatched + 2)), 0.25, 2.0)
    Without any actuals perf = 1, so only bot skips / review kills move the weights.
    """

    def block(scope: list[dict]) -> dict:
        meas = [r["actual_score"] for r in scope if r["actual_score"] is not None]
        base = mean(meas) if meas else None
        out = {}
        for dim in ("mind", "type"):
            groups = defaultdict(list)
            for r in scope:
                groups[r.get(dim) or "none"].append(r)
            out[dim] = {}
            for key, g in sorted(groups.items()):
                m = [r["actual_score"] for r in g if r["actual_score"] is not None]
                perf = ((len(m) * mean(m) + SHRINK * base) / (len(m) + SHRINK)) / base if m and base else 1.0
                disp = [r for r in g if r.get("verdict")]
                rej = sum(r["verdict"] in ("skipped", "killed") for r in disp)
                out[dim][key] = round(min(2.0, max(0.25, perf * (1 - 0.5 * rej / (len(disp) + 2)))), 2)
        return out

    return {
        "updated_at": memory.now(),
        "basis": {"measured": sum(r["actual_score"] is not None for r in rows),
                  "rejected": sum(r.get("verdict") in ("skipped", "killed") for r in rows)},
        "all": block(rows),
        "lanes": {lane: block([r for r in rows if r["lane"] == lane]) for lane in ("reply", "post")},
    }


def get_weights(lane: str | None = None) -> dict[str, dict[str, float]]:
    """{"mind": {name: w}, "type": {type: w}} for a lane (lane-specific over overall). Missing = 1.0."""
    try:
        data = json.loads(weights_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"mind": {}, "type": {}}
    lane_block = data.get("lanes", {}).get(lane, {}) if lane else {}
    return {dim: {**data.get("all", {}).get(dim, {}), **lane_block.get(dim, {})} for dim in ("mind", "type")}


def quota(lane: str, minds, base: int) -> dict[str, int]:
    """Per-mind draft cap for a lane: round(base * mind weight), at least 1."""
    w = get_weights(lane)["mind"]
    return {m: max(1, round(base * w.get(m, 1.0))) for m in minds}


# --------------------------------------------------------------------------- learn


def _mock_distill(ev: dict, cal: list[dict], by_id: dict[str, dict]) -> dict:
    """Offline stand-in for the LLM: lessons from the calibration table and skip reasons."""
    lessons, proposals = [], []
    rows = list(by_id.values())
    if not ev["verified"] and ev["best"]:
        top = ev["best"][0]
        lessons.append({"key": f"panel-favours-{top['mind']}-{top['type']}", "lane": top["lane"], "direction": "do",
                        "evidence": [e["id"] for e in ev["best"] if e["mind"] == top["mind"]],
                        "text": f"The panel rates {top['mind']} {top['type']}s highest; check this against real metrics."})
    for dim in ("mind", "type", "relationship"):
        groups = [c for c in cal if c["dim"] == dim and c["measured"] >= 2]
        if len(groups) < 2:
            continue
        hi, lo = max(groups, key=lambda c: c["actual"]), min(groups, key=lambda c: c["actual"])
        ids = lambda c: [r["id"] for r in rows if str(r.get(dim) or "none") == c["key"] and r["actual_score"] is not None]  # noqa: E731
        lessons.append({"key": f"{dim}-{hi['key']}-works", "lane": "general", "direction": "do", "evidence": ids(hi),
                        "text": f"{dim} '{hi['key']}' performs best (actual {hi['actual']:.2f} over {hi['measured']}); give it more drafts."})
        lessons.append({"key": f"{dim}-{lo['key']}-weak", "lane": "general", "direction": "avoid", "evidence": ids(lo),
                        "text": f"{dim} '{lo['key']}' underperforms (actual {lo['actual']:.2f}, predicted {lo['pred']:.2f}); cut back."})
    reasons, label = defaultdict(list), {}
    for r in ev["rejected"]:
        text = (r["reason"] or "no reason").split(";")[0].strip()
        reasons[text.lower()].append(r["id"])
        label.setdefault(text.lower(), text)
    for low, ids in reasons.items():
        reason = label[low]
        lessons.append({"key": f"bot-skips-{_slug(reason)}", "lane": "general", "direction": "avoid", "evidence": ids,
                        "text": f"The posting bot skipped {len(ids)} drafts for: {reason}. Don't ship drafts like that."})
        proposals.append({"key": f"dont-{_slug(reason)}", "section": "Don't", "direction": "avoid", "evidence": ids,
                          "change": f"Don't: {reason}.", "why": f"bot skipped {len(ids)} drafts for this"})
    worst = [c for c in cal if c["dim"] in ("mind", "type") and c["bias"] is not None and c["measured"] >= 2]
    if worst:
        c = min(worst, key=lambda c: c["bias"])
        lessons.append({"key": f"panel-overrates-{c['dim']}-{c['key']}", "lane": "general", "direction": "calibrate",
                        "evidence": [r["id"] for r in rows if str(r.get(c["dim"])) == c["key"]],
                        "text": f"The panel over-predicts {c['dim']} '{c['key']}' by {-c['bias']:.2f}; discount it."})
    for ls in lessons:
        if ls["direction"] == "avoid" and len(ls["evidence"]) >= SUSTAINED and not ls["key"].startswith("bot-skips"):
            proposals.append({"key": f"mission-{ls['key']}", "section": "Don't", "direction": "avoid",
                              "evidence": ls["evidence"], "change": ls["text"], "why": "sustained underperformance"})
    return {"lessons": lessons[:MAX_NEW_PER_RUN], "drop": [], "mission_proposals": proposals}


def learn(llm: LLM, k: int = 5, max_lessons: int = MAX_LESSONS) -> dict | None:
    """One learn pass; returns {lessons_md, lessons, proposals, weights, calibration} or None if no data."""
    rows = feedback.outcomes()
    if not rows:
        print("[learn] nothing to learn from yet: no recommended, posted or rejected actions in memory")
        return None
    by_id = {r["id"]: r for r in rows}
    cal = feedback.calibration(rows)
    ev = gather(rows, k)
    store = load_store()
    if not ev["verified"]:
        print("[learn] no actual metrics yet; learning from predicted scores (unverified) and rejections")

    cal_rows = [{k2: c[k2] for k2 in ("dim", "key", "measured", "rejected", "pred", "actual", "bias")}
                for c in cal if c["measured"] or c["rejected"]]
    prev = "\n".join(f"- [{ls['key']}] ({ls.get('lane', 'general')}) {ls['text']}" for ls in store["lessons"]) or "(none)"
    pending = "\n".join(f"- [{q['key']}] {q['change']}" for q in pending_proposals()) or "(none)"
    try:
        out = llm.complete_json(
            SYSTEM,
            PROMPT.format(mission=config.mission(), lessons=prev, calibration=json.dumps(cal_rows, indent=0),
                          best=json.dumps(ev["best"], indent=1), worst=json.dumps(ev["worst"], indent=1),
                          rejected=json.dumps(ev["rejected"], indent=1) if ev["rejected"] else "(none)",
                          pending=pending),
            mock=lambda: _mock_distill(ev, cal, by_id),
        )
    except Exception as e:  # noqa: BLE001 - a bad LLM turn shouldn't lose the weights update
        print(f"[learn] lesson distillation failed ({e}); keeping previous lessons")
        out = {}
    out = out if isinstance(out, dict) else {}

    store = merge(store, [x for x in out.get("lessons", []) if isinstance(x, dict)],
                  [str(d) for d in out.get("drop", []) or []], by_id, cap=max_lessons)
    store["basis"] = {"measured": sum(r["actual_score"] is not None for r in rows),
                      "rejected": sum(r.get("verdict") in ("skipped", "killed") for r in rows)}
    md = save_store(store)
    proposals = propose([p for p in out.get("mission_proposals", []) or [] if isinstance(p, dict)], store, by_id)

    weights = compute_weights(rows)
    path = weights_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(weights, indent=1), encoding="utf-8")

    print(f"[learn] {len(store['lessons'])} lessons -> {config.LESSONS_PATH} | weights -> {path} "
          f"({store['basis']['measured']} measured, {store['basis']['rejected']} rejected)")
    return {"lessons_md": md, "lessons": store["lessons"], "proposals": proposals, "weights": weights,
            "calibration": cal}
