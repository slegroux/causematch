import logging
import os
from pathlib import Path

from fasthtml.common import Link, Meta, Script, fast_app, serve
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles

from app import data, demo_seed, notify, security, ui
from app.auth import SESSION_MAX_AGE, beforeware, session_cookie, session_secret
from app.config import (
    contact_email,
    demo_banner,
    is_preview,
    is_production,
    phone_hash_key,
    public_base_url,
    require_env,
    sms_ceilings,
    tasks_token,
    trusted_proxy_hops,
)
from app.routes import (
    admin,
    chat,
    claim,
    deck,
    matches,
    onboarding,
    org,
    org_auth,
    org_profile,
    photos,
    prefs,
    profile,
    pwa,
    safety,
    shifts,
    tasks,
)
from app.routes import auth as auth_routes
from app.routes.safety import not_found

require_env()  # an undeclared environment, or a preview without its password, never runs
notify.get_verifier()  # refuse to start when sign-in codes can't be sent
notify.get_emailer()  # ... or sign-in links
notify.get_sms()  # ... or texts
tasks_token()  # ... or the scheduler can't call in
public_base_url()  # ... or links have no configured origin
contact_email()  # ... or the privacy page has no contact
phone_hash_key()  # ... or deleted accounts' blocks can't be kept
trusted_proxy_hops()  # ... or, on Cloud Run, rate limits can't tell clients apart
sms_ceilings()  # ... or the SMS ceiling is unreadable
demo_banner()  # ... or production was asked to show the demo banner
# uvicorn settings; the Procfile runs the same ones.
UVICORN = {
    "ws_max_size": 65536,  # bytes per websocket frame (default 16 MiB); a message is <= 2000 chars
    "limit_concurrency": 200,  # connections and open sockets; past it uvicorn answers 503
    "timeout_keep_alive": 15,
    "timeout_graceful_shutdown": 8,  # inside Cloud Run's 10 s after SIGTERM
    "proxy_headers": False,  # app/security.py reads X-Forwarded-For itself, only on Cloud Run
}
logging.getLogger("uvicorn.access").addFilter(security.RedactTokens())
# The app's own lines on stderr (Cloud Run collects it). CAUSEMATCH_LOG_LEVEL=DEBUG adds the
# hashed client key that the X-Forwarded-For check (TRUSTED_PROXY_HOPS) reads.
_log = logging.getLogger("causematch")
_log.setLevel(os.environ.get("CAUSEMATCH_LOG_LEVEL") or "INFO")
if not _log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _log.addHandler(_handler)

app, rt = fast_app(
    before=(beforeware, security.throttle),
    # outside the session: covers every request. The preview's password, inside Guard so
    # its 401 carries the headers too.
    middleware=(Middleware(security.Guard), Middleware(security.PreviewGate)),
    secret_key=session_secret(os.environ),
    session_cookie=session_cookie(),
    sess_https_only=is_production(),
    sess_path="/",
    same_site="lax",
    max_age=SESSION_MAX_AGE,
    debug=False,  # no traceback pages; security.Guard serves a plain 500 in production
    title="Causematch",
    body_wrap=ui.body_wrap,  # the demo banner; each APIRouter passes it too
    exception_handlers={404: not_found},  # the shared look instead of a bare "404"
    # Self-hosted, pinned htmx. Not loaded: fasthtml's surreal@main, css-scope-inline@main,
    # pico@latest (unpinned CDN branches the app doesn't use) and fasthtml.js.
    htmx=False,
    surreal=False,
    pico=False,
    hdrs=(
        # htmx would otherwise inject its indicator styles inline, which the CSP forbids
        # Eval and script tags off too: the CSP blocks them already; nothing here uses them.
        Meta(
            name="htmx-config",
            content='{"includeIndicatorStyles": false, "allowEval": false, '
            '"allowScriptTags": false}',
        ),
        Script(src="/static/vendor/htmx-2.0.7.min.js"),
        Script(src="/static/vendor/htmx-ext-ws-2.0.4.min.js"),
        # MonsterUI's Theme.blue.headers(), self-hosted so the CSP allows only 'self': its
        # inline script and styles live in static/theme.js and static/theme.css, and the
        # Tailwind Play CDN is replaced by static/vendor/app.css (scripts/build_css.sh).
        # Not loaded: Franken UI's icon script (no UkIcon here).
        Script(src="/static/theme.js"),
        *[Link(rel="stylesheet", href=href) for href in security.STYLESHEETS],
        Script(type="module", src="/static/vendor/franken-ui-2.0.0/core.iife.js"),
        Link(rel="manifest", href="/static/manifest.webmanifest"),
        Link(rel="icon", href="/favicon.ico", sizes="any"),
        Link(rel="apple-touch-icon", href="/static/icons/apple-touch-icon.png"),
        Meta(name="theme-color", content="#FFF8F0"),  # static/theme.css: cream
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
# gzip for the static files only (daisyUI's CSS is 2.9 MB raw, 170 kB gzipped); pages stay
# uncompressed, so no secret is ever compressed next to reflected input.
app.mount(
    "/static",
    GZipMiddleware(
        StaticFiles(directory=Path(__file__).parent / "static"), minimum_size=1024
    ),
)
for routes in (
    auth_routes,
    org_auth,
    claim,
    admin,
    onboarding,
    deck,
    matches,
    chat,
    org,
    shifts,
    safety,
    prefs,
    tasks,
    pwa,
    profile,
    photos,
    org_profile,
):
    routes.ar.to_app(app)
# invented orgs and openings: never in production
data.start(not is_production())
if is_preview():  # the demo's orgs and people, reseeded at each cold start
    demo_seed.seed_preview(os.environ.get("PREVIEW_SEED_DIR"))

if __name__ == "__main__":
    serve(**UVICORN)
