import logging
import os
import re
import smtplib
import socket
import ssl
import threading
import time
import unicodedata
from contextlib import contextmanager, nullcontext, suppress
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import anyio
import httpx

from app import data, security
from app.config import (
    SMTP_TLS_PORT,
    collect_phone,
    contact_email,
    fakes_allowed,
    public_base_url,
    smtp,
    smtp_max_per_day,
)
from app.match import LA, start_text

log = logging.getLogger("causematch")

FAKE_CODE = "000000"
SMS_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}"
TWILIO_STOPPED = 21610  # the recipient replied STOP
MESSAGE_WINDOW = 30 * 60  # seconds: at most one new-message text per match per window
LIKES_WINDOW = 15 * 60  # seconds: at most one new-likes email per org per window
MAX_ATTEMPTS = 5  # tries for one send before it is left failed
MAX_REMINDERS_PER_SWEEP = 100  # the rest wait for the next hourly sweep
_LOCAL_FORMAT = "%Y-%m-%dT%H:%M"
VERIFY_URL = "https://verify.twilio.com/v2/Services/{sid}"
TWILIO_KEYS = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_VERIFY_SID",
    "TWILIO_FROM_NUMBER",
)
FAKE_KEPT = 1000  # the most recent sends a fake keeps: a preview runs for days
SMTP_TIMEOUT = 10  # seconds, for the connection and each SMTP command
SMTP_DEADLINE = 30  # seconds for one send in all, connecting and signing in included


def _record(sent, item):
    """Append to a fake's .sent, dropping the oldest past FAKE_KEPT."""
    sent.append(item)
    if len(sent) > FAKE_KEPT:
        del sent[0]


class FakeVerifier:
    """Test, local-dev and preview verifier: sends nothing, accepts 000000."""

    def __init__(self):
        self.sent = []

    def send(self, phone):
        _record(self.sent, (phone, FAKE_CODE))

    def check(self, phone, code):
        return code == FAKE_CODE and any(p == phone for p, _ in self.sent)


class TwilioVerifier:
    def __init__(self, account_sid, auth_token, verify_sid):
        self.client = httpx.Client(
            base_url=VERIFY_URL.format(sid=verify_sid),
            auth=(account_sid, auth_token),
            timeout=10,
        )

    def send(self, phone):
        self.client.post(
            "/Verifications", data={"To": phone, "Channel": "sms"}
        ).raise_for_status()

    def check(self, phone, code):
        r = self.client.post("/VerificationCheck", data={"To": phone, "Code": code})
        # Twilio answers 404 for an expired or already-used verification.
        if r.status_code == 404:
            return False
        r.raise_for_status()
        try:
            return r.json().get("status") == "approved"
        except ValueError as e:
            raise httpx.DecodingError("Twilio returned a non-JSON body") from e


def _fake(env, var):
    """Whether the fake gate `var` is on; RuntimeError if it is on outside dev, test, preview."""
    if env.get(var) != "1":
        return False
    if not fakes_allowed(env):
        raise RuntimeError(f"{var}=1 needs CAUSEMATCH_ENV=development, test or preview")
    return True


def _twilio(env, keys):
    """The values of `keys`, or None when no Twilio variable is set at all (Twilio is
    optional: volunteers sign in by email, Google or Apple, and get email instead of
    texts). Some set but not these refuses to start: a half-configured Twilio is a
    mistake."""
    if not any(env.get(k) for k in TWILIO_KEYS):
        return None
    if not all(env.get(k) for k in keys):
        raise RuntimeError(
            f"Twilio is half configured: set {', '.join(keys)}, or none of "
            f"{', '.join(TWILIO_KEYS)} (or CAUSEMATCH_ENV=development with "
            "CAUSEMATCH_FAKE_SMS=1)"
        )
    return [env[k] for k in keys]


def make_verifier(env=os.environ):
    """The code sender that proves a number before Me's add-a-number saves it
    (app/routes/profile.py), or None while TWILIO_VERIFY_SID is unset: then no number can
    be added. Never a sign-in: volunteers sign in by email, Google or Apple."""
    if _fake(env, "CAUSEMATCH_FAKE_SMS"):
        return FakeVerifier()
    if not env.get("TWILIO_VERIFY_SID"):
        return None
    keys = _twilio(
        env, ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SID")
    )
    return keys and TwilioVerifier(*keys)


_UNSET = object()
_verifier = _UNSET


def get_verifier():
    global _verifier
    if _verifier is _UNSET:
        _verifier = make_verifier()
    return _verifier


