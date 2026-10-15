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
                cls="pointer-events-none block h-36 w-full object-cover "
                "[@media(max-height:740px)]:h-28",
            ),
            LogoBadge(c["logo"], "absolute bottom-3 left-3 h-16 w-16")
            if c.get("logo")
            else None,
            cls="relative",
        )
    if c.get("logo"):
        return Div(
            LogoBadge(c["logo"], "h-24 w-24"),
            cls="grid h-36 place-items-center [@media(max-height:740px)]:h-24 "
            + CAUSE_CHIP.get(slug, "bg-sand text-ink"),
        )
    return CauseArt(slug) if slug else None


def OpeningCard(c):
    starts = datetime.fromisoformat(c["starts_at"])
    return Div(
        CardImage(c),
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
                Div(
                    Meta(PEOPLE, f"{c['slots']} spots"),
                    # a 44 px target that takes no more height than the line beside it
                    EventLink(c.get("event_url"), "-my-3"),
                    cls="flex flex-wrap items-center justify-between gap-x-4",
                ),
                cls="space-y-1 pt-1",
            ),
            cls="space-y-1.5 px-5 pt-3 pb-4",
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
        cls="pointer-events-auto grid place-items-center rounded-full shadow-warm "
        "transition "
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
        cls="flex items-center justify-center gap-12",
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


VIEW = "deck_view"  # the session key of the filter sheet's choices (data._viewed)


def deck_view(req):
    """The filter sheet's choices for this visit, or None."""
    return req.session.get(VIEW)


def FilterSheet(uid, today, count, view):
    """The deck's quick filters, in a <details> (no script) whose round button sits left of
    Pass and Like, or under the empty deck. Open, the sheet is fixed just above the tab
    bar, whatever the card's height, so all of it is on screen:
    the main place's travel time, which causes show, and this week only. They hold until
    cleared and change no saved preference. Its button says how many cards the deck
    holds."""
    view = view or {}
    f = data.preferences(uid)
    main = f["places"][0] if f["places"] else None
    minutes = view.get("max_minutes", main and main["max_minutes"])
    allowed = [c for c in data.active_causes() if f["causes"].get(c.slug) != prefs.NO]
    shown = [c.slug for c in allowed if c.slug not in view.get("hidden", [])]
    return Details(
        Summary(
            Icon(SLIDERS, size=20),
            # the count as a badge; its full words for a screen reader
            Span(
                str(count),
                aria_hidden="true",
                cls="absolute -right-1 -top-1 grid h-6 min-w-6 place-items-center "
                "rounded-full bg-ink px-1.5 text-xs font-bold text-cream",
            ),
            Span(f"{count} opening{'' if count == 1 else 's'}", cls="sr-only"),
            Span(" (Filtered)", cls="sr-only") if view else None,
            Span(", filters", cls="sr-only"),
            cls="relative grid h-12 w-12 cursor-pointer list-none place-items-center "
            "rounded-full border bg-white text-ink shadow-warm "
            "[&::-webkit-details-marker]:hidden "
            + ("border-2 border-brand text-brand-ink" if view else "border-line"),
        ),
        PlainForm(
            Hidden(name="today", value="1") if today else None,
            Div(
                H2("Filters", cls="font-display text-lg font-extrabold text-ink"),
                # static/swipe.js closes the <details>; without script, Show closes it
                Button(
                    Icon(CROSS, size=20),
                    Span("Close filters", cls="sr-only"),
                    type="button",
                    data_close_sheet="1",
                    cls="-mr-2 grid h-11 w-11 place-items-center rounded-full "
                    "text-ink-muted hover:bg-sand hover:text-ink",
                ),
                cls="flex items-center justify-between",
            ),
            P(
                "These hold until you clear them. To keep a change, edit ",
                A(
                    "your preferences",
                    href="/me/preferences",
                    cls="font-bold text-brand-ink underline",
                ),
                ".",
                cls="text-sm text-ink-muted",
            ),
            # minutes only limit the deck while distance is "Only show"
            Fieldset(
                Legend(f"Travel from {main['label']}, up to (minutes)", cls=LEGEND),
                Div(
                    *[
                        Chip("radio", "max_minutes", str(v), label, minutes == v)
                        for v, label in MAX_MINUTES
                    ],
                    cls="grid grid-cols-5 gap-1.5",
                ),
            )
            if main and "distance" in f["dealbreakers"]
            else None,
            Fieldset(
                Legend("Causes", cls=LEGEND),
                Div(
                    *[
                        Chip("checkbox", "causes", c.slug, c.label, c.slug in shown)
                        for c in allowed
                    ],
                    cls="flex flex-wrap gap-2",
                ),
            ),
            Chip(
                "checkbox",
                "this_week",
                "1",
                "Only this week",
                bool(view.get("this_week")) or f["horizon_days"] == 7,
            ),
            Div(
                Button(
                    "Show openings",
                    type="submit",
                    cls="uk-btn uk-btn-primary h-11 flex-1",
                ),
                Button(
                    "Clear",
                    type="submit",
                    name="clear",
                    value="1",
                    cls="uk-btn uk-btn-default h-11",
                )
                if view
                else None,
                # stays in view at the bottom when a short screen scrolls the sheet
                cls="sticky -bottom-5 -mx-5 -mb-5 flex gap-2 bg-white px-5 pt-2 pb-5",
            ),
            action="/deck/filters",
            method="post",
            # fixed above the tab bar (cm-filter-sheet in static/theme.css), never
            # taller than the screen above it; a short screen scrolls it
            cls="cm-filter-sheet fixed inset-x-4 z-40 mx-auto max-w-[22rem] "
            "space-y-4 overflow-y-auto rounded-3xl border border-line bg-white p-5 "
            "shadow-warm",
        ),
        cls="relative z-20",
    )


