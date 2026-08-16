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

    # CREATE TABLE IF NOT EXISTS will NOT add columns to a table that already
    # exists, so a schema change lands silently as "no error, no effect".
    # `oi` gained columns after first creation; it is safe to rebuild because
    # it is populated purely from the vault adapter and holds no authored data.
    # Anything holding authored data must get a real migration, never a drop.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(oi)")}
    if cols and "buildup_15d" not in cols:
        n = conn.execute("SELECT count(*) FROM oi").fetchone()[0]
        conn.execute("DROP TABLE oi")
        print(f"migrated: rebuilt `oi` for new columns (dropped {n} regenerable rows)")

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
            "INSERT INTO signals (as_of,kind,direction,conviction,thesis,"
            "falsifier,spec_version,code_sha,created_at) "
            "VALUES ('2026-08-15','single','long','high','t','','v1','sha','now')",
        ),
        (
            "entities rejects a scoreable but untradeable name",
            "INSERT INTO entities (id,kind,name,peer_group,is_tradeable,parent_id) "
            "VALUES ('x','reporting_unit','X','aluminium_primary',0,'hindalco')",
        ),
        (
            "economics rejects an intensity with no provenance",
            "INSERT INTO economics (entity_id,effective_from,line_kind,item,"
            "intensity,intensity_unit,market_pct,source_note,spec_version) "
            "VALUES ('nalco','2026-01-01','input','alumina',1.93,'t/t',0.0,'  ','v1')",
        ),
        (
            "guidance rejects a commitment with no stated target",
            "INSERT INTO guidance (entity_id,source_id,issued_date,period,metric,"
            "target_type,quote,created_at) "
            "VALUES ('vaml','s','2026-08-01','Q2FY27','volume','point','we expect',"
            "'now')",
        ),
        (
            "guidance_evidence rejects neutral evidence",
            "INSERT INTO guidance_evidence (guidance_id,source_id,as_of,direction,"
            "weight,quote,created_at) "
            "VALUES (1,'s','2026-08-01',0,0.5,'nothing happened','now')",
        ),
        (
            "economics rejects an input line with no intensity",
            "INSERT INTO economics (entity_id,effective_from,line_kind,item,"
            "source_note,spec_version) "
            "VALUES ('nalco','2026-01-01','input','alumina','ar-fy26','v1')",
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

    # Rejection tests alone are not enough. A constraint that rejects EVERYTHING
    # passes every rejection test — which is exactly what happened: the date
    # CHECKs used GLOB '____-__-__', but in GLOB `_` is a literal underscore
    # (it is LIKE that treats it as a wildcard), so no real date could ever be
    # inserted. Caught only when the price loader tried to write actual data.
    # Every guard now needs a matching ACCEPTANCE test.
    accepts = [
        (
            "prices accepts a well-formed date",
            "INSERT INTO entities (id,kind,name) VALUES ('_t','commodity','t')",
            "INSERT INTO prices (entity_id,date,close) VALUES ('_t','2026-08-15',1.0)",
        ),
        (
            "observations accepts a properly cited row",
            # each case rolls back, so it must create its own prerequisites
            "INSERT INTO entities (id,kind,name) VALUES ('_t','commodity','t')",
            "INSERT INTO sources (id,kind,source_date,captured_at,raw_path) "
            "VALUES ('_s','manual','2026-08-15','now','x')",
            "INSERT INTO observations (source_id,entity_id,as_of,factor,metric,"
            "value_num,confidence,quote,extractor_version,created_at) "
            "VALUES ('_s','_t','2026-08-15','f','m',1.0,0.9,'cited text','v1','now')",
        ),
    ]
    print("\nacceptance checks (each must be ACCEPTED):")
    for label, *sqls in accepts:
        try:
            for s in sqls:
                conn.execute(s)
            print(f"  ok    accepted  — {label}")
        except sqlite3.Error as exc:
            print(f"  FAIL  {type(exc).__name__}: {exc} — {label}")
            ok = False
        finally:
            conn.rollback()

    conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
