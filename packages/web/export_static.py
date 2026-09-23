"""Freeze the front end into ONE self-contained HTML file, for the vault.

    python packages/web/export_static.py                 # -> the vault
    python packages/web/export_static.py --out x.html    # somewhere else
    python packages/web/export_static.py --no-book       # drop the positions
    python packages/web/export_static.py --selftest

PM, 2026-09-20: *"can we create a copy of the front-end in the vault that can
be accessed by me elsewhere? It should get updated with each full-refresh?"*
The vault lives in OneDrive, so a file written there syncs to the phone and to
any other machine that is signed in. That is the whole delivery mechanism —
there is no server, no port, no hosting cost, which is the same constraint the
rest of this system is built under.

WHAT THIS IS, SAID PLAINLY: a photograph of the running app, not the app. Every
`/api/*` payload the page can ask for is computed ONCE here and baked into the
file; a shim in front of `window.fetch` answers from that map. `app.html` is
copied through UNMODIFIED — one <script> is inserted before </head> and nothing
else is touched. That is deliberate: the day this file starts editing app.html
is the day the vault copy and the desk copy can disagree about what the system
says, and a second front end is exactly what this project does not want.

IT IMPORTS `engine` DIRECTLY AND DOES NOT CALL THE SERVER. refresh.py runs
unattended and serve.py may well not be up; driving the export over HTTP would
make the vault copy silently skip on any morning the port was busy. Same reason
the routes below are transcribed from serve.py rather than fetched: if serve.py
grows a route, this file must be edited too, and the `--selftest` route-parity
check FAILS until it is, rather than the vault copy quietly losing a tab.

THREE THINGS CANNOT SURVIVE THE FREEZE, and each is handled loudly:

  * `/data/companies/*.pdf` — the filed transcripts are ~20MB and are not in
    git. The links stay visible (they are provenance) but a click explains
    where the document actually lives instead of 404ing.
  * a miss on any route — recorded in `window.__IMS_MISSES` and rendered as an
    amber line in the snapshot bar. A hole that shows nothing is the one
    failure this codebase keeps writing guards against.
  * staleness — the in-page refresh light reads the FROZEN status.json, so it
    stays green forever once the file stops being rewritten. The snapshot bar
    therefore computes age in the VIEWER's browser against the export stamp,
    which is the only clock that cannot freeze with the file.

WEEKDAYS, NOT CALENDAR DAYS, in that bar — per the refresh-light rule in
CLAUDE.md, a Monday that reads "2 days old" trains you to ignore the light, and
an indicator that is ignored has failed. Weekdays rather than true trading days
because a holiday calendar is another thing to maintain and wrong the first
year nobody updates it; the bar says "weekdays" so the approximation is stated
rather than implied.

THE BOOK IS INCLUDED BY DEFAULT and `--no-book` turns it off. This is a
judgement, so it is written down: the vault is the PM's own OneDrive inside
PinPOINT, not a third party, and an exported copy of the front end that quietly
dropped the Book would be a worse surprise than including it. CLAUDE.md's
"position data stays in gitignored data/" is a rule about GIT — a public-ish
remote — and it is untouched by this file, which writes nothing into the repo.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
from urllib.parse import parse_qsl, urlparse

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
WEB = REPO / "packages" / "web"
sys.path.insert(0, str(REPO / "packages" / "api"))

# The vault, and a NEW folder inside it. NOT `Dashboard/` — that is the old
# vault system's node dashboard on port 8765, which CLAUDE.md rules is not this
# project and must not be touched.
VAULT = (pathlib.Path.home() / "OneDrive - PinPOINT" / "Obsidian Vault"
         / "Investment Micro-System")

# /api/scores is fetched once per option of the #win select; /api/book_index
# once per button. Both lists are transcribed from app.html and checked against
# it by --selftest, because a window silently missing from the export renders
# as an error the moment the PM touches the control.
WINDOWS = ["5", "10", "30", "60"]
BIX_RANGES = ["1mo", "3mo", "6mo", "1y", "2y", "3y", "5y"]
PILLARS = ["composite", "economics", "valuation", "mood", "guidance"]
BUB_WINS = ["1", "5", "20"]


# ----------------------------------------------------------------- key

def key(url: str) -> str:
    """Normalise a request URL to the map key.

    THIS FUNCTION EXISTS TWICE — here and in `_SHIM` below — and the two must
    agree exactly or the page misses every parameterised route while the
    unparameterised ones still work, which reads as "some tabs are broken"
    rather than "the key rule diverged". They are kept in sync by the
    round-trip case in --selftest and, finally, by opening the exported file.
    Sorted params so `?a=1&b=2` and `?b=2&a=1` cannot be two entries.
    """
    u = urlparse(url)
    ps = sorted(parse_qsl(u.query, keep_blank_values=True))
    q = "&".join(f"{k}={v}" for k, v in ps)
    return u.path + ("?" + q if q else "")


# ----------------------------------------------------------------- collect

def collect(include_book: bool = True, verbose: bool = True) -> dict:
    """Compute every payload app.html can ask for. Returns key -> object."""
    import engine
    import tape as tape_mod

    out: dict[str, object] = {}

    def put(path: str, obj) -> None:
        out[key(path)] = obj
        if verbose:
            print(f"  {key(path)[:68]:<70} {len(json.dumps(obj, default=str)):>9,}")

    # --- no parameters -------------------------------------------------
    nav = engine.nav_list()
    put("/api/nav", nav)
    put("/api/overview", engine.overview())
    put("/api/morning", engine.morning())
    put("/api/cement_watch", engine.cement_watch())
    # Vahan maker share, the Auto tab. Cheap (3 segments x ~6 labels
    # x 13 months, already aggregated in the DB) so the whole thing
    # rides in one payload like cement_watch.
    put("/api/auto_share", engine.auto_share())
    put("/api/flows", engine.flows())
    put("/api/oi", engine.oi_snapshot())
    bub = engine.oi_bubbles()
    put("/api/oi_bubbles", bub)

    # --- the Book ------------------------------------------------------
    # Refused rather than omitted, so the tab says why instead of rendering an
    # empty panel that reads as "no positions".
    if include_book:
        put("/api/book", engine.book_view())
        for r in BIX_RANGES:
            put(f"/api/book_index?range={r}", engine.book_index(r))
    else:
        msg = {"error": "the Book is excluded from this offline copy "
                        "(exported with --no-book)"}
        put("/api/book", msg)
        for r in BIX_RANGES:
            put(f"/api/book_index?range={r}", dict(msg))

    # --- scores, one per #win option -----------------------------------
    for w in WINDOWS:
        put(f"/api/scores?window={w}",
            {pg: engine.compute(pg, int(w))
             for s in engine.SECTORS for pg in s["peer_groups"]})

    # --- tape: pillar x the peer-group SET of each sector ---------------
    # app.html sends the selected sector's groups joined by commas (getTape),
    # and caches on that join — so the export must key on the same join, not on
    # the sector. Two sectors with identical groups share one entry, which is
    # what the page's own cache does.
    # THE EMPTY GROUP-SET IS ENUMERATED, AND IT COSTS 3.7MB OF THE 11MB FILE.
    # It was dropped once, on the reasoning that app.html hides the Pair tab on
    # `live: false` (it, auto) so `?pillar=X` with no groups is unreachable.
    # The exported file then MISSED it three times in a click-through of the
    # nav: `pair` is SECTOR_SCOPED, so switching to IT or Auto re-runs
    # loadPair() for the new sector even though the section is not the visible
    # one, and with no peer_groups the request carries no `groups`.
    #
    # Cheaper alternatives were considered and refused. An {error:...} stub
    # makes getTape() throw, which render() turns into a visible error banner
    # on a tab where nothing is actually wrong. An empty payload renders a
    # blank chart, i.e. the silent hole. And leaving it to miss puts a
    # permanent amber warning in the snapshot bar every time the PM opens IT.
    # 3.7MB buys the export being honest at rest; take it.
    gqs = sorted({",".join(s["peer_groups"]) for s in engine.SECTORS})
    for p in PILLARS:
        for gq in gqs:
            path = f"/api/tape?pillar={p}" + (f"&groups={gq}" if gq else "")
            gs = [x for x in gq.split(",") if x]
            put(path, tape_mod.tape(p, None, gs or None))

    # --- sector detail, one per nav sector ------------------------------
    # est_date is NOT enumerated: it replays the consensus panel as of an
    # arbitrary past date, which is a server feature with an unbounded
    # parameter space. The control falls through to the miss path and says so.
    for s in nav:
        if s.get("kind") == "sector":
            put(f"/api/sector?id={s['id']}", engine.sector_detail(s["id"], None))

    # --- company dossiers ------------------------------------------------
    for s in nav:
        for c in (s.get("companies") or []):
            put(f"/api/company?sector={s['id']}&slug={c['slug']}",
                engine.company_view(c["slug"], s["id"]))
        if s.get("companies"):
            put(f"/api/company?sector={s['id']}",
                engine.company_view(None, s["id"]))

    # --- results: every sector chip x its roster --------------------------
    # The tab's picker offers EVERY covered name, not only the ones holding
    # data, so every (sector, entity) the page can ask for is enumerated —
    # ~50 payloads of a few KB. A name left out would render the tab's error
    # banner on a click where nothing is wrong. The bare route and the
    # per-sector default are what the tab opens on.
    res = engine.results_view()
    put("/api/results", res)
    for s in res["sectors"]:
        rs = engine.results_view(s["id"])
        put(f"/api/results?sector={s['id']}", rs)
        for c in rs["companies"]:
            put(f"/api/results?sector={s['id']}&entity={c['id']}",
                engine.results_view(s["id"], c["id"]))

    # --- per-row history, opened by clicking a row ----------------------
    # Both are lazy in the page, so a miss shows only when a row is expanded —
    # the quietest possible hole, which is why they are enumerated in full
    # rather than sampled.
    for r in engine.oi_snapshot():
        put(f"/api/oi_history?id={r['entity_id']}",
            engine.oi_history(r["entity_id"]))
    for m in (engine.overview().get("movers") or []):
        put(f"/api/input_history?id={m['id']}", engine.input_history(m["id"]))

    # --- oi_movers: every date on the bubble strip x every window -------
    # ~800 entries at ~1KB. Enumerated in FULL rather than capped at the recent
    # end: the miss path here renders an empty list (the page does `m.in || []`)
    # which is indistinguishable from a session in which nothing moved, and
    # that is precisely the silent-hole shape. Cheap enough not to risk it.
    for d in (bub.get("dates") or []):
        for w in BUB_WINS:
            put(f"/api/oi_movers?date={d}&win={w}", engine.oi_movers(d, w))

    return out


# ----------------------------------------------------------------- render

_SHIM = r"""
<script>
/* ---------------------------------------------------------------------
   OFFLINE SNAPSHOT SHIM — inserted by packages/web/export_static.py.
   app.html above/below this block is byte-identical to the desk copy.
   --------------------------------------------------------------------- */
