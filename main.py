import os
from pathlib import Path

from fasthtml.common import Link, Meta, Script, fast_app, serve
from monsterui.all import Theme
from starlette.staticfiles import StaticFiles

from app import data, notify
from app.auth import SESSION_MAX_AGE, beforeware, session_secret
from app.config import is_production, public_base_url, require_env
from app.routes.auth import ar as auth_router
from app.routes.chat import ar as chat_router
from app.routes.deck import ar as deck_router
from app.routes.matches import ar as matches_router
from app.routes.onboarding import ar as onboarding_router
from app.routes.org import ar as org_router
from app.routes.org_auth import ar as org_auth_router
from app.routes.pwa import ar as pwa_router
from app.routes.shifts import ar as shifts_router

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
    # Self-hosted, pinned htmx. Not loaded: fasthtml's surreal@main, css-scope-inline@main,
    # pico@latest (unpinned CDN branches the app doesn't use) and fasthtml.js.
    htmx=False,
    surreal=False,
    pico=False,
    hdrs=(
        Script(src="/static/vendor/htmx-2.0.7.min.js"),
        Script(src="/static/vendor/htmx-ext-ws-2.0.4.min.js"),
        *Theme.blue.headers(),
        Link(rel="manifest", href="/static/manifest.webmanifest"),
        Link(rel="icon", href="/favicon.ico", sizes="any"),
        Link(rel="apple-touch-icon", href="/static/icons/apple-touch-icon.png"),
        Meta(name="theme-color", content="#2563eb"),
        Meta(name="mobile-web-app-capable", content="yes"),
        Meta(name="apple-mobile-web-app-capable", content="yes"),
        Meta(name="apple-mobile-web-app-title", content="Causematch"),
        Script(src="/static/a2hs.js", defer=True),
    ),
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
    matches_router,
    chat_router,
    org_router,
    shifts_router,
    pwa_router,
):
    router.to_app(app)
# invented orgs and openings: never in production unless asked
data.start(not is_production() or os.environ.get("CAUSEMATCH_SEED_DEMO") == "1")

if __name__ == "__main__":
    serve()