def phone_verify_enabled():
    """Whether Me offers add-a-number: CAUSEMATCH_COLLECT_PHONE=1 and a verifier that can
    prove a number by a texted code. Texts to a number a volunteer already has don't
    depend on it."""
    return collect_phone() and get_verifier() is not None


def send_code(phone):
    """The one place a verification code leaves the app, so rate limiting can wrap it."""
    if (verifier := get_verifier()) is None:
        raise RuntimeError("adding a number is off: Twilio Verify is not configured")
    verifier.send(phone)


def check_code(phone, code):
    verifier = get_verifier()
    return verifier is not None and verifier.check(phone, code)


class FakeEmailer:
    """Test, local-dev and preview emailer: sends nothing, records messages, logs them
    (the address masked)."""

    def __init__(self):
        self.sent = []
        self.with_headers = []  # (to, subject, headers) of messages that had any

    def send(self, to, subject, body, headers=None, kind="notice"):
        _record(self.sent, (to, subject, body))
        if headers:
            _record(self.with_headers, (to, subject, headers))
        log.warning("FAKE EMAIL to %s: %s\n%s", mask_email(to), subject, body)


class ResendEmailer:
    def __init__(self, api_key, sender):
        self.sender = sender
        self.client = httpx.Client(
            base_url="https://api.resend.com",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )

    def send(self, to, subject, body, headers=None, kind="notice"):
        message = {"from": self.sender, "to": [to], "subject": subject, "text": body}
        if headers:
            message["headers"] = headers
        self.client.post("/emails", json=message).raise_for_status()


DAY = 24 * 3600
# Shares of SMTP_MAX_PER_DAY, in every 24 hours. Staff's sign-in links get a floor
# (1 - PUBLIC_SHARE) that nothing else can reach. Inside the public share, notices (urgent
# calls' emails among them, with their own share inside, as urgent texts get half the SMS
# ceiling) and sign-up links each have a cap, so volunteers' links to existing accounts
# always have room too.
PUBLIC_SHARE, NOTICE_SHARE, SIGNUP_SHARE, URGENT_SHARE = 0.9, 0.5, 0.25, 0.25
# what an emailer's send(kind=) takes; "staff" is send_login_link's
KINDS = ("staff", "sign-in", "sign-up", "notice", "urgent")
SMTP_BREAK = 60  # seconds without connecting after a deadline or a failed connect
SMTP_AUTH_BREAK = (
    600  # after Gmail refuses the password: it won't fix itself in a minute
)


class EmailCeiling(Exception):
    """SMTP_MAX_PER_DAY emails were sent over SMTP in the last 24 hours, or this kind of
    email used its share of it: not sent."""


class SMTPUnavailable(Exception):
    """A connect failed or a send ran out of time less than SMTP_BREAK ago: not tried."""


class _Deadline:
    """SMTP_DEADLINE for one send. watch(sock) keeps a duplicate of its descriptor, so
    when time runs out the socket can be shut whatever is blocked on it (a TLS handshake,
    the greeting, a reply), and the blocked call fails at once."""

    def __init__(self):
        self.end = time.monotonic() + SMTP_DEADLINE
        self.hit = False
        self._dup = None
        self._lock = threading.Lock()
        self._timer = threading.Timer(SMTP_DEADLINE, self._shut)
        self._timer.start()

    def left(self):
        return max(0.01, self.end - time.monotonic())

    def watch(self, sock):
        with self._lock:
            if self.hit:
                raise TimeoutError("SMTP deadline")
            self._dup = socket.fromfd(sock.fileno(), sock.family, sock.type)

    def _shut(self):
        with self._lock:
            self.hit = True
            if self._dup is not None:
                with suppress(OSError):
                    self._dup.shutdown(socket.SHUT_RDWR)

    def done(self):
        """Stop the timer and wait for it, so hit is final."""
        self._timer.cancel()
        self._timer.join()
        if self._dup is not None:
            self._dup.close()


def _close(server, polite):
    """polite: say QUIT first. Never raises: the message is out, or the error is."""
    with suppress(Exception):
        if polite:
            server.quit()
    server.close()


