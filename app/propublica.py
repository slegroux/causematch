"""Look an EIN up in the ProPublica Nonprofit Explorer API (v2).

Network only, no database. Callers run it off the event loop and never under the data lock.
"""

import re
from contextlib import contextmanager

import httpx

URL = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
SEARCH_URL = "https://projects.propublica.org/nonprofits/api/v2/search.json"
USER_AGENT = "causematch/0.1 (open-source volunteer matching pilot, Los Angeles)"


class LookupFailed(Exception):
    """ProPublica could not be asked, or answered nonsense. Worded for an admin.
    status is the HTTP status when it answered with an error (429: slow down)."""

    status = None


def normalize_ein(raw):
    """Nine digits from "53-0196605", "530196605" or " 53 0196605 "; None if it is not one."""
    digits = re.sub(r"[\s-]", "", raw or "")
    return digits if re.fullmatch(r"[0-9]{9}", digits) else None


def _text(value):
    return value if isinstance(value, str) else ""


def _failed(e):
    err = LookupFailed(f"ProPublica did not answer ({type(e).__name__})")
    if isinstance(e, httpx.HTTPStatusError):
        err.status = e.response.status_code
    return err


def make_client():
    return httpx.Client(
        timeout=10,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )


@contextmanager
def _asking(client):
    """The client (a new one, closed after, when None); every failure is a LookupFailed."""
    own = client is None
    client = client or make_client()
    try:
        yield client
    except httpx.HTTPError as e:
        raise _failed(e) from e
    except (ValueError, KeyError, TypeError) as e:
        raise LookupFailed("ProPublica sent a response we could not read") from e
    finally:
        if own:
            client.close()


def lookup(ein, client=None):
    """{"ein", "name", "city", "state", "zip", "ntee"} for a registered nonprofit, None for an
    unknown EIN. zip is the five-digit ZIP ("" when absent), ntee the NTEE code ("" when absent).

    ValueError for a malformed EIN; LookupFailed on a timeout, an error status or a body
    that is not the expected shape.
    """
    ein = normalize_ein(ein)
    if ein is None:
        raise ValueError("an EIN is nine digits, like 53-0196605")
    with _asking(client) as c:
        r = c.get(URL.format(ein=ein))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            return None
        org = body["organization"]
        if org is None or org["name"] is None:
            return None  # a made-up EIN can answer 200 with nulls
        name = org["name"]
        if not isinstance(name, str) or not name:
            raise ValueError("no name")
        return {
            "ein": ein,
            "name": name,
            "city": _text(org.get("city")),
            "state": _text(org.get("state")),
            "zip": _text(org.get("zipcode"))[:5],
            "ntee": _text(org.get("ntee_code")),
        }


def search(q, state, ntee_id, page=0, client=None):
    """One page of the name search, 501(c)(3)s only: (hits, num_pages, skipped).

    A hit is {"ein", "name", "city", "ntee"} with the EIN as nine digits (the API sends an
    integer, so a leading zero is lost). A record that is not that shape is counted in
    skipped, not returned. LookupFailed on a timeout, an error status or a body that is not
    a search page. The search results carry no ZIP: lookup() it per hit.
    """
    params = {
        "q": q,
        "state[id]": state,
        "ntee[id]": ntee_id,
        "c_code[id]": 3,
        "page": page,
    }
    with _asking(client) as c:
        r = c.get(SEARCH_URL, params=params)
        r.raise_for_status()
        body = r.json()
        records, pages = body["organizations"], body["num_pages"]
        if not isinstance(records, list) or not isinstance(pages, int):
            raise TypeError("not a search page")
    hits, skipped = [], 0
    for rec in records:
        try:
            ein, name = rec["ein"], rec["name"]
            if isinstance(ein, bool) or not isinstance(ein, int | str):
                raise TypeError("ein")
            ein = normalize_ein(str(ein).zfill(9))
            if not ein or not isinstance(name, str) or not name:
                raise ValueError("record")
            hits.append(
                {
                    "ein": ein,
                    "name": name,
                    "city": _text(rec.get("city")),
                    "ntee": _text(rec.get("ntee_code")),
                }
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            skipped += 1
    return hits, pages, skipped
