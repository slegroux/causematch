"""Look an EIN up in the ProPublica Nonprofit Explorer API (v2).

Network only, no database. Callers run it off the event loop and never under the data lock.
"""

import re

import httpx

URL = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"


class LookupFailed(Exception):
    """ProPublica could not be asked, or answered nonsense. Worded for an admin."""


def normalize_ein(raw):
    """Nine digits from "53-0196605", "530196605" or " 53 0196605 "; None if it is not one."""
    digits = re.sub(r"[\s-]", "", raw or "")
    return digits if re.fullmatch(r"[0-9]{9}", digits) else None


def make_client():
    return httpx.Client(timeout=10, headers={"Accept": "application/json"})


def lookup(ein, client=None):
    """{"ein", "name", "city", "state"} for a registered nonprofit, None for an unknown EIN.

    ValueError for a malformed EIN; LookupFailed on a timeout, an error status or a body
    that is not the expected shape.
    """
    ein = normalize_ein(ein)
    if ein is None:
        raise ValueError("an EIN is nine digits, like 53-0196605")
    own = client is None
    client = client or make_client()
    try:
        r = client.get(URL.format(ein=ein))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        org = r.json()["organization"]
        name = org["name"]
        if not isinstance(name, str) or not name:
            raise ValueError("no name")
        return {
            "ein": ein,
            "name": name,
            "city": org.get("city") or "",
            "state": org.get("state") or "",
        }
    except httpx.HTTPError as e:
        raise LookupFailed(f"ProPublica did not answer ({type(e).__name__})") from e
    except (ValueError, KeyError, TypeError) as e:
        raise LookupFailed("ProPublica sent a response we could not read") from e
    finally:
        if own:
            client.close()
