from datetime import datetime

from fasthtml.common import H1, A, APIRouter, Div, Hidden, P, Redirect, Script
from fasthtml.common import Form as PlainForm
from monsterui.all import (
    AT,
    Button,
    ButtonT,
    Card,
    CardTitle,
    Container,
    DivFullySpaced,
    DivLAligned,
    Label,
    TextPresets,
)

from app import data
from app.ui import VolunteerNav

ar = APIRouter()


def when(starts_at):
    return datetime.fromisoformat(starts_at).strftime("%a %b %-d, %-I:%M %p")


def OpeningCard(c):
    return Card(
        DivLAligned(*[Label(name) for name in c["causes"]], cls="flex-wrap gap-1"),
        P(c["org"], cls=TextPresets.muted_sm),
        DivFullySpaced(P(when(c["starts_at"])), P(c["neighborhood"])),
        P(
            "Remote"
            if c["remote"]
            else f"about {c['distance']} away"
            if c["distance"]
            else "",
            cls=TextPresets.muted_sm,
        ),
        P(f"{c['slots']} spots", cls=TextPresets.muted_sm),
        header=CardTitle(c["title"]),
        cls="w-full select-none",
        data_swipe_card="1",
        style="touch-action: pan-y;",
    )


def SwipeButtons(opening_id):
    return PlainForm(
        Hidden(name="opening_id", value=str(opening_id)),
        Button("Pass", name="liked", value="0", cls=ButtonT.default + " flex-1"),
        Button("Like", name="liked", value="1", cls=ButtonT.primary + " flex-1"),
        action="/swipe",
        method="post",
        hx_post="/swipe",
        hx_target="#deck-area",
        hx_swap="outerHTML",
        hx_disabled_elt="find button",
        cls="flex gap-3 mt-4",
    )


def DeckArea(uid):
    """The top card with its buttons, or the empty state; the unit /swipe swaps."""
    cards = data.deck(uid, limit=1)
    if not cards:
        return Div(
            P("You have seen every opening for now. Check back soon.", cls="my-8"),
            A("See your matches", href="/matches", cls=AT.muted),
            id="deck-area",
        )
    c = cards[0]
    return Div(
        OpeningCard(c),
        SwipeButtons(c["id"]),
        P(
            "Couldn't save that. Try again.",
            id="swipe-error",
            role="alert",
            hidden=True,
        ),
        id="deck-area",
    )


@ar("/")
def index():
    return Redirect("/deck")


@ar("/deck")
def deck(req):
    return Container(
        VolunteerNav(),
        H1("Openings near you", cls="text-2xl font-bold my-4"),
        DeckArea(req.scope["uid"]),
        Script(src="/static/swipe.js", defer=True),
        cls="max-w-md pb-8",
    )
