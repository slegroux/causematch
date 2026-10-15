import os

from fasthtml.common import fast_app, serve
from monsterui.all import Theme

from app import notify
from app.auth import beforeware, session_secret
from app.db import ensure_causes, seed_demo
from app.routes.auth import ar as auth_router
from app.routes.deck import ar as deck_router
from app.routes.onboarding import ar as onboarding_router

notify.get_verifier()  # refuse to start when sign-in codes can't be sent
app, rt = fast_app(
    before=beforeware,
    secret_key=session_secret(os.environ),
    sess_https_only=notify.is_production(),
    title="Causematch",
    hdrs=Theme.blue.headers(),
)
for router in (auth_router, onboarding_router, deck_router):
    router.to_app(app)
ensure_causes()
if not notify.is_production() or os.environ.get("CAUSEMATCH_SEED_DEMO") == "1":
    seed_demo()  # invented orgs and openings: never in production unless asked

if __name__ == "__main__":
    serve()
