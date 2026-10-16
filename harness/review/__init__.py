"""Human checkpoint: a local dashboard to approve, edit, or kill recommendations.

Every verdict rewrites out/approved-run<id>.json, the hand-off file for the posting bot.
"""

import json
import re
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import config, memory
from harness.minds.gate import verify_markers
from harness.xtext import MAX_WEIGHTED_CHARS, weighted_length

INDEX = Path(__file__).parent / "index.html"


def export_approved(run_id: int) -> Path:
    """Write the hand-off. An approved action whose text still holds a [VERIFY: ...] marker is refused:
    it goes under "refused", never under "actions", so the bot can't post an unchecked fact."""
    config.OUT_DIR.mkdir(exist_ok=True)
    path = config.OUT_DIR / f"approved-run{run_id}.json"
    ready, refused = [], []
    for a in memory.approved_actions(run_id):
        if markers := verify_markers(a["text"]):
            refused.append({"id": a["id"], "unresolved": markers})
            print(f"[review] hand-off refuses {a['id']}: resolve {', '.join(markers)} first")
        else:
            ready.append(a)
    path.write_text(json.dumps({"run_id": run_id, "account": memory.run_account(run_id), "actions": ready,
                                "refused": refused}, indent=2), encoding="utf-8")
    return path


def check_approval(action_id: str, status: str, text: str | None) -> None:
    """Refuse an approval the hand-off can't take, before the verdict is saved: an edit X would reject on
    length (drafts passed the gate's length rule; an edit hasn't), or text with a [VERIFY: ...] marker
    nobody resolved. KeyError for an unknown action."""
    if status != "approved":
        return
    if text and weighted_length(text) > MAX_WEIGHTED_CHARS:
        raise ValueError(f"edited text weighs {weighted_length(text)} as X counts it (max {MAX_WEIGHTED_CHARS})")
    if markers := verify_markers(text or memory.action_text(action_id)):
        raise ValueError(f"resolve {', '.join(markers)} before approving: check the fact and write it in, or cut it")


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
        if m := re.fullmatch(r"/api/runs/(\d+)", self.path):
            return self._json(memory.run_actions(int(m[1])))
        if m := re.fullmatch(r"/api/runs/(\d+)/approved", self.path):
            return self._json(memory.approved_actions(int(m[1])))
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        m = re.fullmatch(r"/api/actions/(\w+)/review", self.path)
        if not m:
            return self._json({"error": "not found"}, 404)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            check_approval(m[1], body.get("status", ""), body.get("text"))
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
