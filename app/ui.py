from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

from fasthtml.common import (
    H1,
    A,
    APIRouter,
    Button,
    Div,
    Form,
    Img,
    Input,
    Nav,
    Option,
    P,
    Select,
    Span,
    Title,
)
from fasthtml.svg import Path, Rect, Svg
from monsterui.all import (
    AT,
    Alert,
    AlertT,
    Container,
    FormLabel,
    LabelCheckboxX,
    TextPresets,
)

from app import data
from app.config import demo_banner, is_preview, public_base_url
from app.forms import domain

# Each cause's chip: its soft tint with its ink (static/theme.css). Literal class strings,
# so Tailwind's scan of app/ finds them.
CAUSE_CHIP = {
    "food-insecurity": "bg-cause-food-soft text-cause-food-ink",
    "environment-parks": "bg-cause-parks-soft text-cause-parks-ink",
    "animal-shelters": "bg-cause-animals-soft text-cause-animals-ink",
    "homelessness-services": "bg-cause-homeless-soft text-cause-homeless-ink",
}
DEMO_NOTICE = "Demo: sample openings, not affiliated with these organizations"
PREVIEW_NOTICE = (
    "Preview · sample openings, not affiliated with these organizations · "
    "don't enter real details"
)


def PrivacyFooter():
    return P(
        A(
            "Privacy",
            href="/privacy",
            cls=AT.muted + " inline-flex min-h-11 items-center",
        ),
        cls=TextPresets.muted_sm + " pt-6",
    )


def Field(label, *inputs):
    """A label and its field. A lone input, select or textarea gets an id from its name
    (unless it has one), and the label points at it: tapping the label focuses the field,
    and a screen reader reads them together."""
    fields = [
        x for x in inputs if getattr(x, "tag", None) in ("input", "select", "textarea")
    ]
    first = fields[0] if len(fields) == 1 else None
    target = None
    if first is not None:
        target = first.attrs.get("id") or (
            f"field-{first.attrs['name']}" if first.attrs.get("name") else None
        )
        if target:
            first.attrs["id"] = target
    return Div(FormLabel(label, fr=target), *inputs, cls="space-y-2")


def TextInput(**kwargs):
    """Every text-like <input> (text, tel, email, number, date, datetime-local): Franken's
    bordered field, so no form reads as a bare label."""
    return Input(cls=f"uk-input {kwargs.pop('cls', '')}".strip(), **kwargs)


def BigButton(text):
    """The one main action of a short form: full width and easy to hit with a thumb."""
    return Button(
        text, type="submit", cls="uk-btn uk-btn-primary w-full h-12 text-lg shadow-warm"
    )


def CauseChip(slug, label):
    return Span(
        label,
        cls="inline-flex items-center rounded-full px-3 py-1 text-sm font-bold "
        + CAUSE_CHIP.get(slug, "bg-sand text-ink"),
    )


def Wordmark(small=False):
    """A heart on a terracotta tile, then "causematch"."""
    return Div(
        Svg(
            Rect(width="32", height="32", rx="10", fill="currentColor"),
            Path(
                d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2"
                "-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z",
                fill="#fff",
                transform="translate(6 7) scale(0.833)",
            ),
            viewBox="0 0 32 32",
            width="28" if small else "36",
            height="28" if small else "36",
            aria_hidden="true",
            cls="text-brand",
        ),
        Span(
            "cause",
            Span("match", cls="text-brand"),
            cls="font-display font-extrabold tracking-tight text-ink "
            + ("text-2xl" if small else "text-3xl"),
        ),
        cls="flex items-center gap-2",
    )


def SignInPage(title, *content, error=None, intro=None, aside=None):
    """A sign-in step: the wordmark, then one warm card holding the form."""
    return Div(
        Div(Wordmark(), cls="flex justify-center pt-10 pb-6"),
        Div(
            H1(title, cls="font-display text-2xl font-extrabold text-ink"),
            P(intro, cls="text-ink-muted mt-1") if intro else None,
            Alert(error, cls=AlertT.error + " mt-4") if error else None,
            Div(*content, cls="mt-6 space-y-4"),
            cls="uk-card uk-card-body p-6",
        ),
        aside,
        Div(PrivacyFooter(), cls="text-center"),
        cls="mx-auto max-w-md px-4 pb-10",
    )


def DemoBanner():
    return P(
        PREVIEW_NOTICE if is_preview() else DEMO_NOTICE,
        role="note",
        cls="bg-ink text-cream text-center text-xs font-semibold px-4 py-1.5",
    )


