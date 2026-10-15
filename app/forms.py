"""Request input, read explicitly.

FastHTML binds a handler parameter from the path, then the cookies, then headers and the
query, and only then the body. A cookie named like a field would override what the user
typed, so a handler takes user input from here (or from the path), never from a parameter.
"""

import re
from urllib.parse import urlsplit

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


def https_url(raw):
    """A link someone typed, stripped, when it is a plain https:// address with a host and
    no user name or password; else None. Never a javascript:, data: or http: URL."""
    url = (raw or "").strip()
    if not url or len(url) > MAX_URL or re.search(r"[\s\x00-\x1f\x7f]", url):
        return None
    try:
        u = urlsplit(url)
        u.port  # noqa: B018  raises ValueError on a malformed port
    except ValueError:
        return None
    host = u.hostname or ""
    if u.scheme != "https" or u.username or u.password or "." not in host:
        return None
    return url


def domain(url):
    """example.org for https://www.example.org/volunteer: what a link says it opens."""
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")
