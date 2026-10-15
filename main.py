import os

from fasthtml.common import Meta, fast_app, serve
from monsterui.all import Theme

from app import notify
from app.auth import beforeware, session_secret
from app.db import seed
from app.routes.auth import ar as auth_router
from app.routes.deck import ar as deck_router
from app.routes.onboarding import ar as onboarding_router

notify.get_verifier()  # refuse to start when sign-in codes can't be sent
app, rt = fast_app(
    before=beforeware,
    secret_key=session_secret(os.environ),
    sess_https_only=notify.is_production(),
    hdrs=(
        *Theme.blue.headers(),
        Meta(name="viewport", content="width=device-width, initial-scale=1"),
    ),
)
for router in (auth_router, onboarding_router, deck_router):
    router.to_app(app)
seed()

if __name__ == "__main__":
    serve()
