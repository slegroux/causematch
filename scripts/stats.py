"""Print the pilot's headline numbers as `key: value` lines.

uv run python scripts/stats.py --db PATH
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", required=True, help="path to the SQLite file")
    a = p.parse_args()
    if not Path(a.db).is_file():
        sys.exit(f"no such database: {a.db}")
    os.environ["CAUSEMATCH_DB"] = a.db  # before app.db is imported

    from app import data
    from app.metrics import METRICS, pilot_metrics

    rows = data.metrics_rows()
    overall = pilot_metrics(rows)["overall"]
    print(f"open_openings: {data.open_openings_count()}")
    print(
        f"completed_shifts: {sum(1 for s in rows['shifts'] if s['status'] == 'completed')}"
    )
    for key in METRICS:
        v = overall[key]["value"]
        print(f"{key}: {'' if v is None else round(v, 4)}")


if __name__ == "__main__":
    main()
