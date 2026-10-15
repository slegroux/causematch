import logging
import os

import httpx

from app.config import is_dev, public_base_url

log = logging.getLogger("causematch")

FAKE_CODE = "000000"
VERIFY_URL = "https://verify.twilio.com/v2/Services/{sid}"


class FakeVerifier:
    """Test and local-dev verifier: sends nothing, accepts 000000."""

    def __init__(self):
        self.sent = []

    def send(self, phone):
        self.sent.append((phone, FAKE_CODE))

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
        if not is_dev(env):
            raise RuntimeError(
                "CAUSEMATCH_FAKE_SMS=1 needs CAUSEMATCH_ENV=development or test"
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
    """Test and local-dev emailer: sends nothing, records messages, logs them."""

    def __init__(self):
        self.sent = []

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))
        log.warning("FAKE EMAIL to %s: %s\n%s", to, subject, body)


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
        if not is_dev(env):
            raise RuntimeError(
                "CAUSEMATCH_FAKE_EMAIL=1 needs CAUSEMATCH_ENV=development or test"
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
    """The one place a coordinator sign-in link leaves the app."""
    link = f"{public_base_url()}/org/login/verify?token={token}"
    get_emailer().send(
        email,
        "Your Causematch sign-in link",
        f"Sign in to Causematch:\n\n{link}\n\nThis link works once and expires in "
        "15 minutes. If you didn't ask for it, ignore this email.",
    )


def on_new_message(match_id, sender_id):
    """Called after a chat message is saved. Item 7 texts the other side if they are offline."""


def on_shift_scheduled(shift_id):
    """Called after a shift is scheduled. Item 7 texts the volunteer 24 hours before it."""
