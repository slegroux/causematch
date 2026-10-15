"""Give an email address coordinator access to an org (admin only, until self-serve claiming).

uv run python scripts/add_coordinator.py --email x@example.org --org-id 1 \
        --name "Maria Lopez, volunteer coordinator" [--phone "(310) 555-0123"] [--role owner]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import data
from app.auth import normalize_phone


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--email", required=True)
    p.add_argument("--org-id", required=True, type=int)
    p.add_argument(
        "--name", required=True, help="contact name and role, shown to volunteers"
    )
    p.add_argument(
        "--phone", default="", help="optional; lets the coordinator choose Text"
    )
    p.add_argument("--role", choices=["owner", "coordinator"], default="coordinator")
    a = p.parse_args()
    phone = normalize_phone(a.phone) if a.phone else ""
    if a.phone and not phone:
        sys.exit(f"not a US phone number: {a.phone}")
    try:
        uid = data.add_coordinator(a.email, a.org_id, a.role, a.name.strip(), phone)
    except (LookupError, ValueError) as e:
        sys.exit(str(e))
    print(f"{a.email.strip().lower()} is {a.role} of org {a.org_id} (profile {uid})")


if __name__ == "__main__":
    main()