class SMTPEmailer:
    """Email over SMTP with a login: a Gmail account and its app password, for the beta.

    Always TLS with the certificate checked: port 465 is TLS from the start, any other
    upgrades with STARTTLS, and a server that doesn't offer it is refused, so the password
    never crosses in the clear. Each send has SMTP_DEADLINE in all (_Deadline), the
    connect, TLS handshake and greeting included. One that runs out (or times out) once
    the message may have reached the server raises TimeoutError, recorded "unknown"
    (never retried: it may have gone); before that, SMTPUnavailable, recorded "failed"
    (retried by the sweep). After either or a failed connect, no connect is tried for
    SMTP_BREAK: those sends raise SMTPUnavailable too, spending no ceiling. Inside batch()
    the sends of one thread share a connection.

    Every send counts against SMTP_MAX_PER_DAY, and by its kind against shares of it:
    anything but "staff" PUBLIC_SHARE; "notice" NOTICE_SHARE too, "urgent" that and
    URGENT_SHARE, "sign-up" SIGNUP_SHARE; "sign-in" (a volunteer's link to their account)
    the public share only."""

    def __init__(self, host, port, username, password, sender, per_day):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.sender = sender
        self.context = ssl.create_default_context()
        self._local = threading.local()  # .server: the open batch connection, if any
        self._no_connect_until = 0.0  # time.monotonic(): SMTP_BREAK after a failure

        def share(name, part):
            return (
                ((f"email-smtp-{name}-day", max(1, int(per_day * part)), DAY),),
                "all",
            )

        whole = ((("email-smtp-day", per_day, DAY),), "all")
        public = share("public", PUBLIC_SHARE)
        notices = share("notices", NOTICE_SHARE)
        self.limits = {
            "staff": (whole,),
            "sign-in": (whole, public),
            "sign-up": (whole, public, share("signups", SIGNUP_SHARE)),
            "notice": (whole, public, notices),
            "urgent": (whole, public, notices, share("urgent", URGENT_SHARE)),
        }

    @contextmanager
    def batch(self):
        """The sends inside, in this thread, share one signed-in connection."""
        if hasattr(self._local, "server"):  # already in one
            yield
            return
        self._local.server = None
        try:
            yield
        finally:
            server = self._local.server
            del self._local.server
            if server is not None:
                _close(server, polite=True)

    def _open(self, deadline):
        """A connected socket, already TLS on SMTP_TLS_PORT, watched by `deadline`."""
        sock = socket.create_connection(
            (self.host, self.port), timeout=min(SMTP_TIMEOUT, deadline.left())
        )
        try:
            sock.settimeout(SMTP_TIMEOUT)
            deadline.watch(sock)
            if self.port == SMTP_TLS_PORT:
                return self.context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise
        return sock

    def _signed_in(self, deadline):
        """A connection that greeted us with 220, is TLS and is signed in. A failed
        connect or greeting stops connects for SMTP_BREAK, a refused password for
        SMTP_AUTH_BREAK."""
        server = smtplib.SMTP(timeout=SMTP_TIMEOUT)  # no host: it doesn't connect yet
        # the name STARTTLS checks the certificate against; smtplib sets it only from
        # the constructor's host, which would connect outside the deadline
        server._host = self.host
        try:
            server.sock = self._open(deadline)
            code, reply = server.getreply()
            if code != 220:
                raise smtplib.SMTPConnectError(code, reply)
        except BaseException:
            self._no_connect_until = time.monotonic() + SMTP_BREAK
            server.close()
            raise
        try:
            if self.port != SMTP_TLS_PORT:
                # SMTPNotSupportedError when the server doesn't offer it
                server.starttls(context=self.context)
            server.login(self.username, self.password)
        except BaseException as e:
            if isinstance(e, smtplib.SMTPAuthenticationError):
                self._no_connect_until = time.monotonic() + SMTP_AUTH_BREAK
            server.close()
            raise
        return server

    def send(self, to, subject, body, headers=None, kind="notice"):
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        for name, value in (headers or {}).items():
            message[name] = value
        message.set_content(body)
        if (
            time.monotonic() < self._no_connect_until
            and getattr(self._local, "server", None) is None
        ):
            raise SMTPUnavailable  # before the ceiling: nothing is spent on it
        # security.allow logs "rate limit reached: email-smtp-..." (docs/OPERATIONS.md)
        if not security.allow(*self.limits[kind]):
            raise EmailCeiling
        batched = hasattr(self._local, "server")
        server = self._local.server if batched else None
        deadline = _Deadline()
        sending = False  # once the message may have reached the server
        try:
            if server is None:
                server = self._signed_in(deadline)
            else:
                deadline.watch(server.sock)
            sending = True
            server.send_message(message)
        except BaseException as e:
            deadline.done()
            if batched:
                self._local.server = None  # the next send connects afresh
            if server is not None:
                _close(server, polite=False)
            if deadline.hit or isinstance(e, TimeoutError):
                self._no_connect_until = time.monotonic() + SMTP_BREAK
                if sending:  # it may have gone: "unknown", never retried
                    raise TimeoutError("SMTP deadline") from e
                raise SMTPUnavailable from e  # nothing went: "failed", retried
            raise
        deadline.done()
        if batched and not deadline.hit:
            self._local.server = server
        else:  # out of a batch, or the deadline shut it just after the send
            if batched:
                self._local.server = None
            _close(server, polite=not deadline.hit)


