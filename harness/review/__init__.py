"""Human checkpoint: a local dashboard to approve, edit, or kill recommendations.

Every verdict rewrites out/approved-run<id>.json, the hand-off file for the posting bot.
"""

import json
import re
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import config, memory

INDEX = Path(__file__).parent / "index.html"


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
        from harness.review import console  # observability console at /console, kept out of this page

        if hit := console.get(self.path):
            return self._send(*hit)
        if self.path == "/":
            return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/runs":
            return self._json(memory.list_runs())
        if m := re.fullmatch(r"/api/runs/(\d+)", self.path):
            return self._json(memory.run_actions(int(m[1])))
        if m := re.fullmatch(r"/api/runs/(\d+)/approved", self.path):
            return self._json(memory.approved_actions(int(m[1])))
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        from harness.review import console

        if self.path.startswith("/api/console/"):
            # JSON only: a cross-site form can't send it without a CORS preflight, so other pages can't start runs.
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json({"error": "expected application/json"}, 415)
            body =json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            code, data = console.post(self.path, body) or (404, {"error": "not found"})
            return self._json(data, code)
        m = re.fullmatch(r"/api/actions/(\w+)/review", self.path)
        if not m:
            return self._json({"error": "not found"}, 404)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            run_id = memory.set_review(m[1], body.get("status", ""), body.get("text"), body.get("note", ""))
        except KeyError:
            return self._json({"error": "unknown action"}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        path = export_approved(run_id)
        self._json({"ok": True, "export": str(path)})

    def log_message(self, fmt, *args) -> None:  # keep the terminal quiet
        pass


def serve(port: int = 8765, open_browser: bool = True) -> None:
    url = f"http://127.0.0.1:{port}"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[review] dashboard at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[review] stopped")
