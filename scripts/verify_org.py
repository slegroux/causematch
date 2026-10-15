"""Look an EIN up in ProPublica's Nonprofit Explorer; with --org-id and --approve, mark the org verified.

uv run python scripts/verify_org.py --ein 53-0196605 [--org-id 1 --approve]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import data, propublica


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--ein", required=True)
    p.add_argument("--org-id", type=int)
    p.add_argument(
        "--approve", action="store_true", help="verify --org-id under this EIN"
    )
    a = p.parse_args()
    if a.approve and a.org_id is None:
        sys.exit("--approve needs --org-id")
    try:
        found = propublica.lookup(a.ein)
    except (ValueError, propublica.LookupFailed) as e:
        sys.exit(str(e))
    if found is None:
        sys.exit("ProPublica has no nonprofit with that EIN")
    print(f"{found['name']}, {found['city']}, {found['state']} (EIN {found['ein']})")
    if a.approve:
        try:
            data.verify_org(a.org_id, found["ein"])
        except (LookupError, ValueError) as e:
            sys.exit(str(e))
        print(f"org {a.org_id} is verified")


if __name__ == "__main__":
    main()
