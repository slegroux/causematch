"""Import nonprofits from ProPublica's Nonprofit Explorer into the orgs table.

uv run python scripts/import_orgs.py --city "Los Angeles" --state CA [--dry-run] [--limit N] [--cause SLUG]
uv run python scripts/import_orgs.py --city "Santa Monica" --state CA ...
uv run python scripts/import_orgs.py --ein 86-1620575 [--dry-run] [--cause SLUG]

The API cannot filter by city and its search matches names only, so for each cause the
script searches the state's 501(c)(3)s in that cause's NTEE major group for the place's
name and a few cause words, keeps the hits whose postal place is in Los Angeles County
(app/org_import.py PLACE_NAMES, a cost prefilter), then looks each up for its ZIP. A
nonprofit is imported when its ZIP is a Los Angeles County ZIP in app/la_zips.csv, which
is the boundary (so Van Nuys, Pasadena and Santa Monica count) and gives its lat/lon (the
ZIP's centre),
and its NTEE code maps to a cause (below). Everything else is skipped, including
nonprofits with no NTEE code and ones named without any of the search words, so a run
finds a sample, not all of them. --ein imports one nonprofit by EIN under the same rules
(--cause overrides the NTEE mapping; the ZIP rule still applies).

NTEE code -> cause:
  K30-K36  food banks, pantries, meal programs  -> food-insecurity
           (not K03 societies, K2x agriculture, K40 nutrition advocacy, K50 home economics)
  C20-C99  environmental quality and protection -> environment-parks
           (not C01-C19 alliances, management and support groups)
  D20-D23  animal protection, humane societies  -> animal-shelters
           (not D30 wildlife, D40 veterinary services, D50 zoos)
  L        housing and shelter                  -> homelessness-services (inactive)
           Revisit before activating it: L2x is housing development, not homeless
           services, and P85 (homeless centers) is not mapped yet.

Imported orgs are unverified and unclaimed, with intro "cold". Re-running updates the
ProPublica fields by EIN and leaves an admin's edits alone. Be polite: a pause between
requests, a page cap, a User-Agent naming the project, and a stop on HTTP 429.
--dry-run writes nothing; the last line printed is always "N orgs".
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import propublica
from app.org_import import (
    CAUSE_LETTER,
    CAUSE_TERMS,
    NTEE_CAUSES,
    NTEE_GROUP_IDS,
    PLACE_NAMES,
    NotImportable,
    import_ein,
    ntee_cause,
)


class RateLimited(Exception):
    """ProPublica answered 429: stop the whole run."""


def search_page(client, q, state, letter, page, stats):
    """(hits, num_pages) for one page; None after a failure counted in stats."""
    try:
        hits, pages, skipped = propublica.search(
            q, state, NTEE_GROUP_IDS[letter], page, client
        )
    except propublica.LookupFailed as e:
        stats["errors"] += 1
        print(f"search {q!r} page {page}: {e}", file=sys.stderr)
        if e.status == 429:
            raise RateLimited from e
        return None
    stats["skipped"] += skipped
    return hits, pages


def candidates(client, city, state, cause, max_pages, delay, sleep, seen, stats):
    """Search hits mailing from a Los Angeles County place whose NTEE code maps to this
    cause, EINs not yet seen."""
    letter = CAUSE_LETTER[cause]
    for q in (f'"{city}"', *CAUSE_TERMS[cause]):
        for page in range(max_pages):
            sleep(delay)
            found = search_page(client, q, state, letter, page, stats)
            if found is None:
                break
            hits, pages = found
            for h in hits:
                if h["ein"] in seen or h["city"].lower() not in PLACE_NAMES:
                    continue
                if ntee_cause(h["ntee"]) != cause:
                    continue
                seen.add(h["ein"])
                yield h
            if page + 1 >= pages:
                break


def report(result):
    print(
        f"{result['verb']}: {result['ein']} {result['name']} ({result['zip']}, "
        f"{result['ntee']}) -> {result['cause']}"
    )


def main(argv=None, client=None, sleep=time.sleep):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--city", help="a place in Los Angeles County, the area with a ZIP list"
    )
    p.add_argument("--state", help="two letters, like CA")
    p.add_argument("--ein", help="import this one nonprofit instead of searching")
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
    if not a.ein and not (a.city and a.state):
        p.error("--city and --state are required unless --ein is given")
    if a.limit is not None and a.limit < 1:
        p.error("--limit must be at least 1")
    if not 1 <= a.max_pages <= 20:
        p.error("--max-pages must be 1 to 20")
    if a.delay < 0:
        p.error("--delay can't be negative")
    if a.city and a.city.lower() not in PLACE_NAMES:
        p.error("only Los Angeles County has a ZIP list (app/la_zips.csv)")

    own = client is None
    client = client or propublica.make_client()
    count, stats = 0, {"skipped": 0, "errors": 0}
    try:
        if a.ein:
            sleep(a.delay)
            count = import_one(a, client, stats)
        else:
            count = import_search(a, client, sleep, stats)
    except RateLimited:
        print("ProPublica asked us to slow down (429): stopping.", file=sys.stderr)
    finally:
        if own:
            client.close()
    print(f"skipped {stats['skipped']} records, {stats['errors']} errors")
    print(f"{count} orgs")
    return 1 if stats["errors"] and not count else 0


def import_one(a, client, stats):
    try:
        report(import_ein(a.ein, client, a.dry_run, a.cause))
    except (ValueError, propublica.LookupFailed) as e:  # NotImportable is a ValueError
        stats["errors" if isinstance(e, propublica.LookupFailed) else "skipped"] += 1
        print(f"{a.ein}: {e}", file=sys.stderr)
        if getattr(e, "status", None) == 429:
            raise RateLimited from e
        return 0
    return 1


def import_search(a, client, sleep, stats):
    seen, count = set(), 0
    for cause in [a.cause] if a.cause else NTEE_CAUSES.values():
        for hit in candidates(
            client, a.city, a.state, cause, a.max_pages, a.delay, sleep, seen, stats
        ):
            sleep(a.delay)
            try:
                result = import_ein(hit["ein"], client, a.dry_run, cause)
            except NotImportable:
                stats["skipped"] += 1
                continue
            except propublica.LookupFailed as e:
                stats["errors"] += 1
                print(f"{hit['ein']}: {e}", file=sys.stderr)
                if e.status == 429:
                    raise RateLimited from e
                continue
            except (ValueError, TypeError, AttributeError) as e:
                stats["errors"] += 1
                print(
                    f"{hit['ein']}: unexpected record ({type(e).__name__})",
                    file=sys.stderr,
                )
                continue
            count += 1
            report(result)
            if a.limit is not None and count >= a.limit:
                return count
    return count


if __name__ == "__main__":
    sys.exit(main())
