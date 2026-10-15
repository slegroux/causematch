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
from app.ui import (
    CALENDAR,
    CAUSE_CHIP,
    CROSS,
    HEART,
    PEOPLE,
    PIN,
    CauseChip,
    EventLink,
    Icon,
    LogoBadge,
    Router,
    VolunteerNav,
)

ar = Router()


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


def CardImage(c):
    """The opening's photo, else the org's, with the org's logo as a round badge on it;
    with no photo the logo alone on the cause's tint; with neither, the cause art."""
    slug = c["slugs"][0] if c["slugs"] else None
    if c.get("photo"):
        return Div(
            Img(
                src=f"/photos/{c['photo']}.jpg",
                alt="",
                draggable="false",
                # short enough that Pass and Like stay above the tab bar on a small phone
                cls="pointer-events-none block h-40 max-h-[22vh] w-full object-cover",
            ),
            LogoBadge(c["logo"], "absolute bottom-3 left-3 h-16 w-16")
            if c.get("logo")
            else None,
            cls="relative",
        )
    if c.get("logo"):
        return Div(
            LogoBadge(c["logo"], "h-24 w-24"),
            cls="grid h-36 place-items-center "
            + CAUSE_CHIP.get(slug, "bg-sand text-ink"),
        )
    return CauseArt(slug) if slug else None


def OpeningCard(c):
    starts = datetime.fromisoformat(c["starts_at"])
    return Div(
        CardImage(c),
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
                Div(
                    Meta(PEOPLE, f"{c['slots']} spots"),
                    # a 44 px target that takes no more height than the line beside it
                    EventLink(c.get("event_url"), "-my-3"),
                    cls="flex flex-wrap items-center justify-between gap-x-4",
                ),
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


def SwipeButtons(opening_id):
    return PlainForm(
        Hidden(name="opening_id", value=str(opening_id)),
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


def EmptyDeck():
    return Div(
        Img(src="/static/img/empty-deck.svg", alt="", cls="mx-auto h-40 w-40"),
        H2("You're all caught up", cls="font-display text-xl font-extrabold text-ink"),
        P(
            "You have seen every opening for now. Check back soon.",
            cls="mx-auto max-w-xs text-ink-muted",
        ),
        A("See your matches", href="/matches", cls="uk-btn uk-btn-primary mt-2 px-6"),
        cls="space-y-3 rounded-3xl border border-line bg-white px-6 py-10 text-center "
        "shadow-warm",
    )


def DeckArea(uid):
    """The top card with its buttons, or the empty state; the unit /swipe swaps."""
    cards = data.deck(uid, limit=1)
    if not cards:
        return Div(EmptyDeck(), id="deck-area")
    c = cards[0]
    return Div(
        OpeningCard(c),
        SwipeButtons(c["id"]),
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


def PhotoNudge():
    """Below the deck, until the volunteer adds a photo or says "Not now"."""
    return Div(
        P(
            "Add a photo so nonprofits know who's coming. They see it only after "
            "you like one of their openings.",
            cls="text-sm font-semibold text-ink",
        ),
        Div(
            A("Add a photo", href="/me/profile", cls="uk-btn uk-btn-primary px-5"),
            PlainForm(
                Button(
                    "Not now",
                    type="submit",
                    cls="inline-flex min-h-11 items-center px-2 text-sm font-semibold "
                    "text-ink-muted underline underline-offset-4 hover:text-ink",
                ),
                action="/deck/photo-nudge/dismiss",
                method="post",
            ),
            cls="flex items-center gap-4",
        ),
        cls="mt-6 space-y-3 rounded-3xl border border-line bg-white p-4 shadow-warm",
    )


@ar("/deck/photo-nudge/dismiss", methods=["POST"])
def dismiss_photo_nudge(req):
    data.dismiss_photo_nudge(req.scope["uid"])
    return Redirect("/deck")


@ar("/deck")
def deck(req):
    me = data.get_profile(req.scope["uid"])
    return Container(
        VolunteerNav("openings"),
        H1("Openings near you", cls="sr-only"),  # the Openings tab says it on screen
        DeckArea(req.scope["uid"]),
        None if me.photo_key or me.photo_nudge_dismissed_at else PhotoNudge(),
        Script(src="/static/swipe.js", defer=True),
        cls="max-w-md pb-6",
    )
