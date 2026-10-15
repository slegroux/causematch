import os


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
