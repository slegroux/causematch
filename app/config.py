import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def is_production(env=os.environ):
    """Cloud Run (K_SERVICE) counts as production whatever CAUSEMATCH_ENV says."""
    return env.get("CAUSEMATCH_ENV") == "production" or "K_SERVICE" in env


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
    return url


def require_env(env=os.environ):
    """Refuse to start unless CAUSEMATCH_ENV is declared (Cloud Run counts as production)."""
    if "K_SERVICE" in env:
        return
    if env.get("CAUSEMATCH_ENV") not in ("development", "test", "production"):
        raise RuntimeError(
            "CAUSEMATCH_ENV must be set to development, test or production"
        )


def tasks_token(env=os.environ):
    """Bearer token for the scheduler's /tasks/* calls. Required in production; elsewhere
    "" means those calls are always refused."""
    token = env.get("TASKS_TOKEN") or ""
    if not token and is_production(env):
        raise RuntimeError("TASKS_TOKEN must be set in production")
    return token


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
    return key


def db_path(env=os.environ):
    """The SQLite file: CAUSEMATCH_DB, else data/causematch.db in the repo (not the cwd)."""
    return Path(env.get("CAUSEMATCH_DB") or ROOT / "data" / "causematch.db").resolve()
