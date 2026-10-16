"""Request-level defences: same-origin checks, security headers, body size, rate limits, the
client IP, and the error pages.

`Guard` wraps the whole app (main.py) and runs before routing, sessions and Beforeware, so
it also covers the public routes and websockets that Beforeware skips.
"""

import hashlib
import hmac
import ipaddress
import logging
import re
import threading
import time
import traceback
import uuid
from html import escape
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import HTMLResponse

from app import data, photos
from app.config import (
    is_production,
    phone_hash_key,
    public_base_url,
    sms_ceilings,
    trusted_proxy_hops,
)

log = logging.getLogger("causematch")

MAX_BODY = 64 * 1024  # bytes; every form here is far smaller, except the uploads below
# POST paths that take a picture (app/photos.py): they alone may send photos.MAX_UPLOAD.
UPLOADS = (
    re.compile(r"/me/profile/photo"),
    re.compile(r"/org/organization/[0-9]+/(logo|photo)"),
    re.compile(r"/org/openings/[0-9]+/photo"),
)
UNSAFE = ("POST", "PUT", "PATCH", "DELETE")
# Cloud Scheduler calls these with a bearer token and sends no Origin. Exact paths: a
# future /tasks/ route isn't exempt until it is listed (and checks the token).
ORIGIN_EXEMPT = frozenset({"/tasks/reminders", "/tasks/urgent"})
# Static assets may be cached; every other response may hold someone's data.
CACHEABLE = ("/static/", "/favicon.ico")
STYLESHEETS = (
    "/static/vendor/franken-ui-2.0.0/core.min.css",
    "/static/vendor/daisyui-4.12.24/full.min.css",
    "/static/theme.css",
    "/static/vendor/app.css",
)

# --- origins -------------------------------------------------------------------------


def _origin(url):
    """scheme://host[:port] of a URL, the way a browser writes an Origin header."""
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    default = 443 if u.scheme == "https" else 80
    return f"{u.scheme}://{u.hostname}" + (
        f":{port}" if port not in (None, default) else ""
    )


def allowed_origin():
    return _origin(public_base_url())


def same_origin(headers):
    """The request came from one of our own pages: its Origin is exactly ours or, when a
    browser sent no Origin, its Referer is on our origin. Neither: refused."""
    origin = headers.get("origin")
    if origin is not None:
        return origin == allowed_origin()
    referer = headers.get("referer")
    return bool(referer) and _origin(referer) == allowed_origin()


# --- headers -------------------------------------------------------------------------


def csp():
    ws = re.sub(
        r"^http", "ws", allowed_origin()
    )  # https -> wss, http -> ws (development)
    return "; ".join(
        (
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            # Franken UI and daisyUI draw some icons as data: SVGs; blob: is the preview
            # static/photo.js shows of a photo before it is uploaded
            "img-src 'self' data: blob:",
            f"connect-src 'self' {ws}",
            "font-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        )
    )


# The pages whose "Use my location" button (static/geo.js) may ask for the device location.
GEOLOCATION_PATHS = ("/me/preferences", "/onboarding")


def security_headers(path):
    geo = "(self)" if path in GEOLOCATION_PATHS else "()"
    h = {
        "Content-Security-Policy": csp(),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": f"camera=(), microphone=(), geolocation={geo}, payment=()",
        "X-Frame-Options": "DENY",
    }
    if is_production():
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if not path.startswith(CACHEABLE):
        h["Cache-Control"] = "no-store"
    return h


# --- pages ---------------------------------------------------------------------------


def page(status, title, text, headers=None):
    """A small self-contained page for responses made outside the routes (403, 413, 429, 500)."""
    links = "".join(f'<link rel="stylesheet" href="{s}">' for s in STYLESHEETS)
    body = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)} - Causematch</title>"
        f"<script src='/static/theme.js'></script>{links}</head><body>"
        # app/ui.py SimplePage's look, as plain HTML: this page is made outside the routes
        "<div class='mx-auto max-w-2xl px-4 pb-12'>"
        "<p class='pt-6 pb-4 font-display text-2xl font-extrabold tracking-tight "
        "text-ink'>cause<span class='text-brand'>match</span></p>"
        "<div class='p-6 sm:p-8 rounded-3xl border border-line bg-white shadow-warm'>"
        "<h1 class='font-display text-2xl font-extrabold text-ink'>"
        f"{escape(title)}</h1><p class='mt-2 text-ink'>{escape(text)}</p>"
        "<p class='mt-4'><a class='uk-link-muted' href='/'>Back to Causematch</a></p>"
        "</div></div></body></html>"
    )
    return HTMLResponse(body, status_code=status, headers=headers)


def too_many(retry_after=600):
    return page(
        429,
        "Too many tries",
        "That happened too many times in a short while. Wait a few minutes, then try again.",
        headers={"Retry-After": str(retry_after)},
    )