def body_wrap(content):
    """Every full page (main.py's fast_app): the demo banner first, when it is on."""
    return (DemoBanner(), content) if demo_banner() else content


def Router():
    """Every page router: FastHTML's APIRouter ignores the app's own body_wrap, so each
    router passes it again (the demo banner, when it is on)."""
    return APIRouter(body_wrap=body_wrap)


def Checks(name, options, checked=(), cls="space-y-1"):
    return Div(
        *[
            LabelCheckboxX(label, name=name, value=value, checked=value in checked)
            for value, label in options
        ],
        cls=cls,
    )


def Choice(name, options, selected="", placeholder=None):
    return Select(
        Option(placeholder or "Choose one", value="", selected=not selected),
        *[
            Option(label, value=value, selected=value == selected)
            for value, label in options
        ],
        name=name,
        cls="uk-select",
    )


# 24 px line icons, as path data: stroked in the text color (Icon below)
HEART = (
    (
        "M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2"
        "-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"
    ),
)
CARDS = (
    "M4 8a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Z",
    "M8 2h10a2 2 0 0 1 2 2v12",
)
PERSON = ("M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z", "M4 21a8 8 0 0 1 16 0")
CROSS = ("M18 6 6 18", "M6 6l12 12")
CALENDAR = (
    "M8 2v4M16 2v4M3 10h18",
    "M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z",
)
PIN = (
    "M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21Z",
    "M12 12a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z",
)
PEOPLE = (
    "M16 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 4 18.5V20",
    "M10 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z",
    "M20 20v-1.5a3.5 3.5 0 0 0-2.5-3.35M15.5 4.15a3.5 3.5 0 0 1 0 6.7",
)

