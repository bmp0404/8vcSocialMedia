"""Human checkpoint: a local console to approve, edit or kill recommendations, overrule what the
harness held back, and watch the loop, its experiments and its memory.

Every verdict rewrites out/approved-run<id>.json, the hand-off file for the posting bot.
The page is plain ES modules under static/ with no build step; see DESIGN.md for why.
API routes live in checkpoint.py, rounds.py and the *_api.py modules, registered through routes.py.
"""

import contextlib
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

# Importing the API modules registers their routes.
from harness.review import checkpoint, experiments_api, memory_api, rounds  # noqa: F401
# sam/integration's harness/integration/handoff.py imports export_approved from here.
from harness.review.checkpoint import export_approved  # noqa: F401
from harness.review.routes import HTTPError, Request, path_exists, resolve

HERE = Path(__file__).parent
INDEX = HERE / "index.html"
STATIC = (HERE / "static").resolve()
TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
}
MAX_BODY = 1_000_000


class Handler(BaseHTTPRequestHandler):
    port = 8765  # set by serve()

    def _send(self, code: int, body: bytes, ctype: str = "application/json", cache: str = "no-cache") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        # No other page may frame the console and steer clicks onto Clear.
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
        """Why a POST is refused, or None. Other sites can't send JSON here without a CORS
        preflight, which this server never answers, so requiring JSON plus a local Origin keeps
        a web page from approving drafts or starting paid rounds behind the operator's back."""
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return "POST bodies must be application/json"
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}:
            return f"cross-origin request from {origin} refused"
        return None

    def _static(self, rel: str) -> None:
        target = (STATIC / rel).resolve()
        if not target.is_relative_to(STATIC) or not target.is_file():
            return self._json({"error": "not found"}, 404)
        # Fonts never change; everything else revalidates so edits show up on reload.
        cache = "public, max-age=31536000, immutable" if target.suffix == ".woff2" else "no-cache"
        self._send(200, target.read_bytes(), TYPES.get(target.suffix, "application/octet-stream"), cache)

    def _dispatch(self, method: str) -> None:
        if not self._local_host():
            return self._json({"error": "unexpected Host header"}, 421)
        url = urlsplit(self.path)
        path = url.path
        if method == "GET" and path == "/favicon.ico":
            return self._static("favicon.svg")
        if method == "GET" and path in ("/", "/index.html"):
            return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        if method == "GET" and path.startswith("/static/"):
            return self._static(path.removeprefix("/static/"))
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
    Handler.port = port
    url = f"http://127.0.0.1:{port}"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[review] dashboard at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # A round runs in its own process group, so Ctrl+C here doesn't reach it.
        with contextlib.suppress(HTTPError):
            rounds.RUNNER.stop()
        print("\n[review] stopped")
