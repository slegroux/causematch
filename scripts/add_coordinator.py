"""Give an email address coordinator access to an org (admin only, until self-serve claiming).

uv run python scripts/add_coordinator.py --email x@example.org --org-id 1 [--role owner]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import data


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--email", required=True)
    p.add_argument("--org-id", required=True, type=int)
    p.add_argument("--role", choices=["owner", "coordinator"], default="coordinator")
    a = p.parse_args()
    try:
        uid = data.add_coordinator(a.email, a.org_id, a.role)
    except LookupError as e:
        sys.exit(str(e))
    print(f"{a.email.strip().lower()} is {a.role} of org {a.org_id} (profile {uid})")


if __name__ == "__main__":
    main()