def batch():
    """One SMTP connection for the sends inside, in this thread; nothing for the others."""
    emailer = get_emailer()
    return emailer.batch() if hasattr(emailer, "batch") else nullcontext()


AFTER = "causematch.after"  # in a request's scope: what after() queued
# worker threads for what after() queued, apart from anyio's shared pool (40), so a slow
# mail server holds at most these and never the threads every sync route runs in
AFTER_LIMITER = anyio.CapacityLimiter(4)
# sign-in links' own threads, so a link (good for 15 minutes) never waits behind notices
LINK_LIMITER = anyio.CapacityLimiter(2)


def after(req, fn, *args, limiter=None):
    """Call fn(*args) off the request, in a worker thread under `limiter` (AFTER_LIMITER
    by default; RunAfter): an SMTP send can take seconds. For an HTTP request, once its
    response is out; for a websocket, at once beside its frames, which never wait on it
    (RuntimeError off the socket's event loop thread). Without RunAfter around the
    request, at once and inline."""
    queued = req.scope.get(AFTER)
    item = (fn, args, limiter or AFTER_LIMITER)
    if queued is None:
        fn(*args)
    elif isinstance(queued, list):  # an HTTP request's: after its response
        queued.append(item)
    else:  # a websocket's task group, and the thread its loop runs in
        tg, loop_thread = queued
        if threading.get_ident() != loop_thread:
            raise RuntimeError("after() on a websocket runs on its event loop only")
        tg.start_soon(_run_after, [item])


async def _run_after(queued):
    """Each of `queued` in a worker thread under its limiter, as a BackgroundTask would.
    Never raises for them; when the server stops first, logs how many may not have gone
    and lets the stop through."""
    for i, (fn, args, limiter) in enumerate(queued):
        try:
            await anyio.to_thread.run_sync(fn, *args, limiter=limiter)
        except Exception as e:  # noqa: BLE001  never the request's or the socket's error
            log.warning("a notice failed: %s", type(e).__name__)
        except BaseException:
            log.warning(
                "up to %d notices not sent: the server is stopping", len(queued) - i
            )
            raise


