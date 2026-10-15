from datetime import datetime

from fasthtml.common import (
    H1,
    H2,
    A,
    Button,
    Div,
    Hidden,
    Img,
    P,
    Redirect,
    Script,
    Span,
)
from fasthtml.common import Form as PlainForm
from monsterui.all import Container

from app import data
from app.forms import query
from app.ui import (
    CALENDAR,
    CAUSE_CHIP,
    CROSS,
    HEART,
    PEOPLE,
    PIN,
    CauseChip,
    Icon,
    Router,
    VolunteerNav,
)

ar = Router()

ROUTE = ("M3 11 22 2l-9 19-2-8-8-2Z",)  # a navigation arrow: the travel estimate


def when(starts_at):
    return datetime.fromisoformat(starts_at).strftime("%a %b %-d, %-I:%M %p")


def place_line(c):
    if c["remote"]:
        return "Remote"
    if c["distance"]:
        return f"{c['neighborhood']} · about {c['distance']} away"
    return c["neighborhood"]


def Meta(icon, text):
    return P(
        Icon(icon, size=16, cls="text-ink-muted"),
        Span(text),
        cls="flex items-center gap-2 text-sm text-ink",
    )


def CauseArt(slug):
    """The card's header: static/img/causes/<slug>.svg, for the causes that have one."""
    if slug not in CAUSE_CHIP:
        return None
    return Img(
        src=f"/static/img/causes/{slug}.svg",
        alt="",
        draggable="false",  # a mouse drag moves the card, not a ghost of the image
        cls="pointer-events-none block h-36 w-full object-cover",
    )


def OpeningCard(c):
    starts = datetime.fromisoformat(c["starts_at"])
    return Div(
        CauseArt(c["slugs"][0]) if c["slugs"] else None,
        Div(
            Div(
                *[CauseChip(s, label) for s, label in zip(c["slugs"], c["causes"])],
                cls="flex flex-wrap gap-1.5",
            ),
            H2(
                c["title"],
                cls="font-display text-xl font-extrabold leading-tight text-ink",
            ),
            P(c["org"], cls="font-semibold text-ink-muted"),
            Div(
                Meta(CALENDAR, starts.strftime("%a %b %-d · %-I:%M %p")),
                Meta(PIN, place_line(c)),
                Meta(ROUTE, f"About {c['travel']}") if c.get("travel") else None,
                Meta(PEOPLE, f"{c['slots']} spots"),
                cls="space-y-1 pt-1",
            ),
            cls="space-y-1.5 px-5 pt-4 pb-5",
        ),
        # classes, not style attributes: the CSP forbids them (static/swipe.js sets styles
        # from script, which it allows)
        cls="w-full select-none touch-pan-y overflow-hidden rounded-3xl border "
        "border-line bg-white shadow-warm",
        data_swipe_card="1",
    )


def RoundButton(label, liked, icon, cls):
    return Button(
        icon,
        Span(label, cls="sr-only"),
        type="submit",
        name="liked",
        value=liked,
        cls="grid place-items-center rounded-full shadow-warm transition "
        "active:scale-95 disabled:opacity-60 focus-visible:outline "
        "focus-visible:outline-4 focus-visible:outline-offset-2 "
        "focus-visible:outline-brand/40 " + cls,
    )


def SwipeButtons(opening_id, today=False):
    return PlainForm(
        Hidden(name="opening_id", value=str(opening_id)),
        Hidden(name="today", value="1") if today else None,
        RoundButton(
            "Pass",
            "0",
            Icon(CROSS, size=28, cls="stroke-[2.5]"),
            "h-16 w-16 border border-line bg-white text-ink-muted hover:text-ink",
        ),
        RoundButton(
            "Like",
            "1",
            Icon(HEART, size=32, filled=True),
            "h-[4.5rem] w-[4.5rem] bg-brand text-white hover:bg-brand-strong",
        ),
        action="/swipe",
        method="post",
        hx_post="/swipe",
        hx_target="#deck-area",
        hx_swap="outerHTML",
        hx_disabled_elt="find button",
        cls="mt-4 flex items-center justify-center gap-12",
    )


def EmptyDeck(today=False):
    return Div(
        Img(src="/static/img/empty-deck.svg", alt="", cls="mx-auto h-40 w-40"),
        H2(
            "Nothing in the next 48 hours" if today else "You're all caught up",
            cls="font-display text-xl font-extrabold text-ink",
        ),
        P(
            "No shift you could reach starts that soon. Turn off “I'm free today” to "
            "see everything."
            if today
            else "You have seen every opening for now. Check back soon.",
            cls="mx-auto max-w-xs text-ink-muted",
        ),
        A("See your matches", href="/matches", cls="uk-btn uk-btn-primary mt-2 px-6"),
        cls="space-y-3 rounded-3xl border border-line bg-white px-6 py-10 text-center "
        "shadow-warm",
    )


def TodaySwitch(today):
    """ "I'm free today": only shifts starting in the next 48 hours, soonest first. A plain
    GET form, so it works without script; the button says whether it is on."""
    return PlainForm(
        Hidden(name="today", value="0" if today else "1"),
        Button(
            Span(
                Span(
                    cls="block h-5 w-5 rounded-full bg-white shadow transition "
                    + ("translate-x-5" if today else "translate-x-0"),
                ),
                cls="flex h-6 w-11 items-center rounded-full p-0.5 transition "
                + ("bg-brand" if today else "bg-gray-500"),
                aria_hidden="true",
            ),
            Span("I'm free today"),
            type="submit",
            aria_pressed="true" if today else "false",
            cls="flex min-h-11 items-center gap-2.5 rounded-full px-1 text-sm font-bold "
            "text-ink focus-visible:outline focus-visible:outline-2 "
            "focus-visible:outline-brand",
        ),
        action="/deck",
        method="get",
        # up into the header row, beside the wordmark, so the card and its buttons still
        # fit above the tab bar on a phone
        cls="relative z-10 -mt-12 mb-1 flex justify-end",
    )


def DeckArea(uid, today=False):
    """The top card with its buttons, or the empty state; the unit /swipe swaps."""
    cards = data.deck(uid, limit=1, today=today)
    if not cards:
        return Div(EmptyDeck(today), id="deck-area")
    c = cards[0]
    return Div(
        OpeningCard(c),
        SwipeButtons(c["id"], today),
        P(
            "Couldn't save that. Try again.",
            id="swipe-error",
            role="alert",
            hidden=True,
            cls="mt-3 text-center text-sm font-semibold text-red-700",
        ),
        id="deck-area",
    )


@ar("/")
def index():
    return Redirect("/deck")


@ar("/deck")
def deck(req):
    today = query(req, "today") == "1"
    return Container(
        VolunteerNav("openings"),
        H1("Openings near you", cls="sr-only"),  # the Openings tab says it on screen
        TodaySwitch(today),
        DeckArea(req.scope["uid"], today),
        Script(src="/static/swipe.js", defer=True),
        cls="max-w-md pb-6",
    )
