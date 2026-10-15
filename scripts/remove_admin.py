"""Revoke admin access: the admin is signed out at once.

uv run python scripts/remove_admin.py --email x@example.org
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import data


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--email", required=True)
    a = p.parse_args()
    try:
        uid = data.remove_admin(a.email)
    except LookupError as e:
        sys.exit(str(e))
    print(f"{a.email.strip().lower()} is no longer an admin (profile {uid})")


if __name__ == "__main__":
    main()