class RunAfter:
    """ASGI middleware for after(). An HTTP request's notices run once its response is
    out. A websocket's each start at once in a task group of the socket's own, so a frame
    never waits on one; the socket's end waits for them, as a response's end does, so a
    server stopping cancels them the same way (_run_after logs it)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            return await self._socket(scope, receive, send)
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        scope[AFTER] = queued = []
        try:
            await self.app(scope, receive, send)
        finally:
            await _run_after(queued)

    async def _socket(self, scope, receive, send):
        error = None
        async with anyio.create_task_group() as tg:
            scope[AFTER] = (tg, threading.get_ident())  # start_soon isn't thread-safe
            try:
                await self.app(scope, receive, send)
            except Exception as e:  # noqa: BLE001  raised below, once the notices are out
                # inside, it would cancel the notices and come out as an ExceptionGroup
                error = e
        if error is not None:
            raise error


def make_emailer(env=os.environ):
    """The fake where allowed and asked for; else Resend when EMAIL_API_KEY is set; else
    SMTP when it is configured (config.smtp). SMTP set halfway refuses to start even
    beside Resend, and so does no emailer at all."""
    if _fake(env, "CAUSEMATCH_FAKE_EMAIL"):
        return FakeEmailer()
    settings = smtp(env)
    if env.get("EMAIL_API_KEY"):
        if not env.get("EMAIL_FROM"):
            raise RuntimeError("Resend needs EMAIL_FROM beside EMAIL_API_KEY")
        return ResendEmailer(env["EMAIL_API_KEY"], env["EMAIL_FROM"])
    if settings:
        return SMTPEmailer(*settings, smtp_max_per_day(env))
    raise RuntimeError(
        "Email is not configured: set EMAIL_API_KEY and EMAIL_FROM (Resend), or "
        "SMTP_USERNAME and SMTP_PASSWORD (SMTP), or CAUSEMATCH_ENV=development with "
        "CAUSEMATCH_FAKE_EMAIL=1"
    )


_emailer = None


def get_emailer():
    global _emailer
    if _emailer is None:
        _emailer = make_emailer()
    return _emailer


def send_login_link(email, token):
    """The one place a coordinator's or admin's sign-in link leaves the app. Returns the
    link."""
    link = f"{public_base_url()}/org/login/verify?token={token}"
    get_emailer().send(
        email,
        "Your Causematch sign-in link",
        f"Sign in to Causematch:\n\n{link}\n\nThis link works once and expires in "
        "15 minutes. If you didn't ask for it, ignore this email.",
        kind="staff",
    )
    return link


def send_volunteer_link(email, token, new):
    """The one place a volunteer's emailed link leaves the app (/login): to sign in, or,
    for an address no account holds (new), to sign up. Returns the link."""
    link = f"{public_base_url()}/login/verify?token={token}"
    get_emailer().send(
        email,
        "Finish signing up for Causematch" if new else "Your Causematch sign-in link",
        ("Finish signing up for Causematch:" if new else "Sign in to Causematch:")
        + f"\n\n{link}\n\nThis link works once and expires in 15 minutes. If you "
        "didn't ask for it, ignore this email.",
        kind="sign-up" if new else "sign-in",
    )
    return link


def send_claim_approved(email, org_name):
    """An admin approved a claim. No token: it would be stale by the time it is read."""
    get_emailer().send(
        email,
        "You're approved on Causematch",
        f"You're approved to manage {org_name} on Causematch.\n\n"
        f"Sign in here: {public_base_url()}/org/login\n\n"
        "Enter this email address and we'll send you a sign-in link.\n\n"
        f"Questions? Write to {contact_email()}.",
    )


def send_claim_rejected(email):
    """Neutral on purpose: no reason, and none of what the requester typed."""
    get_emailer().send(
        email,
        "Your Causematch request",
        "Your request to manage an organization on Causematch was not approved.\n\n"
        f"If you think that is a mistake, write to {contact_email()}.",
    )


class SMSOptedOut(Exception):
    """The recipient replied STOP."""


def mask(phone):
    """A phone number for logs: never the whole number."""
    return "***" + phone[-4:]


def mask_email(addr):
    """An email address for logs: pat@demo.causematch.test -> p***@d***.test."""
    local, _, domain = addr.partition("@")
    host, dot, tld = domain.rpartition(".") if "." in domain else (domain, "", "")
    return f"{local[:1]}***@{host[:1]}***{dot}{tld}"


class FakeSMS:
    """Test, local-dev and preview sender: sends nothing, records (phone, text)."""

    def __init__(self):
        self.sent = []

    def send(self, phone, text):
        _record(self.sent, (phone, text))
        log.warning("FAKE SMS to %s: %s", mask(phone), text)


class TwilioSMS:
    def __init__(self, account_sid, auth_token, from_number):
        self.from_number = from_number
        self.client = httpx.Client(
            base_url=SMS_URL.format(sid=account_sid),
            auth=(account_sid, auth_token),
            timeout=10,
        )

    def send(self, phone, text):
        r = self.client.post(
            "/Messages.json", data={"To": phone, "From": self.from_number, "Body": text}
        )
        if r.status_code == 400:
            try:
                code = r.json().get("code")
            except ValueError:
                code = None
            if code == TWILIO_STOPPED:
                raise SMSOptedOut
        r.raise_for_status()


def make_sms(env=os.environ):
    """The text sender, or None when Twilio is not configured: volunteers get email."""
    if _fake(env, "CAUSEMATCH_FAKE_SMS"):
        return FakeSMS()
    keys = _twilio(
        env, ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
    )
    return keys and TwilioSMS(*keys)


_sms = _UNSET


def get_sms():
    global _sms
    if _sms is _UNSET:
        _sms = make_sms()
    return _sms


def texts_on():
    """Whether the app can text anyone (Twilio configured, or the fake). Off, as in
    production, every notice is an email and no page speaks of texts or numbers."""
    return get_sms() is not None


def channel(uid):
    """How a volunteer hears from us: "sms" to a number that may be texted (and only while
    Twilio is configured), else "email" when their live profile has an address, else None
    (the app only)."""
    if get_sms() is not None and data.sms_number(uid):
        return "sms"
    if data.urgent_email(uid):
        return "email"
    return None


def now():
    """The clock every window and reminder reads; tests replace it."""
    return datetime.now(UTC)


def _deliver(kind, key, recipient_id, send, window=None, opt_out=None, gate=None):
    """Run `send` once for `key` (or once per `window` seconds), recording the outcome.

    Never raises. Call it from a worker thread and after the DB commit: it makes a network
    call. A claim that is never finished stays `pending` and is not retried (a lost send is
    better than a double text). A timeout is recorded `unknown` and not retried either: the
    provider may have delivered. When `gate` says no (the SMS ceiling), nothing is sent and
    the send is recorded `deferred`, for the hourly sweep to try again.
    """
    try:
        at = now()
        cutoff = (
            (at - timedelta(seconds=window)).isoformat(timespec="seconds")
            if window
            else None
        )
        nid = data.notification_claim(
            kind,
            key,
            recipient_id,
            at.isoformat(timespec="seconds"),
            cutoff,
            MAX_ATTEMPTS,
        )
        if nid is None:
            return False
        if gate is not None and not gate():
            data.notification_done(nid, "deferred")
            return False
        try:
            send()
        except SMSOptedOut:
            if opt_out:
                opt_out()
            status = "skipped"
        except (
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.RemoteProtocolError,
            TimeoutError,  # SMTP's: the server may have taken the message
        ) as e:
            log.warning("%s notification outcome unknown: %s", kind, type(e).__name__)
            status = "unknown"
        except Exception as e:  # noqa: BLE001  a failed notice never fails the request
            log.warning("%s notification failed: %s", kind, type(e).__name__)
            status = "failed"
        else:
            status = "sent"
        data.notification_done(nid, status)
    except Exception as e:  # noqa: BLE001  recording trouble never fails the request either
        log.warning("%s notification not recorded: %s", kind, type(e).__name__)
        return False
    return status == "sent"


def _text(kind, key, uid, body, window=None, urgent=False):
    """urgent: also under the urgent calls' own ceiling (security.sms_urgent)."""
    phone = data.sms_number(uid)
    sms = get_sms()
    if phone is None or sms is None:
        return False
    limits = [(security.sms_all(), "all")]
    if urgent:
        limits.append((security.sms_urgent(), "urgent"))
    return _deliver(
        kind,
        key,
        uid,
        lambda: sms.send(phone, body),
        window,
        opt_out=lambda: data.sms_opted_out(uid),
        gate=lambda: security.allow(*limits),
    )


