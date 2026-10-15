import os

import httpx

from app.config import is_dev

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
