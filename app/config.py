import os


def is_production(env=os.environ):
    """Cloud Run (K_SERVICE) counts as production whatever CAUSEMATCH_ENV says."""
    return env.get("CAUSEMATCH_ENV") == "production" or "K_SERVICE" in env


def is_dev(env=os.environ):
    """Development is something you declare; an unset environment is not development."""
    return env.get("CAUSEMATCH_ENV") in ("development", "test") and not is_production(
        env
    )