def _tell(kind, key, uid, text, subject, body, window=None):
    """Text the volunteer, or email them when they have no number to text (channel). One
    key either way, so the once-per-event and window rules hold across both."""
    how = channel(uid)
    if how == "sms":
        return _text(kind, key, uid, text, window)
    if how != "email":
        return False
    to = data.urgent_email(uid)
    return _deliver(
        kind, key, uid, lambda: get_emailer().send(to, subject, body), window
    )


def on_accepted(match_id):
    """Called after a coordinator accepts. Texts (or emails) the volunteer, once per
    match."""
    info = data.match_notice(match_id)
    if (
        info and info["status"] == "accepted" and not info["silent"]
    ):  # a block withdraws it
        chat = f"{public_base_url()}/chat/{match_id}"
        return _tell(
            "accepted",
            f"accepted:{match_id}",
            info["volunteer_id"],
            f"{info['org']} said yes to {info['opening']}! Say hi: {chat}",
            f"You're matched with {info['org']}",
            f"{info['org']} said yes to {info['opening']}!\n\nSay hi: {chat}",
        )


def on_new_message(match_id, sender_id):
    """Called after a chat message is saved. Texts (or emails) the volunteer about a
    coordinator's message (never its text), at most once per match per 30 minutes."""
    from app.routes.chat import volunteer_online  # chat imports this module

    info = data.match_notice(match_id)
    if (
        not info
        or info["status"] != "accepted"  # blocked or withdrawn: no text
        or info["silent"]
        or info["volunteer_id"] == sender_id
        or volunteer_online(match_id, info["volunteer_id"])
    ):
        return
    return _message_text(match_id, info)


def _message_text(match_id, info):
    chat = f"{public_base_url()}/chat/{match_id}"
    return _tell(
        "message",
        f"msg:{match_id}",
        info["volunteer_id"],
        f"New message from {info['org']} on Causematch: {chat}",
        f"New message from {info['org']}",
        f"{info['org']} sent you a message on Causematch.\n\nRead it: {chat}",
        MESSAGE_WINDOW,
    )


def on_new_like(match_id):
    """Called after a volunteer likes an opening. Emails the org's coordinators, at most
    once per org per 15 minutes."""
    info = data.match_notice(match_id)
    if not info or info["status"] != "pending" or info["silent"]:
        return
    org_id = info["org_id"]
    emails = data.org_coordinator_emails(org_id)
    if not emails:
        return

    def send():
        n = data.org_pending_count(org_id)
        waiting = "1 volunteer is" if n == 1 else f"{n} volunteers are"
        with batch():
            for to in emails:
                get_emailer().send(
                    to,
                    "New volunteer interest on Causematch",
                    f"{waiting} waiting to hear from {info['org']}.\n\n"
                    f"{public_base_url()}/org/inbox",
                )

    _deliver("likes", f"likes:{org_id}", org_id, send, LIKES_WINDOW)


