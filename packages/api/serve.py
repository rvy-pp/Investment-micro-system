"""Local API + static server. stdlib only — no install, no hosting.

    python packages/api/serve.py
    -> http://127.0.0.1:8765

Binds to 127.0.0.1 deliberately: this serves an editable view of the book's
research model and has no auth, so it must not be reachable off the machine.
"""

from __future__ import annotations

import json
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
WEB = REPO / "packages" / "web"
sys.path.insert(0, str(REPO / "packages" / "api"))

import engine  # noqa: E402
import tape as tape_mod  # noqa: E402

# 8765 is taken by the vault's existing node dashboard. Sharing a port does not
# error visibly — the other server just answers, and every request 404s as if
# the routes were wrong. Hence a distinct port AND a loud bind check below.
HOST, PORT = "127.0.0.1", 8770


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(),
                   "application/json; charset=utf-8")

    def log_message(self, fmt, *args):
        pass                      # quiet; the console is for real output

    # ---------------- GET ----------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                html = (WEB / "app.html").read_bytes()
                return self._send(200, html, "text/html; charset=utf-8")

            if u.path == "/api/scores":
                w = int(q.get("window", ["30"])[0])
                return self._json({
                    "aluminium_primary": engine.compute("aluminium_primary", w),
                    "zinc": engine.compute("zinc", w),
                })
            if u.path == "/api/tape":
                # The PERSISTED tape. /api/scores recomputes the bridge live with
                # overrides applied; this one reads pillar_scores untouched. They
                # will disagree whenever an override is active or run_scores.py is
                # stale, and that is correct — the pair chart must show numbers
                # that were actually stored, or it describes a different system
                # from the one the review layer grades.
                p = q.get("pillar", ["composite"])[0]
                since = (q.get("since", [""])[0] or None)
                if p not in tape_mod.PILLARS:
                    return self._json(
                        {"error": f"unknown pillar {p!r}; "
                                  f"expected one of {tape_mod.PILLARS}"}, 400)
                return self._json(tape_mod.tape(p, since))
            if u.path == "/api/inputs":
                return self._json(engine.inputs_for_ui())
            if u.path == "/api/oi":
                return self._json(engine.oi_snapshot())
            if u.path == "/api/guidance":
                return self._json(engine.guidance_rows())
            return self._json({"error": "not found"}, 404)
        except Exception as exc:                    # surface, do not swallow
            import traceback
            traceback.print_exc()
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------------- POST ----------------
    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)

        try:
            if u.path == "/api/override":
                rid = engine.set_override(
                    body["entity_id"], body["scope"], body.get("item"),
                    body["field"], float(body["value"]),
                    body.get("note") or "", body.get("prev"))
                return self._json({"ok": True, "id": rid})
            if u.path == "/api/override/clear":
                engine.clear_override(int(body["id"]))
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


if __name__ == "__main__":
    import argparse
    import socket

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    try:
        srv = ThreadingHTTPServer((HOST, args.port), Handler)
    except OSError as exc:
        # Fail loudly. A silent bind failure here is genuinely dangerous: another
        # server keeps answering on the port and every request 404s, which reads
        # as broken routing rather than "this is not my server".
        print(f"cannot bind {HOST}:{args.port} — {exc}", file=sys.stderr)
        print(f"something else is already listening. Find it with:\n"
              f"  Get-NetTCPConnection -LocalPort {args.port} -State Listen\n"
              f"then re-run with --port <free port>.", file=sys.stderr)
        raise SystemExit(1)

    print(f"Investment Micro-System  ->  http://{HOST}:{args.port}")
    print("ctrl-c to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
