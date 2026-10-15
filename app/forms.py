"""Request input, read explicitly.

FastHTML binds a handler parameter from the path, then the cookies, then headers and the
query, and only then the body. A cookie named like a field would override what the user
typed, so a handler takes user input from here (or from the path), never from a parameter.
"""

import re
from urllib.parse import urlsplit, urlunsplit

from fasthtml.common import Response

MAX_URL = 500


async def str_form(req):
    """The submitted form, or None when any field is a file part."""
    form = await req.form()
    if any(not isinstance(v, str) for _, v in form.multi_items()):
        return None
    return form


def bad_request():
    return Response("Bad request", status_code=400)


def query(req, name):
    """One query-string value, "" when absent."""
    return req.query_params.get(name, "")


# Characters that reorder how text is drawn: they let "good.org" display for another host
BIDI = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def https_url(raw):
    """A link someone typed, as a plain https:// address with a host and no user name or
    password, its host in ASCII (an international name as punycode, xn--..., so a
    look-alike letter can't pass for a familiar domain); else None. Never a javascript:,
    data: or http: URL, a backslash (browsers read it as a slash) or a bidi control."""
    url = (raw or "").strip()
    if (
        not url
        or len(url) > MAX_URL
        or "\\" in url
        or re.search(r"[\s\x00-\x1f\x7f]", url)
        or BIDI.search(url)
    ):
        return None
    try:
        u = urlsplit(url)
        port = u.port  # raises ValueError on a malformed port
        host = (u.hostname or "").encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return None
    if u.scheme != "https" or u.username or u.password or "." not in host:
        return None
    return urlunsplit(u._replace(netloc=host + (f":{port}" if port else "")))


def domain(url):
    """example.org for https://www.example.org/volunteer: what a link says it opens."""
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")
