"""Human checkpoint: a local page to decide on drafts, see how past verdicts did, and audit labels.

Every verdict rewrites out/approved-run<id>.json, the hand-off file for the posting bot.
The page is a React build committed under dist/, so `python -m harness review` needs no Node; its
source is web/. API routes live in queue.py, tweets.py, history.py, audit.py and status.py,
registered through routes.py. DESIGN.md says what each view is for.
"""

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from harness import config, memory
from harness.minds.gate import verify_markers
from harness.review.routes import HTTPError, Request, path_exists, resolve
from harness.xtext import MAX_WEIGHTED_CHARS, weighted_length

DIST = (Path(__file__).parent / "dist").resolve()
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
}
MAX_BODY = 1_000_000


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
    # Write then rename, so a reader such as `outcomes sync` never sees half a file.
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps({"run_id": run_id, "account": memory.run_account(run_id), "actions": ready,
                               "refused": refused}, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def check_approval(action_id: str, status: str, text: str | None) -> None:
    """Refuse an approval the hand-off can't take, before the verdict is saved: an edit X would reject on
    length (drafts passed the gate's length rule; an edit hasn't), or text with a [VERIFY: ...] marker
    nobody resolved. KeyError for an unknown action."""
    if status == "approved":
        check_words(text or memory.action_text(action_id), edited=bool(text))


def check_words(words: str, edited: bool) -> None:
    """check_approval's rules for the words that would post. Only an edit is checked for length."""
    if edited and weighted_length(words) > MAX_WEIGHTED_CHARS:
        raise ValueError(f"edited text weighs {weighted_length(words)} as X counts it (max {MAX_WEIGHTED_CHARS})")
    if markers := verify_markers(words):
        raise ValueError(f"resolve {', '.join(markers)} before approving: check the fact and write it in, or cut it")


class Handler(BaseHTTPRequestHandler):
    port = 8765  # set by serve()

    def _send(self, code: int, body: bytes, ctype: str = "application/json", cache: str = "no-cache") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        # No other page may frame this one and steer clicks onto Approve.
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode())

    def _local_host(self) -> bool:
        """Refuse requests addressed to any other host name, which is what DNS rebinding needs."""
        return self.headers.get("Host", "") in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def _same_origin_post(self) -> str | None:
        """Why a POST is refused, or None. Other sites can't send JSON here without a CORS preflight,
        which this server never answers, so requiring JSON plus a local Origin keeps a web page from
        approving drafts behind the operator's back."""
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return "POST bodies must be application/json"
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}:
            return f"cross-origin request from {origin} refused"
        return None

    def _file(self, rel: str) -> None:
        target = (DIST / rel).resolve()
        if not target.is_relative_to(DIST) or not target.is_file():
            return self._json({"error": "not found"}, 404)
        # Vite puts a content hash in every asset name, so an asset never changes under its name.
        cache = "public, max-age=31536000, immutable" if target.parent.name == "assets" else "no-cache"
        self._send(200, target.read_bytes(), TYPES.get(target.suffix, "application/octet-stream"), cache)

    def _dispatch(self, method: str) -> None:
        if not self._local_host():
            return self._json({"error": "unexpected Host header"}, 421)
        url = urlsplit(self.path)
        path = unquote(url.path)
        if method == "GET" and not path.startswith("/api/"):
            if path.startswith("/assets/") or path == "/favicon.svg":
                return self._file(path.lstrip("/"))
            # Every other page path is the app; it reads the view from the URL.
            return self._file("index.html")
        body = None
        if method == "POST":
            if why := self._same_origin_post():
                return self._json({"error": why}, 403)
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_BODY:
                return self._json({"error": "request body too large"}, 413)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "invalid JSON"}, 400)
            if not isinstance(body, dict):
                return self._json({"error": "the JSON body must be an object"}, 400)
        r, m = resolve(method, path)
        if not r:
            if path_exists(path):
                return self._json({"error": "method not allowed"}, 405)
            return self._json({"error": "not found"}, 404)
        try:
            data = r.fn(Request(method, path, m, dict(parse_qsl(url.query)), body))
        except HTTPError as e:
            return self._json({"error": e.message}, e.code)
        except Exception as e:  # noqa: BLE001 - the page shows the error instead of a dead request
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        self._json(data)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, fmt, *args) -> None:  # keep the terminal quiet
        pass


def serve(port: int = 8765, open_browser: bool = True) -> None:
    if not (DIST / "index.html").is_file():
        raise SystemExit(f"[review] no page at {DIST / 'index.html'}; build it with `pnpm --dir harness/review/web build`")
    Handler.port = port
    url = f"http://127.0.0.1:{port}"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[review] dashboard at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[review] stopped")


# Importing the API modules registers their routes. They import export_approved and check_approval
# from this module, so they come after those definitions.
from harness.review import audit, history, queue, status, tweets  # noqa: E402,F401
