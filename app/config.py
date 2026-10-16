import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
MIN_SECRET = 32  # characters, for every secret a production start requires
MIN_PREVIEW_PASSWORD = 16  # characters


def strong_secret(name, value, env):
    """value, unless production and it is shorter than MIN_SECRET."""
    if is_production(env) and len(value) < MIN_SECRET:
        raise RuntimeError(f"{name} must be at least {MIN_SECRET} random characters")
    return value


def is_production(env=os.environ):
    """Production's rules apply: CAUSEMATCH_ENV=production or preview, or Cloud Run
    (K_SERVICE), whatever CAUSEMATCH_ENV says. A preview is a production deploy behind a
    password: is_preview says what it may do on top."""
    return env.get("CAUSEMATCH_ENV") in ("production", "preview") or "K_SERVICE" in env


def is_preview(env=os.environ):
    """CAUSEMATCH_ENV=preview with a PREVIEW_PASSWORD of at least MIN_PREVIEW_PASSWORD
    characters: the password-protected demo deploy. On top of production's rules every
    request needs that password (security.PreviewGate), and fake SMS and email, the
    banner and the demo seed are allowed. Without the password it is not a preview, and
    require_env refuses to start."""
    return (
        env.get("CAUSEMATCH_ENV") == "preview"
        and len(env.get("PREVIEW_PASSWORD") or "") >= MIN_PREVIEW_PASSWORD
    )


def fakes_allowed(env=os.environ):
    """Fake SMS and email (every code 000000, links in the log): development, test and a
    preview; never production."""
    return is_dev(env) or is_preview(env)


def demo_matches(env=os.environ):
    """CAUSEMATCH_DEMO_MATCHES=1 (scripts/demo.py and the preview deploy set it): signing in
    with the demo number gives each visitor a fresh copy of the demo volunteer, and an org
    run by a stand-in demo coordinator says yes to a like a few seconds later
    (data.demo_accept_due). Allowed where the fakes are (development, test, a preview);
    anywhere else it raises, so production with it set refuses to start (main.py)."""
    if env.get("CAUSEMATCH_DEMO_MATCHES") != "1":
        return False
    if not fakes_allowed(env):
        raise RuntimeError(
            "CAUSEMATCH_DEMO_MATCHES=1 needs CAUSEMATCH_ENV=development, test or preview"
        )
    return True


def is_dev(env=os.environ):
    """Development is something you declare; an unset environment is not development."""
    return env.get("CAUSEMATCH_ENV") in ("development", "test") and not is_production(
        env
    )


def public_base_url(env=os.environ):
    """Origin used in emailed links. Always configured, never taken from a request."""
    url = (env.get("PUBLIC_BASE_URL") or "").rstrip("/")
    if not url:
        if is_dev(env):
            return "http://localhost:5001"
        raise RuntimeError("PUBLIC_BASE_URL must be set (e.g. https://causematch.org)")
    if not url.startswith(("https://", "http://")) or (
        is_production(env) and not url.startswith("https://")
    ):
        raise RuntimeError("PUBLIC_BASE_URL must be an https:// origin in production")
    u = urlsplit(url)
    if u.path or u.query or u.fragment:
        raise RuntimeError("PUBLIC_BASE_URL must be an origin only: no path or query")
    return url


def require_env(env=os.environ):
    """Refuse to start unless CAUSEMATCH_ENV is declared (Cloud Run counts as production),
    or when a preview has no strong PREVIEW_PASSWORD."""
    if env.get("CAUSEMATCH_ENV") == "preview":
        if not is_preview(env):
            raise RuntimeError(
                f"PREVIEW_PASSWORD must be at least {MIN_PREVIEW_PASSWORD} characters "
                "for CAUSEMATCH_ENV=preview"
            )
        return
    if "K_SERVICE" in env:
        return
    if env.get("CAUSEMATCH_ENV") not in ("development", "test", "production"):
        raise RuntimeError(
            "CAUSEMATCH_ENV must be set to development, test, preview or production"
        )


def tasks_token(env=os.environ):
    """Bearer token for the scheduler's /tasks/* calls. Required in production; elsewhere
    "" means those calls are always refused."""
    token = env.get("TASKS_TOKEN") or ""
    if not token and is_production(env):
        raise RuntimeError("TASKS_TOKEN must be set in production")
    return strong_secret("TASKS_TOKEN", token, env)


def contact_email(env=os.environ):
    """Where the privacy page tells people to write. Required in production."""
    addr = (env.get("CONTACT_EMAIL") or "").strip()
    if not addr:
        if is_dev(env):
            return "hello@causematch.test"
        raise RuntimeError("CONTACT_EMAIL must be set (the address on /privacy)")
    return addr


def phone_hash_key(env=os.environ):
    """Key for the hash kept of a deleted account's phone (see data.delete_account). Required
    in production; losing or changing it only means old blocks stop following a number."""
    key = env.get("PHONE_HASH_KEY") or ""
    if not key:
        if is_dev(env):
            return "dev-only-phone-hash-key"
        raise RuntimeError("PHONE_HASH_KEY must be set (any long random string)")
    return strong_secret("PHONE_HASH_KEY", key, env)


def db_path(env=os.environ):
    """The SQLite file: CAUSEMATCH_DB, else data/causematch.db in the repo (not the cwd)."""
    return Path(env.get("CAUSEMATCH_DB") or ROOT / "data" / "causematch.db").resolve()


def trusted_proxy_hops(env=os.environ):
    """How many addresses the proxies in front of the app append to X-Forwarded-For; the
    client is the one that many places from the right (app/security.py, client_ip).

    0 (the default off Cloud Run) ignores the header and uses the socket peer. On Cloud Run
    it must be set, because the right value depends on what is in front: 1 for the plain
    *.run.app URL (Google's front end appends the client), 2 behind an external
    Application Load Balancer (it appends the client, then its own address). Too high a
    value lets a client choose its own address; too low makes every client one.
    """
    raw = (env.get("TRUSTED_PROXY_HOPS") or "").strip()
    if not raw:
        if "K_SERVICE" in env:
            raise RuntimeError(
                "TRUSTED_PROXY_HOPS must be set on Cloud Run: 1 for the run.app URL, "
                "2 behind an external load balancer"
            )
        return 0
    if not raw.isdigit():
        raise RuntimeError("TRUSTED_PROXY_HOPS must be a whole number (0, 1, 2, ...)")
    return int(raw)


def sms_ceilings(env=os.environ):
    """(per hour, per day): every text the app sends, codes and notifications together.
    SMS_MAX_PER_HOUR and SMS_MAX_PER_DAY; defaults 100 and 500."""
    out = []
    for name, default in (("SMS_MAX_PER_HOUR", 100), ("SMS_MAX_PER_DAY", 500)):
        raw = (env.get(name) or "").strip() or str(default)
        if not raw.isdigit() or int(raw) < 1:
            raise RuntimeError(f"{name} must be a positive whole number")
        out.append(int(raw))
    return tuple(out)


def demo_banner(env=os.environ):
    """CAUSEMATCH_DEMO_BANNER=1 (scripts/demo.py --seed-file, which loads real nonprofits
    with sample openings): every page says they are samples. Local demos and a preview
    (always on there) only: production refuses it, so a real deploy never shows sample
    openings as a demo."""
    if is_preview(env):
        return True
    if env.get("CAUSEMATCH_DEMO_BANNER") != "1":
        return False
    if is_production(env):
        raise RuntimeError("CAUSEMATCH_DEMO_BANNER is for local demos, not production")
    return True
