"""Feedback: ingest what actually happened (bot reply + X metrics) and compare it with Eval's prediction.

Ingest
  ingest_bot_reply(text)       parse the posting bot's reply email, mark actions posted / rejected
  ingest_metrics(source)       fill actual metrics from X ("x") or a scraper's JSON/CSV file

Predicted vs. actual
  actual_score(row)            0-1 score from real metrics (formula below)
  outcomes()                   every action with its outcome, actual_score and delta vs. predicted
  calibration(rows)            mean predicted / actual / bias / MAE by lane, mind, type, relationship, panelist
  format_calibration(cal)      a small text table

ACTUAL SCORE (0-1)
  W = likes + 2*reposts + 3*replies + 3*quotes + bookmarks       (replies/quotes are conversation, weigh most)
  impressions >= 100:   score = 1 - exp(-(W / impressions) / 0.025)
                        (2.5% weighted engagement rate -> 0.63, 5% -> 0.86, 1% -> 0.33)
  otherwise (no/low impressions):   score = min(1, log(1 + W) / log(1 + 50))      (W = 50 -> 1.0)
  follows and unmeasured actions have no actual score (None).
  delta = actual_score - predicted_score   (negative = the panel over-predicted)

Bot outcomes (incl. skip reasons) live in this module's own `bot_outcomes` table in the harness DB,
so nothing in memory.py's schema has to change.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from statistics import mean

from harness import config, memory

MIN_IMPRESSIONS = 100
RATE_SCALE = 0.025
LOG_REF = 50
WEIGHTS = {"likes": 1, "reposts": 2, "replies": 3, "quotes": 3, "bookmarks": 1}

OUTCOME_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_outcomes (
    action_id TEXT PRIMARY KEY,
    outcome TEXT,          -- posted | followed | skipped
    post_id TEXT,
    post_url TEXT,
    reason TEXT,
    raw_line TEXT,
    received_at TEXT
);
"""
# `actions.reason` holds a structured code (models.REASONS); the bot's free text stays in bot_outcomes.
REASON_COLUMNS = ("reason", "reject_reason", "rejection_reason", "skip_reason")
REASON_KEYWORDS = [  # first match wins
    ("factual_risk", r"verify|placeholder|fact|unverif|claim|inaccura|stat|false|made.up|invent|wrong number"),
    ("stale", r"stale|too old|outdated|expired|too late|hours? ago|days? old|old thread|moved on"),
    ("wrong_target", r"wrong (target|account|tweet|post|person)|deleted|not found|protected|private|can.?t reply|locked"),
    ("repetitive", r"repeat|duplicate|already (posted|said|replied)|same as|again"),
    ("nothing_to_add", r"nothing to add|adds nothing|low.value|generic|filler|pointless|no value"),
    ("off_tone", r"tone|voice|marketing|sales|promo|spam|cringe|spicy|rude|off.brand|plug|pitch|hype"),
]


def reason_code(text: str | None) -> str:
    """Map a free-text skip reason onto models.REASONS (falls back to 'other')."""
    t = (text or "").lower()
    try:
        from harness.models import REASONS
    except ImportError:
        REASONS = tuple(code for code, _ in REASON_KEYWORDS) + ("other",)
    if t.strip().replace(" ", "_") in REASONS:
        return t.strip().replace(" ", "_")
    return next((code for code, rx in REASON_KEYWORDS if re.search(rx, t) and code in REASONS), "other")

STATUS_URL = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+|i(?:/web)?)/status(?:es)?/(\d+)", re.I)
REPLY_LINE = re.compile(r"action[\s_-]*id\s*[:#]?\s*([A-Za-z0-9_-]{3,40})\s*\|\s*(.*)$", re.I)


def connect():
    conn = memory.connect()
    conn.executescript(OUTCOME_SCHEMA)
    return conn


def _columns(conn, table: str = "actions") -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def post_id_from(value: str | None) -> str | None:
    """Tweet id from a status URL or a bare numeric id."""
    if not value:
        return None
    value = str(value).strip()
    if m := STATUS_URL.search(value):
        return m.group(2)
    return value if re.fullmatch(r"\d{6,25}", value) else None


# --------------------------------------------------------------------------- bot reply