def on_auto_accepted(match_id):
    """Called after a like an urgent call accepted by itself: emails the org's
    coordinators, once per match. No name: the inbox shows who."""
    info = data.auto_accept_notice(match_id)
    if not info:
        return
    emails = data.org_coordinator_emails(info["org_id"])
    if not emails:
        return

    def send():
        with batch():
            for to in emails:
                get_emailer().send(
                    to,
                    f"A volunteer is in for {info['opening']}, {info['when']}",
                    f"A volunteer said yes to your last-minute call for {info['opening']}, "
                    f"{info['when']}, and was accepted automatically ({info['found']} "
                    f"of {info['needed']} found). Their shift is booked.\n\n"
                    f"Who and chat: {public_base_url()}/org/inbox",
                )

    _deliver("auto", f"auto:{match_id}", info["org_id"], send)


_when = start_text  # "Sat Oct 18, 9:00 AM"


def _email_org(kind, key, info, subject, body):
    """Email the org of `info` (a match_notice) at once, once per `key`; nothing for a
    silent match (blind or a deactivated org)."""
    if not info or info["silent"]:
        return
    emails = data.org_coordinator_emails(info["org_id"])
    if not emails:
        return

    def send():
        with batch():
            for to in emails:
                get_emailer().send(to, subject, body)

    _deliver(kind, key, info["org_id"], send)


def on_shift_cancelled(shift_id, done):
    """Called after a volunteer cancelled one shift (data.cancel_my_shift's `done`):
    emails the org's coordinators, with the reason given and whether an urgent call went
    out. No name: the chat link shows who."""
    info = data.shift_notice(shift_id)
    if not info:
        return
    base = public_base_url()
    when = _when(info["starts_at"])
    lines = [
        f"A volunteer can't make their {when} shift for {info['opening']}. "
        + (
            "It was their only shift, so they gave up their spot: it's open again."
            if done["ended"]
            else f"They're still matched with {info['org']}; only this shift is "
            "cancelled."
        )
    ]
    if done["reason"]:
        lines.append(f"Their reason: {done['reason']}")
    call = done["call"]
    if call:
        how = (
            "the first one to say yes is booked automatically"
            if call.auto_accept
            else "you decide who joins"
        )
        lines.append(
            "It was less than 48 hours away, so we posted a last-minute call for 1 person "
            f"at that time to volunteers nearby ({how}). Follow it: {base}/org"
        )
    elif done["can_call"]:
        lines.append(
            "It's less than 48 hours away. Short of people? Post a last-minute call: "
            f"{base}/org"
        )
    elif done["late"] and not done["ended"]:
        lines.append(
            "It's less than 48 hours away. Reply to them in chat to arrange cover."
        )
    chat = f"{base}/org/chat/{info['match_id']}"
    lines.append(
        f"Who: {chat}" if done["ended"] else f"Who, and a way to reply: {chat}"
    )
    _email_org(
        "cancel",
        f"cancel:{shift_id}",
        info,
        f"A volunteer can't make {info['opening']}, {when}",
        "\n\n".join(lines),
    )


def on_withdrawn(match_id, was):
    """Called after a volunteer withdrew from a match that was `was`: emails the org's
    coordinators, once per match, when it was accepted (a pending like the inbox drops
    quietly). No name: the inbox shows who."""
    info = data.match_notice(match_id)
    if was != "accepted" or not info or info["status"] != "withdrawn":
        return
    text = (
        f"A volunteer withdrew from {info['opening']} before you confirmed their screening."
        if info["screening_pending"]
        else f"A volunteer withdrew from {info['opening']}. Their upcoming shifts are "
        "cancelled and their spot is open again."
    )
    _email_org(
        "withdrawn",
        f"withdrawn:{match_id}",
        info,
        f"A volunteer withdrew from {info['opening']}",
        f"{text}\n\nWho: {public_base_url()}/org/inbox",
    )


def send_due_reminders():
    """The hourly sweep (_send_due_reminders), its emails over one SMTP connection."""
    with batch():
        return _send_due_reminders()


