"""Import nonprofits from ProPublica's Nonprofit Explorer into the orgs table.

uv run python scripts/import_orgs.py --city "Los Angeles" --state CA [--dry-run] [--limit N] [--cause SLUG]

The API cannot filter by city and its search matches names only, so for each cause the
script searches the state's 501(c)(3)s in that cause's NTEE major group for the city's name
and a few cause words, keeps the hits whose city is exactly the one asked for, then looks
each up for its ZIP. A nonprofit is imported when its NTEE major letter maps to a cause
(below) and its ZIP is a City of Los Angeles ZIP in app/la_zips.csv, which also gives its
lat/lon (the ZIP's centre). Everything else is skipped, including nonprofits with no NTEE
code and ones named without any of the search words, so a run finds a sample, not all of them.

NTEE major letter -> cause (the NTEE groups the API's ntee[id] filter spans in brackets):
  K  food, agriculture and nutrition   -> food-insecurity      [5 human services]
  C  environmental quality, protection -> environment-parks    [3 environment and animals]
  D  animal-related                    -> animal-shelters      [3 environment and animals]
  L  housing and shelter               -> homelessness-services (inactive until screening exists)

Imported orgs are unverified and unclaimed, with intro "cold". Re-running updates the
ProPublica fields by EIN and leaves an admin's edits alone. Be polite: a pause between
requests, a page cap, a User-Agent naming the project. --dry-run writes nothing; the last
line printed is always "N orgs".
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import geo, propublica

NTEE_CAUSES = {
    "K": "food-insecurity",
    "C": "environment-parks",
    "D": "animal-shelters",
    "L": "homelessness-services",
}
NTEE_GROUP_IDS = {"C": 3, "D": 3, "K": 5, "L": 5}  # the API's ntee[id] values
CAUSE_TERMS = {
    "food-insecurity": ("food", "pantry", "meals"),
    "environment-parks": ("park", "garden", "environment", "conservation"),
    "animal-shelters": ("animal", "rescue", "humane"),
    "homelessness-services": ("homeless", "shelter", "housing"),
}
CAUSE_LETTER = {cause: letter for letter, cause in NTEE_CAUSES.items()}


def candidates(client, city, state, cause, max_pages, delay, sleep, seen, stats):
    """Search hits in the city whose NTEE letter is this cause's, EINs not yet seen."""
    letter = CAUSE_LETTER[cause]
    for q in (f'"{city}"', *CAUSE_TERMS[cause]):
        for page in range(max_pages):
            sleep(delay)
            try:
                hits, pages, skipped = propublica.search(
                    q, state, NTEE_GROUP_IDS[letter], page, client
                )
            except propublica.LookupFailed as e:
                stats["errors"] += 1
                print(f"search {q!r} page {page}: {e}", file=sys.stderr)
                break
            stats["skipped"] += skipped
            for h in hits:
                if h["ein"] in seen:
                    continue
                if h["city"].lower() != city.lower() or h["ntee"][:1] != letter:
                    continue
                seen.add(h["ein"])
                yield h
            if page + 1 >= pages:
                break


def main(argv=None, client=None, sleep=time.sleep):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--city", required=True)
    p.add_argument("--state", required=True, help="two letters, like CA")
    p.add_argument("--dry-run", action="store_true", help="write nothing")
    p.add_argument("--limit", type=int, help="stop after this many orgs")
    p.add_argument("--cause", choices=sorted(CAUSE_LETTER), help="only this cause")
    p.add_argument(
        "--max-pages", type=int, default=5, help="search pages per query (default 5)"
    )
    p.add_argument(
        "--delay", type=float, default=1.0, help="seconds between requests (default 1)"
    )
    a = p.parse_args(argv)
    if a.limit is not None and a.limit < 1:
        p.error("--limit must be at least 1")
    if not 1 <= a.max_pages <= 20:
        p.error("--max-pages must be 1 to 20")
    if a.delay < 0:
        p.error("--delay can't be negative")
    if a.city.lower() != "los angeles":
        p.error("only Los Angeles has a ZIP list (app/la_zips.csv)")

    own = client is None
    client = client or propublica.make_client()
    seen, count = set(), 0
    stats = {"skipped": 0, "errors": 0}
    try:
        for cause in [a.cause] if a.cause else NTEE_CAUSES.values():
            for hit in candidates(
                client,
                a.city,
                a.state,
                cause,
                a.max_pages,
                a.delay,
                sleep,
                seen,
                stats,
            ):
                sleep(a.delay)
                try:
                    org = propublica.lookup(hit["ein"], client)
                except propublica.LookupFailed as e:
                    stats["errors"] += 1
                    print(f"{hit['ein']}: {e}", file=sys.stderr)
                    continue
                place = (
                    geo.city_zip_latlon(org["zip"])
                    if org
                    and org["city"].lower() == a.city.lower()
                    and org["state"].upper() == a.state.upper()
                    and org["ntee"][:1] == CAUSE_LETTER[cause]
                    else None
                )
                if place is None:
                    stats["skipped"] += 1
                    continue
                verb = "would import"
                if not a.dry_run:
                    # imported here: opening the database creates it, which a dry run must not
                    from app import data

                    verb = data.upsert_imported_org(
                        org["ein"],
                        org["name"],
                        org["city"],
                        org["zip"],
                        org["ntee"],
                        cause,
                        *place,
                    )
                count += 1
                print(
                    f"{verb}: {org['ein']} {org['name']} ({org['zip']}, "
                    f"{org['ntee']}) -> {cause}"
                )
                if a.limit is not None and count >= a.limit:
                    break
            if a.limit is not None and count >= a.limit:
                break
    finally:
        if own:
            client.close()
    print(f"skipped {stats['skipped']} records, {stats['errors']} errors")
    print(f"{count} orgs")
    return 1 if stats["errors"] and not count else 0


if __name__ == "__main__":
    sys.exit(main())