def _clean_lines(text: str) -> list[tuple[int, str]]:
    """(quote depth, cleaned line) for every line of an email body."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").replace("​", "")
    if re.search(r"</(div|p|blockquote)>|<br\s*/?>", text, re.I):  # HTML-only reply
        text = re.sub(r"<br\s*/?>|</(div|p|li|blockquote)>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    out = []
    for raw in text.split("\n"):
        m = re.match(r"^\s*((?:>\s*)*)", raw)
        depth = m.group(1).count(">")
        line = raw[m.end():]
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line)  # bullets / numbering
        line = line.replace("`", "").replace("**", "").strip()
        out.append((depth, re.sub(r"\s+", " ", line)))
    return out


def parse_bot_reply(text: str) -> list[dict]:
    """Parse `ACTION_ID <id> | <tweet URL>` / `| followed` / `| skipped | <reason>` lines.

    Tolerates `>` quoting, bullets, backticks, odd spacing and case. If an id appears more than
    once (e.g. an older reply quoted below), the least-quoted line wins. Returns
    [{action_id, outcome: posted|followed|skipped|unknown, post_id, post_url, reason, raw_line}].
    """
    best: dict[str, tuple[int, dict]] = {}
    for depth, line in _clean_lines(text):
        m = REPLY_LINE.search(line)
        if not m:
            continue
        aid, rest = m.group(1), m.group(2).strip()
        parts = [p.strip() for p in rest.split("|")]
        head = parts[0].lower() if parts else ""
        rec = {"action_id": aid, "outcome": "unknown", "post_id": None, "post_url": None, "reason": None,
               "raw_line": line}
        if url := STATUS_URL.search(rest):
            rec.update(outcome="posted", post_url=url.group(0), post_id=url.group(2))
        elif re.fullmatch(r"\d{6,25}", head):
            rec.update(outcome="posted", post_id=head)
        elif head.startswith("follow"):
            rec["outcome"] = "followed"
        elif re.match(r"(skip|reject|declin|not posted|pass)", head):
            rec.update(outcome="skipped", reason=" | ".join(parts[1:]).strip() or None)
        elif head:
            rec["reason"] = rest
        if aid not in best or depth < best[aid][0]:
            best[aid] = (depth, rec)
    return [rec for _, rec in best.values()]


def ingest_bot_reply(text: str) -> list[dict]:
    """Apply the bot's reply to memory: posted/followed -> `posted` (+ post_id/post_url),
    skipped -> `rejected` (+ reason). Each returned record gets `applied: bool`."""
    recs = parse_bot_reply(text)
    with connect() as conn:
        cols = _columns(conn)
        reason_col = next((c for c in REASON_COLUMNS if c in cols), None)
        for r in recs:
            row = conn.execute("SELECT id FROM actions WHERE id = ? OR lower(id) = lower(?)",
                               (r["action_id"], r["action_id"])).fetchone()
            r["applied"] = bool(row) and r["outcome"] != "unknown"
            if not r["applied"]:
                why = "unknown action id" if not row else "unrecognised outcome"
                print(f"[feedback] {why}: {r['raw_line']}")
                continue
            r["action_id"] = aid = row["id"]
            if r["outcome"] in {"posted", "followed"}:
                conn.execute(
                    "UPDATE actions SET status = 'posted', post_id = COALESCE(?, post_id), "
                    "post_url = COALESCE(?, post_url) WHERE id = ?",
                    (r["post_id"], r["post_url"], aid),
                )
            else:
                conn.execute("UPDATE actions SET status = 'rejected' WHERE id = ?", (aid,))
                r["reason_code"] = reason_code(r["reason"])
                if reason_col:
                    value = r["reason_code"] if reason_col == "reason" else r["reason"]
                    conn.execute(f"UPDATE actions SET {reason_col} = ? WHERE id = ?", (value, aid))
            conn.execute(
                "INSERT OR REPLACE INTO bot_outcomes VALUES (?, ?, ?, ?, ?, ?, ?)",
                (aid, r["outcome"], r["post_id"], r["post_url"], r["reason"], r["raw_line"], memory.now()),
            )
    n = sum(r["applied"] for r in recs)
    print(f"[feedback] bot reply: {n}/{len(recs)} lines applied "
          f"({sum(r['outcome'] == 'skipped' and r['applied'] for r in recs)} skipped)")
    return recs


# --------------------------------------------------------------------------- metrics


def _count(v) -> int | None:
    """'1,234' / '1.2K' / '3M' / 7 / '' -> int or None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "").upper()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMB]?)", s)
    if not m:
        return None
    return int(round(float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)]))


