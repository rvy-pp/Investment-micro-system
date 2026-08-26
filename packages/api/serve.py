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
sys.path.insert(0, str(REPO / "packages" / "core"))
from code_fingerprint import fingerprint  # noqa: E402

# CAPTURED ONCE, AT IMPORT. That is the point — it records the code as it was when
# this process loaded it, so /api/version can be compared against the same
# function run fresh against disk. Computing it per request would make the two
# always agree and the guard useless.
BOOT_FINGERPRINT = fingerprint(REPO)

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
                # Driven off engine.SECTORS rather than a literal pair of
                # groups. The two were hardcoded here, so adding steel to
                # SECTORS made it appear in the nav and stay absent from
                # /api/scores — the tab would render, the Bridge view would be
                # empty, and nothing would say why.
                return self._json({
                    pg: engine.compute(pg, w)
                    for s in engine.SECTORS for pg in s["peer_groups"]
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
                # `groups` is a comma-separated peer_group list. Absent, the
                # tape returns every scored name — which is what made the Pair
                # tab identical on every sector until 2026-08-26.
                gs = [x for x in (q.get("groups", [""])[0] or "").split(",") if x]
                return self._json(tape_mod.tape(p, since, gs or None))
            if u.path == "/api/inputs":
                return self._json(engine.inputs_for_ui())
            if u.path == "/api/oi":
                return self._json(engine.oi_snapshot())
            if u.path == "/api/sectors":
                return self._json(engine.sector_list())
            if u.path == "/api/nav":
                return self._json(engine.nav_list())
            if u.path == "/api/overview":
                return self._json(engine.overview())
            if u.path == "/api/flows":
                return self._json(engine.flows())
            if u.path == "/api/sector":
                sid = q.get("id", [""])[0]
                d = engine.sector_detail(sid)
                return self._json(d, 404 if d.get("error") else 200)
            if u.path == "/api/version":
                # What this PROCESS loaded, versus what is on disk right now.
                # build_frontend compares them; a mismatch means restart.
                now = fingerprint(REPO)
                return self._json({
                    "boot": BOOT_FINGERPRINT,
                    "disk": now,
                    "stale": now["newest_mtime"] > BOOT_FINGERPRINT["newest_mtime"],
                })
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
