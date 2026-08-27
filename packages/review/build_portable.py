#!/usr/bin/env python3
"""
Build a SELF-CONTAINED copy of the live front end, for viewing on another device.

WHY THIS EXISTS
---------------
`packages/web/app.html` is a CLIENT, not a page. Every tab draws itself by
calling `/api/...` on `packages/api/serve.py`, which reads `data/ims.db`.
Copying that file to OneDrive gets you the shell and nothing in it -- no
scores, no OI table, no charts -- because there is no server on the far side.

So this script does what a copy cannot: it plays the part of the browser
ONCE, here, where the server exists. It walks every URL the page can ask
for, keeps the answers, and welds them into the HTML. The result opens from
a file:// path on a phone, a laptop, a machine with no Python at all.

IT DOES NOT FORK THE FRONT END. The page is read from disk on every run and
only ADDED TO -- a `fetch` shim goes in ahead of the app's own <script>, and
the app itself is byte-identical below it. That is the whole reason the
portable copy tracks the real one: change app.html, rebuild, and the change
is there. A hand-edited second copy would have drifted by the second week,
which is the failure this avoids.

WHAT IT WILL NEVER BE
---------------------
Live. It is a photograph with a date on it, and the page says so in the
corner. Rebuild it to move the date. `/api/override` and the other write
routes are absent from the capture on purpose: a snapshot that appeared to
accept an override would be lying about where the data went.

USAGE
    python packages/review/build_portable.py            # -> the vault folder
    python packages/review/build_portable.py --out X.html
    python packages/review/build_portable.py --port 8770

THE SERVER. If 8770 is already listening this uses it and LEAVES IT ALONE --
it never stops the server you are working in. Otherwise it starts its own on
a spare port and kills that one when it is done. Either way the database is
only ever read.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "packages" / "web" / "app.html"
SERVE = REPO / "packages" / "api" / "serve.py"

# Default destination: the OneDrive-backed vault folder, because the point of
# the exercise is a file that syncs to other devices on its own.
DEFAULT_OUT = (
    Path.home()
    / "OneDrive - PinPOINT"
    / "Obsidian Vault"
    / "Modular Investment System"
    / "Investment Micro-System.html"
)

LIVE_PORT = 8770          # the one the .vbs launcher and Stop.bat use
SPARE_PORT = 8771         # ours, when nothing is up

# ---------------------------------------------------------------------------
# The URL space.
#
# These two lists are the ONLY hand-maintained mirror of the front end, and
# they are the thing to update when a control gains an option. Both are taken
# from app.html directly:
#
#   WINDOWS  <select id="win">          (app.html, the header)
#   PILLARS  stackControls()            (app.html, the Pair view)
#
# Sectors and their peer groups are NOT listed here -- they come from /api/nav
# at build time, so adding a sector to the engine needs no edit to this file.
# ---------------------------------------------------------------------------
WINDOWS = ["5", "10", "30", "60"]
PILLARS = ["composite", "economics", "valuation", "mood", "guidance"]


# ---------------------------------------------------------------------------
# Key normalisation.
#
# The capture is stored under a normalised key -- path, then query parameters
# DECODED and sorted by name -- and the injected shim normalises the runtime
# URL the identical way before looking it up. Matching on the raw URL string
# would have made the whole thing hostage to percent-encoding trivia:
# encodeURIComponent escapes a comma, urlencode does not, and `groups=a,b`
# would miss `groups=a%2Cb` while looking correct in both files.
# ---------------------------------------------------------------------------
def norm_key(path, params):
    if not params:
        return path
    ordered = sorted(params, key=lambda kv: kv[0])
    return path + "?" + "&".join("%s=%s" % (k, v) for k, v in ordered)


def port_open(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def get_json(port, path, params):
    url = "http://127.0.0.1:%d%s" % (port, path)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def start_server(port):
    proc = subprocess.Popen(
        [sys.executable, str(SERVE), "--port", str(port)],
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for _ in range(60):                     # 30s, same patience as the .vbs
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise SystemExit("serve.py exited before binding %d:\n%s" % (port, out))
        if port_open(port):
            return proc
        time.sleep(0.5)
    proc.terminate()
    raise SystemExit("serve.py did not bind %d within 30s" % port)


# ---------------------------------------------------------------------------
def capture(port):
    """Walk every URL the page can construct. Returns (responses, meta)."""
    responses = {}

    def grab(path, params=None):
        params = params or []
        key = norm_key(path, params)
        if key in responses:
            return responses[key]
        responses[key] = get_json(port, path, params)
        print("    " + key)
        return responses[key]

    # /api/nav first -- it names the sectors, so the rest of the walk is
    # derived from the engine rather than from a list that could go stale.
    nav = grab("/api/nav")

    for p in ("/api/overview", "/api/flows", "/api/oi"):
        grab(p)

    for w in WINDOWS:
        grab("/api/scores", [("window", w)])

    sectors = [s for s in nav if s.get("kind") == "sector"]
    for s in sectors:
        grab("/api/sector", [("id", s["id"])])

    # The tape is keyed on the peer groups, not the sector, so two sectors
    # sharing a group set cost one request. getTape() also caches per pillar
    # for the SELECTED sector, hence the product.
    group_sets = set(",".join(s.get("peer_groups") or []) for s in sectors)
    for gq in sorted(group_sets):
        for pillar in PILLARS:
            params = [("pillar", pillar)]
            if gq:
                params.append(("groups", gq))
            grab("/api/tape", params)

    ov = responses.get("/api/overview") or {}
    as_of = ""
    if isinstance(ov, dict):
        as_of = str(ov.get("as_of") or ov.get("asof") or "")
    if not as_of:
        for k, v in responses.items():
            if k.startswith("/api/tape") and isinstance(v, dict) and v.get("as_of_max"):
                as_of = str(v["as_of_max"])
                break

    meta = {
        "as_of": as_of,
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "n_responses": len(responses),
    }
    return responses, meta


# ---------------------------------------------------------------------------
def js_string(text):
    """A JS string literal holding `text`, safe to sit inside <script>."""
    lit = json.dumps(text)
    # `</script` anywhere in the payload would end the block early; \u2028 and
    # \u2029 are line terminators to some parsers. Neither can survive here.
    return (
        lit.replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


SHIM_TEMPLATE = """<script>
/* ===================================================================
   PORTABLE SNAPSHOT SHIM -- generated by packages/review/build_portable.py
   Do not edit this block; edit the builder and rebuild.

   Everything below this <script> is packages/web/app.html, unchanged.
   This intercepts fetch so the app reads captured answers instead of a
   server that is not there.
   =================================================================== */
