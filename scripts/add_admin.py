"""Give an email address admin access (the pilot metrics page). Signs in through the same
emailed link as a coordinator.

uv run python scripts/add_admin.py --email x@example.org --name "Sylvain"

Refuses an address a volunteer signs in with unless --force is given: it would become an
admin sign-in, and they'd reach their volunteer account only through Google or Apple.
A suspended volunteer's address is refused even with --force.
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
    p.add_argument(
        "--force",
        action="store_true",
        help="go ahead even when a volunteer signs in with this address",
    )
    a = p.parse_args()
    try:
        uid = data.add_admin(a.email, a.name.strip(), allow_volunteer=a.force)
    except data.VolunteerAddress as e:
        sys.exit(f"{e} Use a different address, or run again with --force.")
    except ValueError as e:
        sys.exit(str(e))
    print(f"{a.email.strip().lower()} is an admin (profile {uid})")


if __name__ == "__main__":
    main()
