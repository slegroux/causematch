from fasthtml.common import Meta, fast_app, serve
from monsterui.all import Theme

from app.db import seed
from app.routes.deck import ar as deck_router

app, rt = fast_app(
    hdrs=(
        *Theme.blue.headers(),
        Meta(name="viewport", content="width=device-width, initial-scale=1"),
    ),
)
deck_router.to_app(app)
seed()

if __name__ == "__main__":
    serve()
