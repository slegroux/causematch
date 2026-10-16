"""Give an email address admin access (the pilot metrics page). Signs in through the same
emailed link as a coordinator.

uv run python scripts/add_admin.py --email x@example.org --name "Sylvain"
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import data


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    a = p.parse_args()
    try:
        uid = data.add_admin(a.email, a.name.strip())
    except ValueError as e:
        sys.exit(str(e))
    print(f"{a.email.strip().lower()} is an admin (profile {uid})")


if __name__ == "__main__":
    main()
