"""A fingerprint of the Python the server has loaded, so staleness is detectable.

WHY. serve.py imports engine, tape and everything under them AT STARTUP, so a
running server keeps serving whatever the code said when it launched. Nothing in
the store or the page reveals that.

THE FIRST ATTEMPT AT A GUARD WAS TOO NARROW AND I FOUND OUT BY BREAKING IT.
build_frontend compared the running server's /api/nav peer_groups against
engine.SECTORS on disk. That caught the case it was written for — steel showing
"no spec" because SECTORS was a day old — and MISSED the very next one: after
tape() gained a `peer_groups` filter, the old server happily accepted
`?groups=steel_integrated,steel_converter` and IGNORED it, returning all eleven
names. nav still matched, so the guard stayed quiet while the Pair tab was still
drawing every sector's names on every tab.

A guard keyed on one symptom only catches that symptom. This one is keyed on the
CODE, so any edit to a module the server loads is visible regardless of which
route it affects.

MTIME, NOT A CONTENT HASH, deliberately. Content hashing every module on each
request is wasted work for a localhost dev server, and mtime is what actually
changes when a file is edited or pulled. The failure mode of mtime — a file
touched without changing — reports a false staleness, which is the safe
direction: it tells you to restart a server that did not need it, rather than
staying silent about one that did.
"""

from __future__ import annotations

import pathlib

# The trees serve.py's import graph actually reaches. `web/` is NOT here: app.html
# is read from disk per request, so editing it needs no restart — which is exactly
# the distinction this fingerprint has to preserve, or it would nag on every
# front-end tweak.
WATCH = ("packages/api", "packages/score", "packages/core", "packages/extract")


def fingerprint(repo: pathlib.Path) -> dict:
    newest, count = 0.0, 0
    newest_file = None
    for rel in WATCH:
        d = repo / rel
        if not d.is_dir():
            continue
        for f in d.glob("*.py"):
            m = f.stat().st_mtime
            count += 1
            if m > newest:
                newest, newest_file = m, f"{rel}/{f.name}"
    return {"newest_mtime": round(newest, 3),
            "newest_file": newest_file,
            "n_modules": count}
