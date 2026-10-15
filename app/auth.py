import logging
import re

from fasthtml.common import Beforeware, Redirect

from app import data
from app.notify import is_production

log = logging.getLogger("causematch")

# Dev-only fallback so a fresh checkout runs; production refuses to start without a real secret.
DEV_SECRET = "dev-only-secret-do-not-use-in-production"

PUBLIC = [
    r"/login.*",
    r"/favicon\.ico",
    r"/static/.*",
    r".*\.(css|js|png|jpg|jpeg|gif|svg|ico|webp|webmanifest|woff2?)",
]
# Signed in but not yet onboarded: these stay reachable.
ONBOARDING_OK = ("/onboarding", "/logout")

BAD_PHONE = "Enter a US mobile number, like (310) 555-0123."


def session_secret(env):
    secret = env.get("SESSION_SECRET")
    if secret:
        return secret
    if is_production(env):
        raise RuntimeError("SESSION_SECRET must be set when CAUSEMATCH_ENV=production")
    log.warning("SESSION_SECRET is not set: using an insecure development secret")
    return DEV_SECRET


def normalize_phone(raw):
    """E.164 for a US number given as 10 digits or +1 and 10 digits; None otherwise."""
    raw = (raw or "").strip()
    digits = re.sub(r"[\s().-]", "", raw)
    if re.fullmatch(r"\+1\d{10}", digits):
        digits = digits[2:]
    elif not re.fullmatch(r"\d{10}", digits):
        return None
    if (
        digits[0] in "01" or digits[3] in "01"
    ):  # NANP: area and exchange codes start 2-9
        return None
    return f"+1{digits}"


def check_session(req, sess):
    profile = data.get_profile(sess["uid"]) if sess.get("uid") else None
    if not profile:
        sess.pop("uid", None)
        return Redirect("/login")
    req.scope["uid"] = profile.id
    if not profile.onboarded_at and req.url.path not in ONBOARDING_OK:
        return Redirect("/onboarding")


beforeware = Beforeware(check_session, skip=PUBLIC)