def _forbidden():
    return page(
        403,
        "Request refused",
        "This form was sent from outside Causematch. Go back to the page and try again.",
    )


def _too_large(path=""):
    if max_body(path) > MAX_BODY:
        return page(413, "Photo too large", "Choose a photo under 6 MB and try again.")
    return page(
        413, "Too much text", "That was too long to send. Shorten it and try again."
    )


def max_body(path):
    """The most bytes a request to `path` may send."""
    if any(rx.fullmatch(path) for rx in UPLOADS):
        return photos.MAX_UPLOAD
    return MAX_BODY


def _server_error(rid):
    return page(
        500,
        "Something went wrong",
        f"We couldn't finish that. Try again in a moment. Reference: {rid}",
    )


# --- the middleware ------------------------------------------------------------------


class _BodyTooLarge(Exception):
    pass


def request_id(headers):
    """Cloud Run's trace id when there is one (it ties our log line to the request log)."""
    trace = headers.get("x-cloud-trace-context", "").split("/")[0]
    return trace if re.fullmatch(r"[0-9a-f]{32}", trace) else uuid.uuid4().hex


class Guard:
    """Outermost app middleware: same-origin checks, headers, body limit, error page."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            # Browsers send Origin on every handshake, and the same-origin policy doesn't
            # stop a foreign page reading a socket: refuse it before any handler runs.
            if Headers(scope=scope).get("origin") != allowed_origin():
                return await send({"type": "websocket.close", "code": 1008})
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = Headers(scope=scope)
        path, method = scope["path"], scope["method"]
        rid = request_id(headers)
        started = False

        async def send_with_headers(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                h = MutableHeaders(scope=message)
                for k, v in security_headers(path).items():
                    if k not in h:  # a route's own value wins (no-referrer, say)
                        h[k] = v
                h["X-Request-Id"] = rid
            await send(message)

        early = None
        limit = max_body(path)
        if method in UNSAFE and path not in ORIGIN_EXEMPT and not same_origin(headers):
            early = _forbidden()
        elif int(headers.get("content-length") or 0) > limit:
            early = _too_large(path)
        if early is not None:
            return await early(scope, receive, send_with_headers)

        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > limit:  # a chunked body with no Content-Length
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except _BodyTooLarge:
            if started:
                raise
            await _too_large(path)(scope, receive, send_with_headers)
        except Exception as e:
            # Frames only: an exception's message can hold a phone number or an email.
            frames = "".join(traceback.format_tb(e.__traceback__))
            log.error(
                "request %s: %s %s raised %s\n%s",
                rid,
                method,
                path,
                type(e).__name__,
                frames,
            )
            if started or not is_production():
                raise  # development and tests keep the traceback
            await _server_error(rid)(scope, receive, send_with_headers)


# --- client IP -----------------------------------------------------------------------


def client_ip(scope):
    """The address rate limits count against, IPv4-mapped IPv6 written as IPv4.

    TRUSTED_PROXY_HOPS (app/config.py) says how many addresses the proxies in front append
    to X-Forwarded-For. The client is that many places from the right; anything further
    left is whatever the client sent, and is ignored. With 0 (off Cloud Run), or a header
    too short or unparseable at that place, the socket peer is used.
    """
    peer = (scope.get("client") or ("unknown", 0))[0]
    hops = trusted_proxy_hops()
    ip = peer
    if hops:
        entries = Headers(scope=scope).get("x-forwarded-for", "").split(",")
        if len(entries) >= hops:
            ip = entries[-hops].strip()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return peer  # not an address at all (the test client says "testclient")
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


def _subject(ip, v6_prefix=64):
    """An IPv6 client is counted by its /64 (one subscriber usually holds a whole one), or
    by a coarser prefix for the network-wide SMS limit."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/{v6_prefix}", strict=False))
    return ip


# --- persistent limits ---------------------------------------------------------------

# (name, limit, window seconds), counted in a SQLite table so a restart doesn't reset
# them: these send texts or email (which cost money) or would let someone enumerate.
# Codes are limited per (phone, client address) first, so someone else hammering a
# number from elsewhere doesn't lock its owner out; the per-phone day cap is looser.
SMS_PER_PHONE_IP = (("sms-phone-ip-10m", 5, 600), ("sms-phone-ip-day", 10, 24 * 3600))
SMS_PER_PHONE = (("sms-phone-day", 20, 24 * 3600),)
SMS_PER_IP = (("sms-ip", 60, 3600),)  # IPv4 address or IPv6 /64
SMS_PER_NET = (("sms-net", 120, 3600),)  # IPv6 /48 (IPv4: the address again)
VERIFY_PER_PHONE = (
    ("verify-phone", 10, 600),
)  # wrong codes; then locked for the window
LINK_PER_EMAIL = (("link-email", 5, 3600),)
LINK_PER_IP = (("link-ip", 20, 3600),)
CLAIM_PER_IP = (("claim-ip", 5, 3600),)
CLAIM_PER_EMAIL = (("claim-email", 3, 24 * 3600),)
PROPUBLICA = (
    ("propublica", 30, 3600),
)  # admin lookups and imports, all admins together
assert all(
    w <= data.RATE_KEEP_SECONDS
    for rules in (SMS_PER_PHONE_IP, SMS_PER_PHONE, SMS_PER_IP, SMS_PER_NET)
    + (VERIFY_PER_PHONE, LINK_PER_EMAIL, LINK_PER_IP, CLAIM_PER_IP, CLAIM_PER_EMAIL)
    + (PROPUBLICA,)
    for _, _, w in rules
)