def _send_due_reminders():
    """Text the volunteer of every scheduled shift starting 23 to 25 hours from now, once
    per shift, and retry failed "you're matched" texts and texts the SMS ceiling deferred.
    How many were sent.

    At most one reminder per volunteer and MAX_REMINDERS_PER_SWEEP in all per sweep: a
    coordinator adding dozens of shifts can't turn into dozens of texts at once. A held-back
    reminder goes in a later sweep while its shift is still in the window."""
    t = now()
    first, last = (
        (t + timedelta(hours=h)).astimezone(LA).strftime(_LOCAL_FORMAT)
        for h in (23, 25)
    )
    sent = 0
    for match_id in data.failed_accepts(
        MAX_ATTEMPTS
    ):  # the "you're matched" texts that failed
        info = data.match_notice(match_id)
        if info and info["status"] == "accepted":
            sent += bool(on_accepted(match_id))
    for match_id in data.deferred_messages():
        info = data.match_notice(match_id)
        if info and info["status"] == "accepted" and not info["silent"]:
            sent += bool(_message_text(match_id, info))
    reminded = set()  # volunteers texted a reminder in this sweep
    for shift_id, volunteer_id in data.due_reminders(first, last):
        if volunteer_id in reminded or len(reminded) >= MAX_REMINDERS_PER_SWEEP:
            continue
        info = data.shift_notice(shift_id)
        if not info:
            continue
        when = _when(info["starts_at"])
        confirm = f"{public_base_url()}/shifts/{shift_id}/confirm"
        if _tell(
            "reminder",
            f"reminder:{shift_id}",
            volunteer_id,
            f"Reminder: {info['opening']} with {info['org']} is on {when}. "
            f"Still coming? {confirm}",
            f"Reminder: {info['opening']}, {when}",
            f"{info['opening']} with {info['org']} is on {when}.\n\n"
            f"Still coming? Let them know: {confirm}",
        ):
            reminded.add(volunteer_id)
    return sent + len(reminded)


def send_urgent_waves():
    """The 10-minute sweep (_send_urgent_waves), its emails over one SMTP connection."""
    with batch():
        return _send_urgent_waves()


def _send_urgent_waves():
    """Cloud Scheduler calls /tasks/urgent every 10 minutes: send each urgent call's due
    wave (data.urgent_waves_due) to the volunteers data.urgent_recipients picks, once per
    volunteer and call. A wave is claimed only once it has someone to reach, so one that
    falls in everyone's quiet hours waits. A text counts against the SMS ceiling and the
    urgent calls' own half of it; one over either is recorded `deferred` and not retried
    by the hourly sweep (the call may be over by then), though wave 2 may try that
    volunteer again. An email carries a signed unsubscribe link and List-Unsubscribe.
    How many were sent."""
    at = now()
    sent = 0
    for call_id, done, size in data.urgent_waves_due(at):
        info = data.urgent_notice(call_id, at)
        recipients = data.urgent_recipients(call_id, size, at) if info else []
        # a wave that would reach no one (all in quiet hours, say) isn't spent
        if not recipients or not data.urgent_claim_wave(call_id, done, at):
            continue
        link = f"{public_base_url()}/deck?card={info['opening_id']}"
        people = "1 more person" if info["left"] == 1 else f"{info['left']} more people"
        text = (
            f"Causematch: {sms_safe(info['org'])} needs {people} {info['when']} for "
            f"{sms_safe(info['opening'])}. Interested? {link} (Turn these off in "
            "Preferences. Reply STOP to end all texts.)"
        )
        subject = f"Last-minute: {info['org']} needs {people} {info['when']}"
        for uid, how in recipients:
            key = f"urgent:{call_id}:{uid}"
            if how == "sms" and get_sms() is not None:  # else by email
                sent += bool(_text("urgent", key, uid, text, urgent=True))
                continue
            email = data.urgent_email(uid)
            if not email:
                continue
            off = f"{public_base_url()}/urgent/off/{data.urgent_off_key(uid)}"
            body = (
                f"{info['org']} is short of people for {info['opening']}, "
                f"{info['when']}, and needs {people}.\n\nInterested? Have a look: "
                f"{link}\n\nYou get these because you turned on last-minute calls. "
                f"To stop them: {off}"
            )
            sent += bool(
                _deliver(
                    "urgent",
                    key,
                    uid,
                    lambda to=email, s=subject, b=body, off=off: get_emailer().send(
                        to, s, b, {"List-Unsubscribe": f"<{off}>"}, kind="urgent"
                    ),
                )
            )
    return sent


SMS_TITLE_MAX = 60
# a web address in an org's own words: a text must carry only our link
_URLISH = re.compile(
    r"\S*(?:://|www\.)\S*|\S*\b[\w-]+\.(?:[a-z]{2,24})\b(?:/\S*)?", re.IGNORECASE
)


def sms_safe(text):
    """An org's name or title as a text quotes it: no web addresses, no emoji (they turn
    the whole text into UCS-2, 2 to 3 times the segments), at most SMS_TITLE_MAX
    characters."""
    text = _URLISH.sub("", text)
    text = "".join(
        c for c in text if ord(c) <= 0xFFFF and unicodedata.category(c) != "So"
    )
    text = " ".join(text.split())
    if len(text) > SMS_TITLE_MAX:
        text = text[: SMS_TITLE_MAX - 3].rstrip() + "..."  # "…" isn't GSM-7
    return text or "an opening"
