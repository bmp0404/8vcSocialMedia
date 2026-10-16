"""The header's one line: when the last round ran, and whether the watcher is running."""

import subprocess

from harness import config, memory
from harness.review.routes import Request, route
from harness.sources.watch import account_handle


def watcher() -> str:
    """"running", "stopped", or "unknown" where `ps` isn't available. The pid file is the one
    `harness watch` writes, and the command-line check is the one its own status command uses."""
    pid_file = config.OUT_DIR / "watch" / "watch.pid"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return "stopped"
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
    command = out.stdout.strip()
    return "running" if out.returncode == 0 and "harness" in command and "watch" in command else "stopped"


def last_round() -> dict | None:
    """The newest run that was a round. Hot targets get runs of their own (hot.py), which aren't rounds."""
    with memory.connect() as conn:
        row = conn.execute("SELECT id, started_at, provider FROM runs WHERE COALESCE(sources, '') != 'watch' "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


@route("GET", r"/api/status")
def get_status(req: Request):
    return {"last_round": last_round(), "watcher": watcher(), "account": account_handle()[0] or None}