def sms_all():
    """The ceiling on every text the app sends (codes and notifications), subject "all"."""
    hour, day = sms_ceilings()
    return (("sms-all-hour", hour, 3600), ("sms-all-day", day, 24 * 3600))


def sms_urgent():
    """Urgent calls' own ceiling, half of each of sms_all's, subject "urgent": waves never
    take the whole budget, so sign-in codes always have room."""
    hour, day = sms_ceilings()
    return (
        ("sms-urgent-hour", max(1, hour // 2), 3600),
        ("sms-urgent-day", max(1, day // 2), 24 * 3600),
    )


def _key(name, subject):
    """The stored key: the limit's name and a keyed hash, never the phone, email or IP."""
    mac = hmac.new(phone_hash_key().encode(), str(subject).encode(), hashlib.sha256)
    return f"{name}:{mac.hexdigest()[:32]}"


def allow(*limits, now=None, record=True):
    """Count one action against every (rules, subject) pair; False, counting nothing, when
    any of them is already at its limit (logged by name, never the subject). With
    record=False only check. Calls data, so run it off the event loop."""
    keys = [
        (_key(name, subject), limit, window)
        for rules, subject in limits
        for name, limit, window in rules
    ]
    over = data.rate_take(keys, time.time() if now is None else now, record)
    if over is not None:
        log.warning("rate limit reached: %s", over.split(":")[0])
    return over is None


def ip_subject(req, v6_prefix=64):
    subject = _subject(client_ip(req.scope), v6_prefix)
    # For the deploy check that X-Forwarded-For is read right (TRUSTED_PROXY_HOPS): a
    # spoofed header must not change this, another network must. Hashed, never the address.
    log.debug("client key %s", _key("client", subject)[-12:])
    return subject


def sms_send_limits(req, phone):
    """Every limit one sign-in code counts against."""
    ip = ip_subject(req)
    return (
        (SMS_PER_PHONE_IP, f"{phone}|{ip}"),
        (SMS_PER_PHONE, phone),
        (SMS_PER_IP, ip),
        (SMS_PER_NET, ip_subject(req, 48)),
        (sms_all(), "all"),
    )


# --- in-memory token buckets ---------------------------------------------------------

# name: (capacity, seconds to refill from empty). Frequent, cheap actions, per user; a
# restart refilling them costs nothing.
BUCKETS = {
    "swipe": (30, 30),  # a burst of 30 cards, then one a second
    "chat": (20, 10),  # chat POSTs and websocket frames together
    "safety": (10, 3600),  # report and block POSTs
    "upload": (10, 600),  # pictures (UPLOADS): 10 in a row, then one a minute
}
# POST paths and their bucket
THROTTLED = (
    (re.compile(r"/swipe"), "swipe"),
    (re.compile(r"/(org/)?chat/[^/]+/send"), "chat"),
    (re.compile(r"/(org/)?matches/[^/]+/(report|block)"), "safety"),
    *((rx, "upload") for rx in UPLOADS),
)
_buckets = {}  # (name, user id) -> (tokens, last refill time)
_buckets_lock = threading.Lock()


def take_token(name, uid, now=None):
    capacity, refill = BUCKETS[name]
    now = time.monotonic() if now is None else now
    with _buckets_lock:
        tokens, last = _buckets.get((name, uid), (capacity, now))
        tokens = min(capacity, tokens + (now - last) * capacity / refill)
        if tokens < 1:
            _buckets[(name, uid)] = (tokens, now)
            return False
        _buckets[(name, uid)] = (tokens - 1, now)
        return True


async def throttle(req):
    """Beforeware, after the sign-in check (which sets the user id)."""
    if req.method != "POST":
        return
    for rx, name in THROTTLED:
        if rx.fullmatch(req.url.path):
            if not take_token(name, req.scope.get("uid")):
                return too_many(retry_after=BUCKETS[name][1])
            return


def reset_buckets():
    with _buckets_lock:
        _buckets.clear()


# --- logs ----------------------------------------------------------------------------


class RedactTokens(logging.Filter):
    """Drop the sign-in token from uvicorn's access log lines (the emailed link's query)."""

    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            args[2] = re.sub(r"(token=)[^&\s]+", r"\1[redacted]", str(args[2]))
            record.args = tuple(args)
        return True
