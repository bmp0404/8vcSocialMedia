"""What predicts a reply's likes in action/fixtures/x_threads.json: timing, the replier's size,
or the text's length? No LLM involved.

Correlations are Spearman, computed inside each thread and then averaged, so a thread with a
huge root doesn't dominate. The fixture holds X's first page of replies, which X ranks by
engagement, so late low-like replies are under-sampled and every number here is conditional on
a reply already making the visible page.

    uv run python -m harness.minds.lab.reply_dynamics
"""

import json
import statistics

from harness.minds.lab.fetch_x_threads import OUT as FIXTURE
from harness import config

REPORT = config.ROOT / "action" / "experiments" / "reply_dynamics.json"


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 5 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    var = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return cov / var if var else None


def main() -> None:
    threads = json.loads(FIXTURE.read_text(encoding="utf-8"))["threads"]
    per_thread = {"minutes_after_root": [], "replier_followers": [], "chars": []}
    top3_minutes, rest_minutes = [], []
    small_top, all_top = 0, 0
    for t in threads:
        rs = [r for r in t["direct_replies"] if r["replier_followers"] is not None and r["minutes_after_root"] >= 0]
        likes = [r["likes"] for r in rs]
        for feat, xs in (
            ("minutes_after_root", [r["minutes_after_root"] for r in rs]),
            ("replier_followers", [r["replier_followers"] for r in rs]),
            ("chars", [len(r["text"]) for r in rs]),
        ):
            rho = spearman(xs, likes)
            if rho is not None:
                per_thread[feat].append(rho)
        ranked = sorted(rs, key=lambda r: r["likes"], reverse=True)
        top3_minutes += [r["minutes_after_root"] for r in ranked[:3]]
        rest_minutes += [r["minutes_after_root"] for r in ranked[3:]]
        for r in ranked[:3]:
            all_top += 1
            small_top += r["replier_followers"] < 1000

    report = {
        "threads": len(threads),
        "direct_replies": sum(len(t["direct_replies"]) for t in threads),
        "mean_within_thread_spearman_with_likes": {
            k: {"mean": round(statistics.fmean(v), 3), "threads": len(v)} for k, v in per_thread.items() if v
        },
        "median_minutes_after_root": {
            "top3_replies": statistics.median(top3_minutes),
            "other_replies": statistics.median(rest_minutes),
        },
        "top3_replies_from_accounts_under_1000_followers": f"{small_top}/{all_top}",
        "caveat": "fxtwitter returns X's first, engagement-ranked page of replies; late low-like replies are under-sampled.",
    }
    REPORT.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
