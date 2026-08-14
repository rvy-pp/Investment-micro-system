"""Apply schema.sql to the store. Idempotent — safe to re-run.

Usage:  python packages/core/init_db.py [--db data/ims.db]
"""

import argparse
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
SCHEMA = HERE / "schema.sql"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(REPO / "data" / "ims.db"))
    args = ap.parse_args()

    db_path = pathlib.Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"sqlite runtime : {sqlite3.sqlite_version}")
    if sqlite3.sqlite_version_info < (3, 37):
        print("ERROR: STRICT tables need sqlite >= 3.37", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()

    def names(kind: str) -> list[str]:
        q = "SELECT name FROM sqlite_master WHERE type=? ORDER BY name"
        return [r[0] for r in conn.execute(q, (kind,))]

    print(f"db             : {db_path}")
    print(f"tables ({len(names('table')):2d})    : {', '.join(names('table'))}")
    print(f"views  ({len(names('view')):2d})    : {', '.join(names('view'))}")

    # Prove the two commitments that are enforced in SQL, rather than assume it.
    checks = [
        (
            "observations rejects an uncited number",
            "INSERT INTO observations (source_id,entity_id,as_of,factor,metric,"
            "value_num,confidence,quote,extractor_version,created_at) "
            "VALUES ('s','e','2026-08-15','f','m',1.0,0.9,'  ','v1','now')",
        ),
        (
            "signals rejects a signal with no falsifier",
            "INSERT INTO signals (run_id,as_of,kind,direction,conviction,thesis,"
            "falsifier,created_at) "
            "VALUES (1,'2026-08-15','single','long','high','t','','now')",
        ),
        (
            "entities rejects a scoreable but untradeable name",
            "INSERT INTO entities (id,kind,name,peer_group,is_tradeable,parent_id) "
            "VALUES ('x','reporting_unit','X','aluminium_primary',0,'hindalco')",
        ),
    ]
    print("\nintegrity checks (each must be REJECTED):")
    ok = True
    for label, sql in checks:
        try:
            conn.execute(sql)
            conn.rollback()
            print(f"  FAIL  accepted  — {label}")
            ok = False
        except sqlite3.IntegrityError:
            print(f"  ok    rejected  — {label}")
        except sqlite3.Error as exc:  # wrong error type still means the guard is wrong
            print(f"  FAIL  {type(exc).__name__}: {exc} — {label}")
            ok = False

    conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
