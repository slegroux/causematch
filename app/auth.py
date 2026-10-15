import logging
import re

from fasthtml.common import Beforeware, Redirect

from app import data
from app.config import is_dev

log = logging.getLogger("causematch")

# Used only when CAUSEMATCH_ENV is explicitly development or test and SESSION_SECRET is unset.
DEV_SECRET = "dev-only-secret-do-not-use-in-production"
SESSION_MAX_AGE = 30 * 24 * 3600

PUBLIC = [r"/login", r"/login/send", r"/login/verify", r"/favicon\.ico", r"/static/.*"]
# Signed in but not yet onboarded: these stay reachable.
ONBOARDING_OK = ("/onboarding", "/logout")

BAD_PHONE = "Enter a US mobile number, like (310) 555-0123."


def session_secret(env):
    secret = env.get("SESSION_SECRET")
    if secret:
        return secret
    if not is_dev(env):
        raise RuntimeError(
            "SESSION_SECRET must be set (or CAUSEMATCH_ENV=development for local work)"
        )
    log.warning("SESSION_SECRET is not set: using an insecure development secret")
    return DEV_SECRET


def normalize_phone(raw):
    """E.164 for a US number given as 10 digits, 1 and 10 digits, or +1 and 10 digits."""
    digits = re.sub(r"[\s().-]", "", (raw or "").strip())
    m = re.fullmatch(r"(?:\+?1)?([0-9]{10})", digits)
    if not m:
        return None
    digits = m.group(1)
    if (
        digits[0] in "01" or digits[3] in "01"
    ):  # NANP: area and exchange codes start 2-9
        return None
    return f"+1{digits}"


def check_session(req, sess):
    profile = data.get_profile(sess["uid"]) if sess.get("uid") else None
    if not profile or sess.get("sv") != (profile.session_version or 1):
        sess.pop("uid", None)
        return Redirect("/login")
    req.scope["uid"] = profile.id
    if not profile.onboarded_at and req.url.path not in ONBOARDING_OK:
        return Redirect("/onboarding")


beforeware = Beforeware(check_session, skip=PUBLIC)
