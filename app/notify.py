import logging
import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app import data, security
from app.config import contact_email, fakes_allowed, public_base_url

log = logging.getLogger("causematch")

FAKE_CODE = "000000"
SMS_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}"
TWILIO_STOPPED = 21610  # the recipient replied STOP
MESSAGE_WINDOW = 30 * 60  # seconds: at most one new-message text per match per window
LIKES_WINDOW = 15 * 60  # seconds: at most one new-likes email per org per window
MAX_ATTEMPTS = 5  # tries for one send before it is left failed
MAX_REMINDERS_PER_SWEEP = 100  # the rest wait for the next hourly sweep
LA = ZoneInfo("America/Los_Angeles")
_LOCAL_FORMAT = "%Y-%m-%dT%H:%M"
VERIFY_URL = "https://verify.twilio.com/v2/Services/{sid}"
FAKE_KEPT = 1000  # the most recent sends a fake keeps: a preview runs for days


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


def make_verifier(env=os.environ):
    if env.get("CAUSEMATCH_FAKE_SMS") == "1":
        if not fakes_allowed(env):
            raise RuntimeError(
                "CAUSEMATCH_FAKE_SMS=1 needs CAUSEMATCH_ENV=development, test or preview"
            )
        return FakeVerifier()
    keys = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SID")
    if not all(env.get(k) for k in keys):
        raise RuntimeError(
            "Twilio is not configured: set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and "
            "TWILIO_VERIFY_SID (or CAUSEMATCH_ENV=development with CAUSEMATCH_FAKE_SMS=1)"
        )
    return TwilioVerifier(*(env[k] for k in keys))


_verifier = None


def get_verifier():
    global _verifier
    if _verifier is None:
        _verifier = make_verifier()
    return _verifier


def send_code(phone):
    """The one place a sign-in code leaves the app, so rate limiting can wrap it."""
    get_verifier().send(phone)


def check_code(phone, code):
    return get_verifier().check(phone, code)


class FakeEmailer:
    """Test, local-dev and preview emailer: sends nothing, records messages, logs them
    (the address masked)."""

    def __init__(self):
        self.sent = []

    def send(self, to, subject, body):
        _record(self.sent, (to, subject, body))
        log.warning("FAKE EMAIL to %s: %s\n%s", mask_email(to), subject, body)


class ResendEmailer:
    def __init__(self, api_key, sender):
        self.sender = sender
        self.client = httpx.Client(
            base_url="https://api.resend.com",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )

    def send(self, to, subject, body):
        self.client.post(
            "/emails",
            json={"from": self.sender, "to": [to], "subject": subject, "text": body},
        ).raise_for_status()


def make_emailer(env=os.environ):
    if env.get("CAUSEMATCH_FAKE_EMAIL") == "1":
        if not fakes_allowed(env):
            raise RuntimeError(
                "CAUSEMATCH_FAKE_EMAIL=1 needs CAUSEMATCH_ENV=development, test or preview"
            )
        return FakeEmailer()
    if not (env.get("EMAIL_API_KEY") and env.get("EMAIL_FROM")):
        raise RuntimeError(
            "Email is not configured: set EMAIL_API_KEY and EMAIL_FROM "
            "(or CAUSEMATCH_ENV=development with CAUSEMATCH_FAKE_EMAIL=1)"
        )
    return ResendEmailer(env["EMAIL_API_KEY"], env["EMAIL_FROM"])


_emailer = None


def get_emailer():
    global _emailer
    if _emailer is None:
        _emailer = make_emailer()
    return _emailer


def send_login_link(email, token):
    """The one place a coordinator sign-in link leaves the app. Returns the link."""
    link = f"{public_base_url()}/org/login/verify?token={token}"
    get_emailer().send(
        email,
        "Your Causematch sign-in link",
        f"Sign in to Causematch:\n\n{link}\n\nThis link works once and expires in "
        "15 minutes. If you didn't ask for it, ignore this email.",
    )
    return link


def send_claim_approved(email, org_name):
    """An admin approved a claim. No token: it would be stale by the time it is read."""
    get_emailer().send(
        email,
        "Your Causematch request was approved",
        f"Your request to manage {org_name} on Causematch was approved.\n\n"
        f"Sign in any time at {public_base_url()}/org/login: enter this email address "
        "and we'll send you a sign-in link.\n\n"
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
    if env.get("CAUSEMATCH_FAKE_SMS") == "1":
        if not fakes_allowed(env):
            raise RuntimeError(
                "CAUSEMATCH_FAKE_SMS=1 needs CAUSEMATCH_ENV=development, test or preview"
            )
        return FakeSMS()
    keys = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
    if not all(env.get(k) for k in keys):
        raise RuntimeError(
            "Twilio is not configured: set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and "
            "TWILIO_FROM_NUMBER (or CAUSEMATCH_ENV=development with CAUSEMATCH_FAKE_SMS=1)"
        )
    return TwilioSMS(*(env[k] for k in keys))


_sms = None


def get_sms():
    global _sms
    if _sms is None:
        _sms = make_sms()
    return _sms


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
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as e:
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


def _text(kind, key, uid, body, window=None):
    phone = data.sms_number(uid)
    if phone is None:
        return False
    return _deliver(
        kind,
        key,
        uid,
        lambda: get_sms().send(phone, body),
        window,
        opt_out=lambda: data.sms_opted_out(uid),
        gate=lambda: security.allow((security.sms_all(), "all")),
    )


def on_accepted(match_id):
    """Called after a coordinator accepts. Texts the volunteer, once per match."""
    info = data.match_notice(match_id)
    if (
        info and info["status"] == "accepted" and not info["silent"]
    ):  # a block withdraws it
        return _text(
            "accepted",
            f"accepted:{match_id}",
            info["volunteer_id"],
            f"{info['org']} said yes to {info['opening']}! "
            f"Chat and details: {public_base_url()}/matches",
        )


def on_new_message(match_id, sender_id):
    """Called after a chat message is saved. Texts the volunteer about a coordinator's
    message (never its text), at most once per match per 30 minutes."""
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
    return _text(
        "message",
        f"msg:{match_id}",
        info["volunteer_id"],
        f"New message from {info['org']} on Causematch: {public_base_url()}/chat/{match_id}",
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
        for to in emails:
            get_emailer().send(
                to,
                "New volunteer interest on Causematch",
                f"{waiting} waiting to hear from {info['org']}.\n\n"
                f"{public_base_url()}/org/inbox",
            )

    _deliver("likes", f"likes:{org_id}", org_id, send, LIKES_WINDOW)


def send_due_reminders():
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
        start = datetime.strptime(info["starts_at"], _LOCAL_FORMAT)
        if _text(
            "reminder",
            f"reminder:{shift_id}",
            volunteer_id,
            f"Reminder: {info['opening']} with {info['org']} is on "
            f"{start.strftime('%a %b %-d, %-I:%M %p')}. Details: {public_base_url()}/matches",
        ):
            reminded.add(volunteer_id)
    return sent + len(reminded)
