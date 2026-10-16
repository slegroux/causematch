"""Print the pilot's headline numbers as `key: value` lines, reading the database
read-only: nothing is created, migrated or written.

uv run python scripts/stats.py --db PATH
"""

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.metrics import METRICS, URGENT, pilot_metrics
from app.metrics_rows import fetch_rows


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", required=True, help="path to the SQLite file")
    a = p.parse_args()
    if not Path(a.db).is_file():
        sys.exit(f"no such database: {a.db}")
    con = sqlite3.connect(f"file:{Path(a.db).resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    def q(sql, params=()):
        return [dict(r) for r in con.execute(sql, params)]

    try:
        rows = fetch_rows(q)
        open_openings = q("SELECT count(*) AS n FROM opening WHERE status = 'open'")[0][
            "n"
        ]
    except sqlite3.Error as e:
        sys.exit(f"not a Causematch database: {e}")
    metrics = pilot_metrics(rows, datetime.now(UTC))
    overall = metrics["overall"]
    shifts = [s for s in rows["shifts"] if s["status"] == "completed"]
    print(f"open_openings: {open_openings}")
    print(f"completed_shifts: {len(shifts)}")
    print(f"completed_pairs: {len({(s['volunteer_id'], s['org_id']) for s in shifts})}")
    for key in METRICS:
        v = overall[key]["value"]
        print(f"{key}: {'' if v is None else round(v, 4)}")
    for key in URGENT:
        v = metrics["urgent"][key]["value"]
        print(f"{key}: {'' if v is None else round(v, 4)}")
    empty = metrics["empty_deck"]
    print(f"empty_deck_views: {empty['views']}")
    print(f"empty_deck_volunteers: {empty['volunteers']}")


if __name__ == "__main__":
    main()
