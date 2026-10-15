from datetime import datetime

from fasthtml.common import (
    H1,
    H2,
    A,
    Button,
    Details,
    Div,
    Fieldset,
    Hidden,
    Img,
    Legend,
    P,
    Redirect,
    Script,
    Span,
    Summary,
)
from fasthtml.common import Form as PlainForm
from monsterui.all import Container
from starlette.concurrency import run_in_threadpool

from app import data, prefs
from app.choices import MAX_MINUTES
from app.forms import bad_request, query, str_form
from app.routes.prefs import Chip
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
        # shorter on a short phone, so the buttons stay above the tab bar
        cls="pointer-events-none block h-36 w-full object-cover "
        "[@media(max-height:740px)]:h-24",
    )


def WhyChip(w):
    """One reason the card is shown: a cause in its color (♥ when loved), else a sand chip."""
    if w["slug"]:
        return CauseChip(w["slug"], w["text"])
    tone = (
        "bg-brand-soft text-brand-ink"
        if w["text"] == "Surprise pick"
        else "bg-sand text-ink"
    )
    return Span(
        w["text"],
        cls=f"inline-flex items-center rounded-full px-2.5 py-1 text-xs font-bold {tone}",
    )


def OpeningCard(c):
    starts = datetime.fromisoformat(c["starts_at"])
    return Div(
        CauseArt(c["slugs"][0]) if c["slugs"] else None,
        Div(
            Div(*[WhyChip(w) for w in c["why"]], cls="flex flex-wrap gap-1.5"),
            H2(
                c["title"],
                cls="font-display text-xl font-extrabold leading-tight text-ink",
            ),
            P(c["org"], cls="font-semibold text-ink-muted"),
            Div(
                Meta(CALENDAR, starts.strftime("%a %b %-d · %-I:%M %p")),
                Meta(PIN, place_line(c)),
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


def EmptyDeck(today=False, relax=None):
    """Nothing to swipe. relax: data.relaxations(), the one change that adds most cards."""
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
        PlainForm(
            Hidden(name="key", value=relax["key"]),
            Hidden(name="today", value="1") if today else None,
            Button(
                f"{relax['label']}: {relax['more']} more",
                type="submit",
                cls="uk-btn uk-btn-primary mt-2 px-6",
            ),
            action="/deck/relax",
            method="post",
        )
        if relax
        else None,
        A(
            "See your matches",
            href="/matches",
            cls="uk-btn mt-2 px-6 " + ("uk-btn-default" if relax else "uk-btn-primary"),
        ),
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


LEGEND = "mb-1.5 text-sm font-bold text-ink"
SLIDERS = (
    "M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1",
    "M15 4v4M9 10v4M17 16v4",
)


def FilterSheet(uid, today, count):
    """The deck's quick filters, in a <details> over the card's top corner (no script):
    the main place's travel time, which causes show, and this week only. Its button says
    how many cards the deck holds."""
    f = data.preferences(uid)
    main = f["places"][0] if f["places"] else None
    return Details(
        Summary(
            Icon(SLIDERS, size=18),
            Span(f"{count} opening{'' if count == 1 else 's'}"),
            Span(", filters", cls="sr-only"),
            cls="flex h-11 cursor-pointer list-none items-center gap-2 rounded-full "
            "border border-line bg-white/95 px-4 text-sm font-bold text-ink shadow-warm "
            "[&::-webkit-details-marker]:hidden",
        ),
        PlainForm(
            Hidden(name="today", value="1") if today else None,
            Fieldset(
                Legend(f"Travel from {main['label']}, up to (minutes)", cls=LEGEND),
                Div(
                    *[
                        Chip(
                            "radio",
                            "max_minutes",
                            str(v),
                            label,
                            main["max_minutes"] == v,
                        )
                        for v, label in MAX_MINUTES
                    ],
                    cls="grid grid-cols-5 gap-1.5",
                ),
            )
            if main
            else None,
            Fieldset(
                Legend("Causes", cls=LEGEND),
                Div(
                    *[
                        Chip(
                            "checkbox",
                            "causes",
                            c.slug,
                            c.label,
                            f["causes"].get(c.slug) != prefs.NO,
                        )
                        for c in data.active_causes()
                    ],
                    cls="flex flex-wrap gap-2",
                ),
            ),
            Chip(
                "checkbox", "this_week", "1", "Only this week", f["horizon_days"] == 7
            ),
            Div(
                Button(
                    "Show openings",
                    type="submit",
                    cls="uk-btn uk-btn-primary h-11 flex-1",
                ),
                A(
                    "All preferences",
                    href="/me/preferences",
                    cls="uk-btn uk-btn-default h-11",
                ),
                cls="flex gap-2",
            ),
            action="/deck/filters",
            method="post",
            cls="absolute right-0 top-full z-30 mt-2 w-[min(22rem,calc(100vw-2rem))] "
            "space-y-4 rounded-3xl border border-line bg-white p-5 shadow-warm",
        ),
        cls="absolute right-3 top-3 z-20",
    )


def DeckArea(uid, today=False):
    """The top card with its buttons, or the empty state; the unit /swipe swaps. The
    filter sheet comes last, so swipe.js finds the swipe form first; it sits over the
    card's top corner."""
    cards, count = data.deck_and_count(uid, limit=1, today=today)
    sheet = FilterSheet(uid, today, count)
    if not cards:
        data.record_empty_deck(uid, today)
        relax = data.relaxations(uid, today)
        return Div(EmptyDeck(today, relax), sheet, id="deck-area", cls="relative")
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
        sheet,
        id="deck-area",
        cls="relative",
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


@ar("/deck/filters", methods=["POST"])
async def filters_save(req):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_filters, req, form)


def _sync_filters(req, form):
    minutes = form.get("max_minutes")
    shown = set(form.getlist("causes"))
    if (
        minutes is not None and minutes not in {str(m) for m, _ in MAX_MINUTES}
    ) or not shown <= {c.slug for c in data.active_causes()}:
        return bad_request()
    data.save_quick_filters(
        req.scope["uid"],
        max_minutes=None if minutes is None else int(minutes),
        shown=shown,
        this_week=form.get("this_week") == "1",
    )
    return Redirect("/deck?today=1" if form.get("today") == "1" else "/deck")


@ar("/deck/relax", methods=["POST"])
async def relax(req):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_relax, req, form)


def _sync_relax(req, form):
    try:
        data.apply_relaxation(req.scope["uid"], form.get("key", ""))
    except ValueError:
        pass  # no longer on offer (the deck changed since): just show it again
    return Redirect("/deck?today=1" if form.get("today") == "1" else "/deck")