(function () {
  var SNAP = JSON.parse(__PAYLOAD__);

  function keyOf(u) {
    var url;
    try { url = new URL(u, 'http://snapshot.local'); }
    catch (e) { return String(u); }
    var ps = [];
    url.searchParams.forEach(function (v, k) { ps.push([k, v]); });
    ps.sort(function (a, b) { return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0; });
    return url.pathname + (ps.length
      ? '?' + ps.map(function (kv) { return kv[0] + '=' + kv[1]; }).join('&')
      : '');
  }

  function reply(obj) {
    return Promise.resolve(new Response(JSON.stringify(obj), {
      status: 200, headers: { 'Content-Type': 'application/json' }
    }));
  }

  window.fetch = function (input, init) {
    var u = (typeof input === 'string') ? input
          : (input && input.url) ? input.url : String(input);
    var k = keyOf(u);
    if (Object.prototype.hasOwnProperty.call(SNAP.responses, k)) {
      return reply(SNAP.responses[k]);
    }
    /* Answer 200 with an {error} body rather than rejecting. The app checks
       d.error on every call and renders it; a rejected promise would surface
       as a tab that never finishes loading, which looks like a broken file
       instead of a missing capture. */
    return reply({ error: 'not captured in this snapshot: ' + k
      + ' -- rebuild with launch/Publish Portable Copy.bat' });
  };

  /* Corner pill, fixed. Deliberately NOT inserted into the layout: this page
     is someone else's HTML and a banner in the flow would push the sticky
     header around on exactly the small screens this copy exists for. */
  function badge() {
    var d = document.createElement('div');
    d.textContent = 'SNAPSHOT \\u00b7 data ' + (SNAP.as_of || 'unknown')
                  + ' \\u00b7 built ' + SNAP.built_at;
    d.title = 'A read-only copy. It does not update on its own -- '
            + 'rebuild it on the machine that runs the system.';
    d.style.cssText = [
      'position:fixed', 'right:10px', 'bottom:10px', 'z-index:99999',
      'font:11px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace',
      'padding:5px 9px', 'border-radius:999px',
      'background:rgba(120,90,10,.92)', 'color:#fff',
      'box-shadow:0 2px 8px rgba(0,0,0,.35)',
      'cursor:default', 'opacity:.9'
    ].join(';');
    document.body.appendChild(d);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', badge);
  } else {
    badge();
  }
})();
</script>
"""


def build_shim(responses, meta):
    payload = dict(meta)
    payload["responses"] = responses
    blob = json.dumps(payload, separators=(",", ":"))
    return SHIM_TEMPLATE.replace("__PAYLOAD__", js_string(blob))


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Build the portable front-end copy.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--port", type=int, default=None,
                    help="use a server already listening here")
    args = ap.parse_args()

    if not APP.exists():
        raise SystemExit("front end not found: %s" % APP)

    proc = None
    if args.port:
        port = args.port
        if not port_open(port):
            raise SystemExit("nothing listening on %d" % port)
        print("using the server already on %d" % port)
    elif port_open(LIVE_PORT):
        port = LIVE_PORT
        print("using the server already on %d (leaving it running)" % LIVE_PORT)
    else:
        port = SPARE_PORT
        print("no server up -- starting one on %d" % SPARE_PORT)
        proc = start_server(port)

    try:
        print("capturing:")
        responses, meta = capture(port)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            print("stopped the temporary server on %d" % port)

    html = APP.read_text(encoding="utf-8")
    marker = "<script>"
    i = html.find(marker)
    if i < 0:
        raise SystemExit("no <script> in app.html -- cannot inject")
    out_html = html[:i] + build_shim(responses, meta) + html[i:]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename. OneDrive will happily begin
    # uploading a half-written file, and a truncated 3 MB page on the far
    # device is worse than yesterday's intact one.
    tmp = args.out.with_name(args.out.name + ".tmp")
    tmp.write_text(out_html, encoding="utf-8")
    tmp.replace(args.out)

    mb = args.out.stat().st_size / 1e6
    print("\n%d responses captured, data as of %s"
          % (meta["n_responses"], meta["as_of"] or "?"))
    print("wrote %s  (%.1f MB)" % (args.out, mb))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
