"""Quick inspection of the local SQLite corpus -- uses Python's built-in
sqlite3 module, so no separate CLI install is needed (useful on Windows,
where sqlite3.exe isn't preinstalled the way it often is on Mac/Linux).

Usage:
    python src/inspect_db.py
    python src/inspect_db.py --sample 5
"""

import argparse
import sqlite3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/triage.db")
    parser.add_argument("--sample", type=int, default=0, help="Also show N recent issue titles")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    issues_total = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    open_count = conn.execute("SELECT COUNT(*) FROM issues WHERE state='open'").fetchone()[0]
    closed_count = conn.execute("SELECT COUNT(*) FROM issues WHERE state='closed'").fetchone()[0]
    docs_total = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]

    print(f"Issues: {issues_total} total ({open_count} open, {closed_count} closed)")
    print(f"Docs:   {docs_total} files")

    if args.sample:
        print(f"\n{args.sample} most recently created issues:")
        rows = conn.execute(
            "SELECT number, state, title FROM issues ORDER BY created_at DESC LIMIT ?",
            (args.sample,),
        ).fetchall()
        for number, state, title in rows:
            print(f"  #{number} [{state}] {title}")

    conn.close()


if __name__ == "__main__":
    main()