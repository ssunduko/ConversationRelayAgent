"""Clear stored call data.

Wipes callers, conversations and appointments — the agent's memory of who has
called and what was agreed. Useful after testing, when the records are full of
voicemail greetings and mis-captured details.

    python scripts/reset_db.py --show      # what's in there
    python scripts/reset_db.py --all       # wipe everything
    python scripts/reset_db.py --before 2026-08-16T20:00
    python scripts/reset_db.py --phone +15555550133
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import shutil
import sqlite3
from datetime import datetime

from core.config import SETTINGS

TABLES = ("appointments", "conversations", "callers")


def connect() -> sqlite3.Connection:
    db = Path(SETTINGS.database_path)
    if not db.exists():
        sys.exit(f"No database at {db}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def show(conn: sqlite3.Connection) -> None:
    for table in TABLES:
        n = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        print(f"  {table:14} {n} row(s)")

    print("\nconversations:")
    for r in conn.execute(
        "SELECT id, phone, mode, started_at, summary FROM conversations ORDER BY id"
    ):
        summary = (r["summary"] or "(no summary)").replace("\n", " ")[:64]
        print(f"  #{r['id']:<3} {r['started_at'][:16]}  {r['phone']:>14}  {r['mode']:<9} {summary}")

    print("\nappointments:")
    for r in conn.execute("SELECT id, phone, first_name, last_name, email, requested_time FROM appointments"):
        name = " ".join(x for x in (r["first_name"], r["last_name"]) if x) or "(no name)"
        print(f"  #{r['id']:<3} {r['phone']:>14}  {name}  <{r['email'] or '-'}>  {r['requested_time']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", action="store_true", help="Print contents and exit")
    parser.add_argument("--all", action="store_true", help="Delete everything")
    parser.add_argument("--phone", help="Delete only this caller's records")
    parser.add_argument("--before", help="Delete conversations started before this ISO timestamp")
    parser.add_argument("--no-backup", action="store_true", help="Skip the .bak copy")
    args = parser.parse_args()

    conn = connect()

    if args.show or not (args.all or args.phone or args.before):
        show(conn)
        if not args.show:
            print("\nNothing deleted. Pass --all, --phone or --before to remove records.")
        return 0

    db = Path(SETTINGS.database_path)
    if not args.no_backup:
        backup = db.with_suffix(f".{datetime.now():%Y%m%d-%H%M%S}.bak")
        shutil.copy2(db, backup)
        print(f"backed up to {backup.name}")

    with conn:
        if args.all:
            for table in TABLES:
                n = conn.execute(f"DELETE FROM {table}").rowcount
                print(f"  deleted {n} row(s) from {table}")
            # Restart id numbering so the next call is #1 again.
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('conversations','appointments')")
        elif args.phone:
            for table in TABLES:
                n = conn.execute(f"DELETE FROM {table} WHERE phone = ?", (args.phone,)).rowcount
                print(f"  deleted {n} row(s) from {table} for {args.phone}")
        elif args.before:
            n = conn.execute(
                "DELETE FROM conversations WHERE started_at < ?", (args.before,)
            ).rowcount
            print(f"  deleted {n} conversation(s) started before {args.before}")

    conn.execute("VACUUM")
    print("\nremaining:")
    show(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