window.__IMS_SNAP = __SNAP__;
window.__IMS_DATA = __DATA__;
window.__IMS_MISSES = [];
(function () {
  /* MUST MATCH export_static.key() EXACTLY. Sorted pairs, joined with & and
     =, prefixed by the path. A divergence here does not throw — it misses
     every parameterised route while the plain ones keep working, which reads
     as "some tabs broke" rather than "the key rule moved". */
  function k(url) {
    var s = String(url), i = s.indexOf('?');
    var path = i < 0 ? s : s.slice(0, i);
    if (i < 0) return path;
    var ps = [];
    s.slice(i + 1).split('&').forEach(function (p) {
      if (!p) return;
      var j = p.indexOf('=');
      ps.push([decodeURIComponent(j < 0 ? p : p.slice(0, j)),
               decodeURIComponent(j < 0 ? '' : p.slice(j + 1))]);
    });
    ps.sort(function (a, b) {
      if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;
      return a[1] === b[1] ? 0 : (a[1] < b[1] ? -1 : 1);
    });
    return path + '?' + ps.map(function (p) { return p[0] + '=' + p[1]; }).join('&');
  }
  window.fetch = function (url) {
    var kk = k(url), hit = Object.prototype.hasOwnProperty.call(window.__IMS_DATA, kk);
    if (!hit && window.__IMS_MISSES.indexOf(kk) < 0) {
      window.__IMS_MISSES.push(kk);
      if (window.__imsBar) window.__imsBar();
    }
    var body = hit ? window.__IMS_DATA[kk]
                   : {error: 'not in this offline snapshot: ' + kk};
    return Promise.resolve({
      ok: hit, status: hit ? 200 : 404,
      json: function () { return Promise.resolve(body); },
      text: function () { return Promise.resolve(JSON.stringify(body)); }
    });
  };

  /* WEEKDAYS since the export, computed in the VIEWER's browser. The refresh
     light inside the page reads the frozen status.json and therefore stays
     green forever; this is the only clock in the file that still moves.
     Weekdays, not calendar days, or every Monday reads two days old and the
     bar gets ignored — the one way an indicator can fail. */
  function wd(from, to) {
    var n = 0, d = new Date(from.getFullYear(), from.getMonth(), from.getDate());
    var e = new Date(to.getFullYear(), to.getMonth(), to.getDate());
    while (d < e) { d.setDate(d.getDate() + 1); if (d.getDay() % 6) n++; }
    return n;
  }
  function bar() {
    var el = document.getElementById('ims-snapbar');
    if (!el) {
      el = document.createElement('div');
      el.id = 'ims-snapbar';
      el.style.cssText = 'font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;'
        + 'padding:6px 14px;border-bottom:1px solid #30363d;background:#161b22;'
        + 'color:#8b949e';
      document.body.insertBefore(el, document.body.firstChild);
    }
    var S = window.__IMS_SNAP, age = wd(new Date(S.exported_at), new Date());
    var warn = age >= 2, col = warn ? '#d29922' : '#8b949e';
    el.style.color = col;
    el.style.borderBottom = '1px solid ' + (warn ? '#d29922' : '#30363d');
    var t = (warn ? '\u26a0 ' : '') + 'offline snapshot \u00b7 exported '
      + S.exported_at.slice(0, 16).replace('T', ' ')
      + ' \u00b7 ' + (age === 0 ? 'today' : age + ' weekday' + (age > 1 ? 's' : '') + ' old')
      + ' \u00b7 scores as of ' + (S.as_of || '?')
      + (S.book ? '' : ' \u00b7 Book excluded');
    if (window.__IMS_MISSES.length)
      t += '  \u26a0 not in this snapshot: ' + window.__IMS_MISSES.join(', ');
    el.textContent = t;
  }
  window.__imsBar = bar;

  /* Filed documents are ~20MB of PDF and stay on the desk machine. The link is
     provenance, so it is kept and explained rather than removed — a citation
     with nothing behind it is worse than one that says where the thing is. */
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href^="/data/"]');
    if (!a) return;
    e.preventDefault();
    alert('This filed document is not in the offline copy.\n\n'
      + a.getAttribute('href') + '\n\nIt lives under data/ on the desk machine; '
      + 'open the live app at http://127.0.0.1:8770 to read it.');
  }, true);

  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', bar);
  else bar();
})();
</script>
"""


def render(payloads: dict, meta: dict) -> str:
    html = (WEB / "app.html").read_text(encoding="utf-8")
    if "</head>" not in html:
        raise SystemExit("app.html has no </head> — cannot insert the shim")
    data = json.dumps(payloads, default=str, separators=(",", ":"))
    # `<` only ever appears inside a JSON string here, so escaping it is safe
    # and closes the </script> break-out. Do not drop this: a dossier quote
    # containing "</script>" would otherwise end the block mid-payload.
    data = (data.replace("<", "\\u003c").replace(">", "\\u003e")
                .replace("&", "\\u0026").replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029"))
    shim = (_SHIM.replace("__SNAP__", json.dumps(meta))
                 .replace("__DATA__", data))
    return html.replace("</head>", shim + "\n</head>", 1)


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=REPO, capture_output=True, text=True,
                              timeout=10).stdout.strip() or "?"
    except Exception:
        return "?"


def export(out: pathlib.Path, include_book: bool = True,
           verbose: bool = True) -> dict:
    payloads = collect(include_book, verbose)
    ov = payloads.get("/api/overview") or {}
    meta = {"exported_at": dt.datetime.now().isoformat(timespec="seconds"),
            "as_of": ov.get("as_of_scores") or ov.get("as_of"),
            "git": git_sha(), "book": bool(include_book),
            "routes": len(payloads)}
    html = render(payloads, meta)
    out.parent.mkdir(parents=True, exist_ok=True)
    # ATOMIC, and not for tidiness. This runs DETACHED from refresh.py (the
    # build takes ~80s and the launcher blocks on the refresh), and the
    # launcher re-refreshes on every double-click — so two exports can be
    # writing at once, and OneDrive is watching the folder and will sync
    # whatever it sees. A plain write would let it upload a half-finished 11MB
    # file that opens as a blank page on the phone. os.replace is atomic on the
    # same volume, hence the temp file NEXT TO the target rather than in %TEMP%.
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(out)
    meta["bytes"] = out.stat().st_size
    meta["path"] = str(out)
    return meta


# ----------------------------------------------------------------- selftest

def selftest() -> None:
    ok = 0

    def eq(got, want, what):
        nonlocal ok
        assert got == want, f"{what}: {got!r} != {want!r}"
        ok += 1

    # --- key normalisation: ACCEPTANCE and rejection, per the GLOB lesson ---
    eq(key("/api/nav"), "/api/nav", "bare path")
    eq(key("/api/scores?window=30"), "/api/scores?window=30", "one param")
    eq(key("/api/oi_movers?date=2026-09-19&win=5"),
       "/api/oi_movers?date=2026-09-19&win=5", "two params in order")
    eq(key("/api/oi_movers?win=5&date=2026-09-19"),
       "/api/oi_movers?date=2026-09-19&win=5", "two params out of order")
    eq(key("/api/company?sector=auto&slug=eicher-motors"),
       "/api/company?sector=auto&slug=eicher-motors", "company")
    # percent-encoded values must decode to the same key as plain ones, or the
    # page's encodeURIComponent() on an id with a separator misses.
    # THIS FIXTURE WAS WRONG ON ITS FIRST RUN AND THE CODE WAS RIGHT — it
    # expected `pillar` first, forgetting that the whole point is to SORT, and
    # `groups` < `pillar`. Do not "fix" the code back to insertion order: that
    # is the collision the sort exists to prevent.
    eq(key("/api/tape?pillar=composite&groups=a%2Cb"),
       "/api/tape?groups=a,b&pillar=composite", "encoded comma, sorted")
    assert key("/api/scores?window=30") != key("/api/scores?window=60"), \
        "different windows must not collide"
    ok += 1

    # --- the JS twin must carry the same rule -------------------------------
    # It cannot be executed here, so what is checked is that it is still SORTING
    # and still joining with the same characters. This is a smoke check, not a
    # proof; the real test is opening the exported file, which is why the shim
    # also records misses into __IMS_MISSES rather than failing silently.
    for frag in ["ps.sort(", "decodeURIComponent", "p[0] + '=' + p[1]", "'&'"]:
        assert frag in _SHIM, f"shim lost {frag!r} — key rule may have diverged"
        ok += 1

    # --- ROUTE PARITY with serve.py ----------------------------------------
    # The point of this check: a route added to serve.py and not here means a
    # tab that works on the desk and is a blank panel in the vault. Failing the
    # selftest is how that gets noticed.
    src = (REPO / "packages" / "api" / "serve.py").read_text(encoding="utf-8")
    served = set(re.findall(r'u\.path == "(/api/[a-z_]+)"', src))
    # Fetched by app.html; the rest of serve.py's routes are not wired to the UI.
    app = set(re.findall(r"fetch\(\s*[`'\"]?(/api/[a-z_]+)",
                         (WEB / "app.html").read_text(encoding="utf-8")))
    app |= set(re.findall(r"'(/api/[a-z_]+)\?", (WEB / "app.html").read_text(encoding="utf-8")))
    exported = {"/api/nav", "/api/overview", "/api/morning", "/api/cement_watch",
                "/api/flows", "/api/oi", "/api/oi_bubbles", "/api/book",
                "/api/book_index", "/api/scores", "/api/tape", "/api/sector",
                "/api/company", "/api/oi_history", "/api/input_history",
                "/api/oi_movers", "/api/results", "/api/auto_share"}
    missing = (app & served) - exported
    assert not missing, f"app.html fetches {missing} and the export skips it"
    ok += 1
    assert exported <= served, f"export knows a route serve.py does not: " \
                               f"{exported - served}"
    ok += 1

    # --- control lists must match app.html ---------------------------------
    html = (WEB / "app.html").read_text(encoding="utf-8")
    wins = re.search(r'<select id="win">(.*?)</select>', html, re.S)
    eq(re.findall(r'value="(\d+)"', wins.group(1)), WINDOWS, "#win options")
    lab = re.search(r"const BIX_LABEL = \{(.*?)\};", html, re.S)
    eq(re.findall(r"'([0-9a-z]+)':", lab.group(1)), BIX_RANGES, "index ranges")
    eq(re.findall(r'class="bbtn bwin" data-w="(\d+)"', html), BUB_WINS,
       "bubble windows")

    # --- render: the </script> break-out must be closed --------------------
    poison = {"/api/nav": [{"label": "</script><script>alert(1)</script>"}]}
    out = render(poison, {"exported_at": "2026-01-01T00:00:00", "as_of": "x",
                          "git": "0", "book": True, "routes": 1})
    assert "</script><script>alert" not in out, "payload escaped the script tag"
    assert "\\u003c/script" in out, "expected the escaped form"
    ok += 2
    # ...and the acceptance case: an ordinary payload survives round-trip.
    plain = {"/api/nav": [{"id": "overview", "label": "Daily Overview"}]}
    out = render(plain, {"exported_at": "2026-01-01T00:00:00", "as_of": "x",
                         "git": "0", "book": True, "routes": 1})
    m = re.search(r"window\.__IMS_DATA = (.*?);\nwindow\.__IMS_MISSES", out, re.S)
    eq(json.loads(m.group(1)), plain, "payload round-trips")
    assert out.count("<!doctype html>") == 1 and out.count("</head>") == 1, \
        "app.html was altered beyond the single insertion"
    ok += 1

    print(f"export_static selftest: {ok} checks passed")


# ----------------------------------------------------------------- cli

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path,
                    default=VAULT / "Investment Micro-System.html")
    ap.add_argument("--no-book", action="store_true",
                    help="exclude the PM's positions from the exported copy")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        raise SystemExit(0)

    if not a.out.parent.exists() and a.out.parent.parent.exists() is False:
        # The vault is in OneDrive; if it is not there, say so rather than
        # creating a lookalike folder somewhere unsynced.
        raise SystemExit(f"no such folder: {a.out.parent.parent} — is OneDrive "
                         f"signed in? Pass --out to write elsewhere.")

    m = export(a.out, include_book=not a.no_book, verbose=not a.quiet)
    print(f"\n{m['routes']} routes  {m['bytes']:,} bytes  "
          f"as_of {m['as_of']}  git {m['git']}"
          f"{'' if m['book'] else '  (no book)'}")
    print(m["path"])