def DeckArea(uid, today=False, view=None, notice=None):
    """The top card with its buttons, or the empty state; the unit /swipe swaps. The
    filter sheet's round button sits left of Pass and Like, clear of the picture and
    adding no height (swipe.js finds the swipe form by its action). notice: a line above
    the card (a swipe that didn't count)."""
    cards, count = data.deck_and_count(uid, limit=1, today=today, view=view)
    note = (
        P(notice, role="status", cls="mb-2 text-center text-sm font-semibold text-ink")
        if notice
        else None
    )
    if not cards:
        data.record_empty_deck(uid, today)
        relax = data.relaxations(uid, today, view)
        return Div(
            note,
            EmptyDeck(today, relax),
            Div(FilterSheet(uid, today, count, view), cls="mt-3 flex justify-center"),
            id="deck-area",
            cls="relative",
        )
    c = cards[0]
    return Div(
        note,
        OpeningCard(c),
        # cm-swipe-buttons (static/theme.css): a tall card scrolls under this row, so Pass
        # and Like stay in reach above the tab bar; the gaps between them still drag
        Div(
            Div(
                FilterSheet(uid, today, count, view),
                cls="pointer-events-auto",
            ),
            SwipeButtons(c["id"], today),
            Span(cls="w-12", aria_hidden="true"),  # keeps Pass and Like centred
            cls="cm-swipe-buttons pointer-events-none mt-4 flex items-center "
            "justify-center gap-6",
        ),
        P(
            "Couldn't save that. Try again.",
            id="swipe-error",
            role="alert",
            hidden=True,
            cls="mt-3 text-center text-sm font-semibold text-red-700",
        ),
        id="deck-area",
        cls="relative",
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
    today = query(req, "today") == "1"
    me = data.get_profile(req.scope["uid"])
    return Container(
        VolunteerNav("openings"),
        H1("Openings near you", cls="sr-only"),  # the Openings tab says it on screen
        TodaySwitch(today),
        DeckArea(req.scope["uid"], today, deck_view(req)),
        None if me.photo_key or me.photo_nudge_dismissed_at else PhotoNudge(),
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
    shown = form.getlist("causes")
    if (
        minutes is not None and minutes not in {str(m) for m, _ in MAX_MINUTES}
    ) or not set(shown) <= {c.slug for c in data.active_causes()}:
        return bad_request()
    saved = data.preferences(req.scope["uid"])
    main = saved["places"][0] if saved["places"] else None
    # only what differs from the saved preferences, so the view pins nothing else
    view = {}
    if minutes is not None and main and int(minutes) != main["max_minutes"]:
        view["max_minutes"] = int(minutes)
    hidden = sorted(
        c.slug
        for c in data.active_causes()
        if c.slug not in shown and saved["causes"].get(c.slug) != prefs.NO
    )
    if hidden:
        view["hidden"] = hidden
    if form.get("this_week") == "1" and not 0 < saved["horizon_days"] <= 7:
        view["this_week"] = True
    if form.get("clear") == "1" or not view:
        req.session.pop(VIEW, None)
    else:  # the session, never the saved preferences
        req.session[VIEW] = view
    return Redirect("/deck?today=1" if form.get("today") == "1" else "/deck")


@ar("/deck/relax", methods=["POST"])
async def relax(req):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_relax, req, form)


def _sync_relax(req, form):
    if form.get("key") == "clear_view":
        req.session.pop(VIEW, None)
    else:
        try:
            data.apply_relaxation(req.scope["uid"], form.get("key", ""))
        except ValueError:
            pass  # no longer on offer (the deck changed since): it shows again, as is
    return Redirect("/deck?today=1" if form.get("today") == "1" else "/deck")
