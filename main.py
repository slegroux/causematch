import os
from pathlib import Path

from fasthtml.common import fast_app, serve
from monsterui.all import Theme
from starlette.staticfiles import StaticFiles

from app import notify
from app.auth import SESSION_MAX_AGE, beforeware, session_secret
from app.config import is_production, public_base_url, require_env
from app.db import ensure_causes, seed_demo
from app.routes.auth import ar as auth_router
from app.routes.deck import ar as deck_router
from app.routes.onboarding import ar as onboarding_router
from app.routes.org import ar as org_router
from app.routes.org_auth import ar as org_auth_router

require_env()  # an undeclared environment never runs
notify.get_verifier()  # refuse to start when sign-in codes can't be sent
notify.get_emailer()  # ... or sign-in links
public_base_url()  # ... or links have no configured origin
app, rt = fast_app(
    before=beforeware,
    secret_key=session_secret(os.environ),
    sess_https_only=is_production(),
    max_age=SESSION_MAX_AGE,
    title="Causematch",
    hdrs=Theme.blue.headers(),
)
# fast_app registers a catch-all that serves any file under the working directory
# (and, through an encoded "..", outside it). Drop it; only /static is served.
app.routes[:] = [
    r for r in app.routes if getattr(r, "path", "") != "/{fname:path}.{ext:static}"
]
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"))
for router in (
    auth_router,
    org_auth_router,
    onboarding_router,
    deck_router,
    org_router,
):
    router.to_app(app)
ensure_causes()
if not is_production() or os.environ.get("CAUSEMATCH_SEED_DEMO") == "1":
    seed_demo()  # invented orgs and openings: never in production unless asked

if __name__ == "__main__":
    serve()
