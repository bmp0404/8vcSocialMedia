"""Human checkpoint: a local dashboard to approve, edit, or kill recommendations.

Every verdict rewrites out/approved-run<id>.json, the hand-off file for the posting bot.
"""

import contextlib
import io
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import config, memory
from harness.evolve import evolve, leaderboard
from harness.llm import get_llm

INDEX = Path(__file__).parent / "index.html"
EVOLVE_LOCK = threading.Lock()
PROVIDER: str | None = None  # set by serve(); None = HARNESS_PROVIDER


def run_evolve(spawn: bool) -> tuple[int, dict]:
    if not EVOLVE_LOCK.acquire(blocking=False):
        return 409, {"error": "evolve is already running"}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            evolve(get_llm(PROVIDER), spawn=spawn)
        return 200, {"ok": True, "log": buf.getvalue()}
    except (Exception, SystemExit) as e:  # noqa: BLE001 - report to the page instead of crashing the server
        return 500, {"error": str(e) or type(e).__name__, "log": buf.getvalue()}
    finally:
        EVOLVE_LOCK.release()


def export_approved(run_id: int) -> Path:
    config.OUT_DIR.mkdir(exist_ok=True)
    path = config.OUT_DIR / f"approved-run{run_id}.json"
    path.write_text(
        json.dumps({"run_id": run_id, "actions": memory.approved_actions(run_id)}, indent=2),
        encoding="utf-8",
    )
    return path


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode())

    def do_GET(self) -> None:
        if self.path == "/":
            return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/runs":
            return self._json(memory.list_runs())
        if self.path == "/api/stats":
            return self._json(leaderboard())
        if m := re.fullmatch(r"/api/runs/(\d+)", self.path):
            return self._json(memory.run_actions(int(m[1])))
        if m := re.fullmatch(r"/api/runs/(\d+)/approved", self.path):
            return self._json(memory.approved_actions(int(m[1])))
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "invalid JSON"}, 400)
        if self.path == "/api/evolve":
            code, data = run_evolve(bool(body.get("spawn")))
            return self._json(data, code)
        m = re.fullmatch(r"/api/actions/(\w+)/review", self.path)
        if not m:
            return self._json({"error": "not found"}, 404)
        try:
            run_id = memory.set_review(m[1], body.get("status", ""), body.get("text"), body.get("note", ""))
        except KeyError:
            return self._json({"error": "unknown action"}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        path = export_approved(run_id)
        self._json({"ok": True, "export": str(path)})

    def log_message(self, fmt, *args) -> None:  # keep the terminal quiet
        pass


def serve(port: int = 8765, open_browser: bool = True, provider: str | None = None) -> None:
    global PROVIDER
    PROVIDER = provider
    url = f"http://127.0.0.1:{port}"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[review] dashboard at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[review] stopped")