CHAT = ("M21 12a8 8 0 0 1-11.6 7.1L4 21l1.9-5.4A8 8 0 1 1 21 12Z",)
SEND = ("M22 2 11 13", "M22 2 15 22l-4-9-9-4Z")
BACK = ("m15 6-6 6 6 6",)
NEXT = ("m9 6 6 6-6 6",)
CLOCK = ("M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "M12 7v5l3 2")
SHIELD = ("M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6Z",)
SIGN_OUT = ("M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3", "M10 17l-5-5 5-5", "M5 12h11")
TRASH = (
    "M4 7h16M10 11v6M14 11v6M9 7V4h6v3",
    "M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12",
)
# A status pill's tones; literal class strings, so Tailwind's scan of app/ finds them
PILL = {
    "wait": "bg-cause-food-soft text-cause-food-ink",
    "good": "bg-cause-parks-soft text-cause-parks-ink",
    "info": "bg-cause-homeless-soft text-cause-homeless-ink",
    "off": "bg-sand text-ink-muted",
}
TITLE = "font-display text-2xl font-extrabold text-ink my-4"  # a volunteer page's H1
# a small text link that is still a 44 px tap target
SMALL_LINK = (
    "inline-flex min-h-11 items-center font-semibold text-ink-muted underline "
    "underline-offset-4 hover:text-ink"
)
CARD = "rounded-3xl border border-line bg-white shadow-warm"


def Icon(paths, size=24, filled=False, cls=""):
    """A line icon in the current text color; decorative, so hidden from screen readers."""
    return Svg(
        *[Path(d=d) for d in paths],
        viewBox="0 0 24 24",
        width=str(size),
        height=str(size),
        fill="currentColor" if filled else "none",
        stroke="none" if filled else "currentColor",
        stroke_width="2",
        stroke_linecap="round",
        stroke_linejoin="round",
        aria_hidden="true",
        cls=f"shrink-0 {cls}".strip(),
    )


def Meta(icon, text):
    """One fact of an opening (when, where, how many), after its icon."""
    return P(
        Icon(icon, size=16, cls="text-ink-muted"),
        Span(text),
        cls="flex items-center gap-2 text-sm text-ink",
    )


def when(starts_at):
    return datetime.fromisoformat(starts_at).strftime("%a %b %-d, %-I:%M %p")


def BackLink(href, text):
    return A(
        Icon(BACK, size=18),
        text,
        href=href,
        cls="-ml-1 inline-flex min-h-11 items-center gap-1 text-sm font-semibold "
        "text-ink-muted hover:text-ink",
    )


def Pill(text, tone):
    """A status: a dot and a word or two on a soft tint (PILL)."""
    return Span(
        Span(cls="h-1.5 w-1.5 rounded-full bg-current", aria_hidden="true"),
        text,
        cls="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs "
        "font-bold " + PILL[tone],
    )


def AddToHomeScreen():
    """A one-line hint at the top of the deck, hidden until static/a2hs.js decides
    (iPhone Safari, in a tab, not dismissed). In the page's flow, never fixed: fixed, it
    covered Pass and Like. The outer box has no display class, so `hidden` holds."""
    return Div(
        Div(
            P(
                "Tip: tap Share, then Add to Home Screen",
                cls="min-w-0 truncate text-xs font-semibold",
            ),
            Button(
                Icon(CROSS, size=20),
                Span("Dismiss", cls="sr-only"),
                type="button",
                id="a2hs-close",
                cls="grid h-11 w-11 shrink-0 place-items-center rounded-full "
                "hover:bg-white/10 focus-visible:outline focus-visible:outline-2 "
                "focus-visible:outline-cream",
            ),
            cls="mx-auto flex max-w-md items-center justify-between gap-2 "
            "rounded-full bg-ink pl-4 text-cream shadow-warm",
        ),
        id="a2hs",
        hidden=True,
        cls="mb-2",
    )


# (key, label, href, icon) of each tab in the volunteer's bottom bar
TABS = (
    ("openings", "Openings", "/deck", CARDS),
    ("matches", "Matches", "/matches", HEART),
    ("me", "Me", "/profile", PERSON),
)


# a like's confirmation, on the deck and on the card after the first like
LIKE_SENT = "Sent to {}. We'll text you when they say yes."


def MatchesDot(on, oob=False):
    """The small dot on the Matches tab while a like waits for a nonprofit. Always in the
    page (hidden when off), so a like's response can swap it in without a reload."""
    return Span(
        Span(" (a like is waiting)", cls="sr-only"),
        id="matches-dot",
        hidden=not on,
        hx_swap_oob="true" if oob else None,
        cls="absolute left-1/2 top-2 ml-3 h-2.5 w-2.5 rounded-full bg-brand "
        "ring-2 ring-white",
    )


def TabBar(active, dot=False):
    def tab(key, label, href, icon):
        on = key == active
        # the current tab differs by more than hue: a brand bar on top, a heavier label
        return A(
            Span(cls="absolute inset-x-6 top-0 h-[3px] rounded-b-full bg-brand")
            if on
            else None,
            Span(
                Icon(icon, size=22),
                cls="grid h-8 w-14 place-items-center rounded-full "
                + ("bg-brand-soft" if on else ""),
            ),
            Span(label),
            MatchesDot(dot) if key == "matches" else None,
            href=href,
            aria_current="page" if on else None,
            cls="relative flex h-16 flex-col items-center justify-center gap-0.5 text-xs "
            + (
                "font-extrabold text-brand-ink"
                if on
                else "font-semibold text-ink-muted hover:text-ink"
            ),
        )

    return Nav(
        Div(*[tab(*t) for t in TABS], cls="mx-auto grid max-w-md grid-cols-3"),
        aria_label="Main",
        id="tabbar",
        cls="cm-tabbar fixed inset-x-0 bottom-0 z-30 border-t border-line bg-white/95",
    )


def VolunteerNav(active, uid):
    """The volunteer's header and bottom tab bar, with the Matches dot while a like of
    theirs waits. The bar is fixed, so it can sit anywhere in the page; static/theme.css
    pads the body above it."""
    return Div(
        Div(
            A(
                Wordmark(small=True),
                href="/deck",
                cls="inline-flex min-h-11 items-center",
            ),
            cls="flex items-center pt-2 pb-2",
        ),
        TabBar(active, data.has_pending_like(uid)),
    )


def VolunteerShell(active, *content, uid):
    """A volunteer page: the header and tab bar, then the content in one narrow column.
    uid is required, so no page leaves out the Matches dot."""
    return Container(VolunteerNav(active, uid), *content, cls="max-w-md pb-8")


# a page's H1 and a section's H2 on the staff pages (nonprofit, admin)
ORG_TITLE = "font-display text-2xl font-extrabold text-ink"
ORG_SECTION = "font-display text-lg font-extrabold text-ink"
# (key, label, href) of each section of the nonprofit's top nav
ORG_TABS = (
    ("openings", "Openings", "/org"),
    ("inbox", "Inbox", "/org/inbox"),
    ("shifts", "Shifts", "/org/shifts"),
    ("organization", "Organization", "/org/organization"),
)


def TopNav(tabs, active, label, home, badges=None):
    """A staff header (nonprofit, admin): the wordmark, the sections (a tab can carry a
    count badge) and Sign out. One row on a desk, two on a phone."""
    badges = badges or {}

    def tab(key, text, href):
        on = key == active
        return A(
            text,
            Span(
                str(badges[key]),
                Span(" waiting", cls="sr-only"),
                cls="grid h-6 min-w-6 place-items-center rounded-full bg-brand px-1.5 "
                "text-xs text-white",
            )
            if badges.get(key)
            else None,
            href=href,
            aria_current="page" if on else None,
            cls="inline-flex min-h-11 shrink-0 items-center gap-2 rounded-full px-4 "
            "font-bold "
            + (
                "bg-brand-soft text-brand-ink"
                if on
                else "text-ink-muted hover:bg-sand hover:text-ink"
            ),
        )

    return Div(
        Div(
            A(
                Wordmark(small=True),
                href=home,
                cls="order-1 inline-flex min-h-11 items-center",
            ),
            Form(
                Button(
                    "Sign out",
                    type="submit",
                    cls="min-h-11 rounded-full px-4 font-bold text-ink-muted "
                    "hover:bg-sand hover:text-ink",
                ),
                action="/logout",
                method="post",
                cls="order-2 ml-auto lg:order-3 lg:ml-0",
            ),
            Nav(
                *[tab(*t) for t in tabs],
                aria_label=label,
                cls="order-3 -mx-2 flex w-full gap-1 overflow-x-auto lg:order-2 "
                "lg:mx-0 lg:ml-6 lg:w-auto lg:flex-1",
            ),
            # uk-container: the same side padding as the page below
            cls="uk-container flex max-w-5xl flex-wrap items-center gap-y-2 py-3",
        ),
        cls="border-b border-line bg-white",
    )


def OrgNav(active, waiting=0):
    """The nonprofit's header; the Inbox tab counts the volunteers waiting for an answer."""
    return TopNav(ORG_TABS, active, "Nonprofit", "/org", {"inbox": waiting})


def OrgShell(active, waiting, *content):
    """A nonprofit page: the top nav, then the content, up to two columns wide on a desk."""
    return Div(OrgNav(active, waiting), Container(*content, cls="max-w-5xl pt-6 pb-12"))


def SimplePage(title, *content):
    """A page outside any signed-in shell (privacy, not found): the wordmark home, then
    one warm card. The tab says which page it is."""
    return Title(f"{title} - Causematch"), Div(
        A(Wordmark(small=True), href="/", cls="inline-flex pt-6 pb-4"),
        Div(
            H1(title, cls="font-display text-2xl font-extrabold text-ink"),
            *content,
            cls="p-6 sm:p-8 " + CARD,
        ),
        cls="mx-auto max-w-2xl px-4 pb-12",
    )


def Refusal(status):
    """A route's 403 or 400 in the shared look (app/security.py's page), not a bare word."""
    from app.security import page  # imported here: security loads the data layer

    if status == 403:
        return page(
            403,
            "Not available",
            "That isn't yours to see or change. Go back and try again.",
        )
    return page(
        400,
        "Something was missing",
        "That request was incomplete. Go back to the page and try again.",
    )


# Avatar sizes; literal class strings, so Tailwind's scan of app/ finds them
AVATAR = {
    "sm": "h-9 w-9 text-base",
    "md": "h-14 w-14 text-2xl",
    "lg": "h-28 w-28 text-5xl",
}


def Avatar(name, photo_key=None, size="md", **kwargs):
    """A volunteer's round photo (its 160 px square), or the first letter of their name.
    Decorative: the name is always written next to it. Pass a photo_key only to someone
    data.photo_for serves it to (the org's lists blank it under a block)."""
    cls = "shrink-0 rounded-full " + AVATAR[size]
    if photo_key:
        return Img(
            src=f"/photos/thumb/{photo_key}.jpg",
            alt="",
            cls=cls + " object-cover ring-2 ring-white shadow-warm",
            **kwargs,
        )
    return Span(
        ((name or "").strip()[:1] or "?").upper(),
        aria_hidden="true",
        cls=cls + " grid place-items-center bg-brand font-display font-extrabold "
        "text-white",
        **kwargs,
    )


def EventLink(url, cls=""):
    """The opening as the nonprofit advertises it, in a new tab: "See it on example.org ↗".
    `url` was checked as https when it was saved (forms.https_url)."""
    if not url:
        return None
    return A(
        f"See it on {domain(url)}",
        Span(" ↗", aria_hidden="true"),
        Span(" (opens in a new tab)", cls="sr-only"),
        href=url,
        target="_blank",
        rel="noopener noreferrer",
        draggable="false",  # a mouse drag on a swipe card moves the card
        cls="inline-flex min-h-11 items-center text-sm font-bold text-brand-ink "
        "underline underline-offset-4 " + cls,
    )


def LogoBadge(key, cls):
    """An org's logo as a round badge: photos.process drew it centred on its own square
    background, small enough that the circle keeps all of it."""
    return Img(
        src=f"/photos/{key}.jpg",
        alt="",
        draggable="false",
        cls="pointer-events-none rounded-full object-cover ring-4 ring-white "
        "shadow-warm " + cls,
    )


def VolunteerHeader(name, photo_key=None, pronouns=None):
    """A volunteer as a nonprofit sees them: photo or initial, name, pronouns."""
    return Div(
        Avatar(name, photo_key, "sm"),
        Span(
            Span(name, cls="font-semibold text-ink"),
            Span(f" ({pronouns})", cls="text-sm text-ink-muted") if pronouns else None,
        ),
        cls="flex items-center gap-3",
    )


def OrgFace(org, logo=None, photo=None, slug=None):
    """A nonprofit at 48 px on the volunteer's lists: its logo, else the opening's photo,
    else its initial on its cause's tint. Decorative: the org's name is written next to it.
    Logos and opening photos are public to anyone signed in (data.photo_for)."""
    cls = "h-12 w-12 shrink-0 rounded-full"
    if logo or photo:
        return Img(
            src=f"/photos/thumb/{logo or photo}.jpg",
            alt="",
            loading="lazy",
            width="48",
            height="48",
            cls=cls + " object-cover ring-1 ring-line",
        )
    return Span(
        ((org or "").strip()[:1] or "?").upper(),
        aria_hidden="true",
        cls=f"{cls} grid place-items-center font-display text-xl font-extrabold "
        + CAUSE_CHIP.get(slug, "bg-sand text-ink"),
    )


CALENDAR_HOURS = 2  # openings have no end time yet: a calendar gets this long
CALENDAR_ZONE = "America/Los_Angeles"  # the wall clock starts_at is stored in


def calendar_event(s):
    """What a calendar gets for an upcoming shift s (data.shift_event's keys): start and
    end as CALENDAR_ZONE wall clock "YYYYMMDDTHHMMSS", title, place (the neighborhood: no
    address is stored), and a line with the link back to the chat."""
    start = datetime.fromisoformat(s["starts_at"])
    end = start + timedelta(hours=CALENDAR_HOURS)
    return {
        "start": start.strftime("%Y%m%dT%H%M%S"),
        "end": end.strftime("%Y%m%dT%H%M%S"),
        "title": f"{s['title']} · {s['org']}",
        "place": s["neighborhood"] or "",
        "details": f"Chat with {s['org']}: {public_base_url()}/chat/{s['match_id']}",
    }


def AddToCalendar(s):
    """On an upcoming accepted shift: its .ics (iPhone opens it in Calendar) and a Google
    Calendar link, both from calendar_event."""
    e = calendar_event(s)
    google = "https://calendar.google.com/calendar/render?" + urlencode(
        {
            "action": "TEMPLATE",
            "text": e["title"],
            "dates": f"{e['start']}/{e['end']}",
            "ctz": CALENDAR_ZONE,
            "details": e["details"],
            "location": e["place"],
        },
        quote_via=quote,
    )
    return Div(
        Span(
            Icon(CALENDAR, size=16, cls="text-ink-muted"),
            "Add to calendar:",
            cls="inline-flex items-center gap-2 text-ink",
        ),
        A("Apple", href=f"/shifts/{s['id']}.ics", cls=SMALL_LINK),
        A(
            "Google",
            Span(" (opens in a new tab)", cls="sr-only"),
            href=google,
            target="_blank",
            rel="noopener noreferrer",
            cls=SMALL_LINK,
        ),
        cls="flex flex-wrap items-center gap-x-4 text-sm",
    )


def Notes(text):
    """An opening's notes ("closed-toe shoes", parking), shown once the volunteer is in."""
    if not text:
        return None
    return Div(  # not a P: Franken UI spaces a p after a p
        Span("Good to know: ", cls="font-bold text-ink"),
        text,
        cls="whitespace-pre-line break-words text-sm text-ink-muted",
    )