def _load_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            return [dict(r) for r in csv.DictReader(f)]
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("data", "posts", "tweets", "results", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        # {post_id: metrics} (fetch_metrics shape)
        return [{"post_id": k, **(v if isinstance(v, dict) else {})} for k, v in data.items()]
    return list(data)


def _metrics(row: dict) -> dict:
    flat = {}
    for src in (row, row.get("public_metrics") or {}, row.get("metrics") or {}):
        if isinstance(src, dict):
            for k, v in src.items():
                if not isinstance(v, (dict, list)) and (c := _count(v)) is not None:
                    flat[str(k).lower().strip()] = c
    return {k: v for k, v in flat.items() if any(k in keys for keys in memory.METRIC_COLUMNS.values())}


def _norm(text: str) -> str:
    return re.sub(r"https?://\S+|\W+", " ", (text or "").lower()).strip()


def ingest_metrics(source: str | Path = "x", handle: str | None = None) -> dict:
    """Fill actual metrics for posted actions.

    source="x": X API `fetch_metrics` for every posted action with a post_id. With `handle` (the brand's
      X handle), posted actions missing a post_id are first matched to the account's own posts by text.
    source=<path.json|path.csv>: rows of {post_id | url | action_id [, text], likes, reposts, replies,
      quotes, bookmarks, impressions}; X `public_metrics` names, nested `public_metrics`, and scraper-style
      counts ("1.2K") are fine. Rows without an id are matched by tweet text.
    Returns {"updated": n, "unmatched": [rows]}.
    """
    with connect() as conn:
        actions = [dict(r) for r in conn.execute(
            "SELECT id, post_id, post_url, text, status, type FROM actions WHERE type != 'follow'")]
    by_post = {a["post_id"]: a for a in actions if a.get("post_id")}
    by_id = {a["id"]: a for a in actions}
    by_text = {_norm(a["text"]): a for a in actions if a.get("text") and a["status"] in ("posted", "recommended")}

    rows: list[dict] = []
    if str(source).lower() == "x":
        from harness.sources import x_api

        try:
            if handle:
                missing = [a for a in actions if a["status"] == "posted" and not a.get("post_id")]
                if missing:
                    for p in x_api.fetch_own_posts(handle):
                        a = by_text.get(_norm(p["text"]))
                        if a and not a.get("post_id"):
                            a["post_id"], a["post_url"] = p["id"], p["url"]
                            by_post[p["id"]] = a
            ids = [pid for pid, a in by_post.items() if a["status"] == "posted"]
            if not ids:
                print("[feedback] no posted actions with a post_id to measure")
                return {"updated": 0, "unmatched": []}
            fetched = x_api.fetch_metrics(ids)
        except x_api.XAPIError as e:
            print(f"[feedback] X API failed: {e}")
            return {"updated": 0, "unmatched": [], "error": str(e)}
        rows = [{"post_id": pid, **pm} for pid, pm in fetched.items()]
        for pid in set(ids) - set(fetched):
            print(f"[feedback] X returned nothing for post {pid} (deleted/protected?)")
    else:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(path)
        rows = _load_rows(path)

    outcomes, unmatched = [], []
    for row in rows:
        row = {str(k).lower().strip(): v for k, v in row.items()} if isinstance(row, dict) else {}
        pid = next((post_id_from(row.get(k)) for k in ("post_id", "tweet_id", "id_str", "id", "url", "post_url",
                                                         "tweet_url", "link", "permalink") if post_id_from(row.get(k))),
                   None)
        a = (by_id.get(str(row.get("action_id") or "")) or by_post.get(pid)
             or by_text.get(_norm(row.get("text") or row.get("full_text") or "")) if row else None)
        m = _metrics(row)
        if not a or not m:
            unmatched.append(row)
            continue
        url = next((row[k] for k in ("url", "post_url", "tweet_url", "link") if row.get(k)), None)
        pid = pid or a.get("post_id")
        outcomes.append({"action_id": a["id"], "metrics": m, "post_id": pid, "status": "posted",
                         "post_url": url or a.get("post_url") or (f"https://x.com/i/status/{pid}" if pid else None)})
    n = memory.record_outcomes(outcomes) if outcomes else 0
    print(f"[feedback] metrics: updated {n} actions from {source} ({len(unmatched)} rows unmatched)")
    return {"updated": n, "unmatched": unmatched}


# --------------------------------------------------------------------------- predicted vs. actual


def actual_score(row: dict) -> float | None:
    """0-1 from real metrics; see module docstring. None if unmeasured or a follow."""
    if row.get("type") == "follow":
        return None
    vals = {k: row.get(f"actual_{k}") for k in (*WEIGHTS, "impressions")}
    if all(vals[k] is None for k in ("likes", "reposts", "replies", "quotes", "impressions")):
        return None
    w = sum(WEIGHTS[k] * (vals[k] or 0) for k in WEIGHTS)
    imp = vals["impressions"] or 0
    if imp >= MIN_IMPRESSIONS:
        return round(1 - math.exp(-(w / imp) / RATE_SCALE), 3)
    return round(min(1.0, math.log1p(w) / math.log1p(LOG_REF)), 3)


def targets() -> dict[str, str]:
    """{handle (lower): relationship} from the mission's `<stem>_targets.json`, if present."""
    path = config.MISSION_PATH.with_name(f"{config.MISSION_PATH.stem}_targets.json")
    try:
        accounts = json.loads(path.read_text(encoding="utf-8")).get("accounts", [])
    except (OSError, ValueError):
        return {}
    out = {}
    for acc in accounts:
        for h in [acc.get("handle"), *(acc.get("aliases") or [])]:
            if h:
                out[h.lstrip("@").lower()] = acc.get("relationship") or "tracked"
    return out


def outcomes(limit: int = 1000) -> list[dict]:
    """Every action that reached the bot or got an outcome, with actual_score, delta and verdict."""
    rel = targets()
    with connect() as conn:
        has_reviews = "status" in _columns(conn, "reviews")
        rows = conn.execute(
            f"""SELECT a.*, b.outcome AS bot_outcome, b.reason AS bot_reason
                {", rv.status AS review_status, rv.note AS review_note" if has_reviews else ""}
                FROM actions a LEFT JOIN bot_outcomes b ON b.action_id = a.id
                {"LEFT JOIN reviews rv ON rv.action_id = a.id" if has_reviews else ""}
                WHERE a.status = 'posted' OR a.rank IS NOT NULL OR b.action_id IS NOT NULL
                {"OR rv.status = 'killed'" if has_reviews else ""}
                ORDER BY a.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    out = []
    for r in map(dict, rows):
        for k in ("context", "panel"):
            try:
                r[k] = json.loads(r[k]) if r.get(k) else {}
            except ValueError:
                r[k] = {}
        handle = (r.get("target_handle") or "").lstrip("@").lower()
        r["relationship"] = rel.get(handle, "untracked") if handle else "none"
        r["actual_score"] = actual_score(r)
        p = r.get("predicted_score")
        r["reason_code"] = r.get("reason")  # structured column, if present; free text goes in `reason`
        r["delta"] = round(r["actual_score"] - p, 3) if r["actual_score"] is not None and p is not None else None
        if r.get("bot_outcome") == "skipped":
            r["verdict"], r["reason"] = "skipped", r.get("bot_reason") or r["reason_code"] or ""
            r["reason_code"] = r["reason_code"] or reason_code(r["reason"])
        elif r.get("review_status") == "killed":
            r["verdict"], r["reason"] = "killed", r.get("review_note") or r["reason_code"] or ""
        else:
            r["verdict"] = r.get("bot_outcome") or ("posted" if r.get("status") == "posted" else None)
            r["reason"] = None
        r["metrics"] = {k: r.get(f"actual_{k}") for k in (*WEIGHTS, "impressions") if r.get(f"actual_{k}") is not None}
        out.append(r)
    return out


def calibration(rows: list[dict] | None = None, dims=("lane", "mind", "type", "relationship")) -> list[dict]:
    """[{dim, key, n, measured, rejected, pred, actual, bias, mae}] with an `all` row first.

    bias = mean(actual - predicted) over measured actions (negative = over-predicted).
    `panelist` rows compare each simulated panelist's score with the actual score.
    """
    rows = outcomes() if rows is None else rows

    def stats(dim: str, key: str, group: list[dict], pred_of=lambda r: r.get("predicted_score")) -> dict:
        meas = [r for r in group if r["actual_score"] is not None and pred_of(r) is not None]
        deltas = [r["actual_score"] - pred_of(r) for r in meas]
        return {
            "dim": dim, "key": key, "n": len(group), "measured": len(meas),
            "rejected": sum(r.get("verdict") in ("skipped", "killed") for r in group),
            "pred": round(mean(pred_of(r) for r in meas), 3) if meas else None,
            "actual": round(mean(r["actual_score"] for r in meas), 3) if meas else None,
            "bias": round(mean(deltas), 3) if meas else None,
            "mae": round(mean(abs(d) for d in deltas), 3) if meas else None,
        }

    out = [stats("all", "all", rows)]
    for dim in dims:
        groups = defaultdict(list)
        for r in rows:
            groups[str(r.get(dim) or "none")].append(r)
        out += [stats(dim, k, g) for k, g in sorted(groups.items())]
    panelists = sorted({p for r in rows for p in (r.get("panel") or {})})
    for p in panelists:
        group = [r for r in rows if p in (r.get("panel") or {})]
        out.append(stats("panelist", p, group, pred_of=lambda r, p=p: r["panel"].get(p)))
    return out


def format_calibration(cal: list[dict]) -> str:
    def f(v, sign: bool = False) -> str:
        return "-" if v is None else f"{v:+.2f}" if sign else f"{v:.2f}"

    lines = [f"{'group':<30} {'n':>3} {'meas':>4} {'rej':>3}  {'pred':>6} {'actual':>6} {'bias':>6} {'mae':>6}"]
    for c in cal:
        label = "all" if c["dim"] == "all" else f"{c['dim']}={c['key']}"
        lines.append(f"{label[:30]:<30} {c['n']:>3} {c['measured']:>4} {c['rejected']:>3}  "
                     f"{f(c['pred']):>6} {f(c['actual']):>6} {f(c['bias'], True):>6} {f(c['mae']):>6}")
    return "\n".join(lines)
