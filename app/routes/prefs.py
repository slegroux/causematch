"""/me/preferences: what a volunteer wants from the deck, editable at any time. Places,
cause ratings, dates, kind of work, skills and the Only-show choices stay private; the
sections a nonprofit sees after a like (choices.SHARED_WITH_NONPROFIT) say so."""

from datetime import datetime

from fasthtml.common import (
    H1,
    H2,
    A,
    Button,
    Datalist,
    Div,
    Fieldset,
    Form,
    FtResponse,
    Hidden,
    Input,
    Label,
    Legend,
    Option,
    P,
    Redirect,
    Script,
    Select,
    Span,
)
from monsterui.all import Alert, AlertT
from starlette.concurrency import run_in_threadpool

from app import data, prefs
from app.choices import (
    COMMITMENT,
    DAYS,
    DEALBREAKERS,
    EFFORT,
    FREQUENCY,
    GROUP,
    HORIZON,
    HOURS,
    LANGUAGES,
    MAX_FREE_DATES,
    MAX_MINUTES,
    MAX_PLACES,
    MODES,
    PARTS,
    SETTING,
    SKILLS,
    URGENT_PER_WEEK,
    URGENT_WINDOWS,
)
from app.forms import bad_request, query, shown_point, str_form
from app.geo import neighborhoods
from app.ui import (
    BACK,
    CARD,
    HEART,
    PEOPLE,
    PIN,
    SHIELD,
    TITLE,
    BigButton,
    CauseChip,
    Field,
    Icon,
    Router,
    SimplePage,
    TextInput,
    VolunteerShell,
)

ar = Router()

# A volunteer's kind-of-work answers: the opening's choices plus "doesn't matter"
SETTINGS = [*SETTING, ("either", "Either")]
EFFORTS = [*EFFORT, ("any", "Any")]
GROUPS = [*GROUP, ("any", "Any")]

SLOTS = {f"{d}_{p}" for d, _ in DAYS for p, _ in PARTS}
CHECK = ("M5 12.5 10 17.5 19.5 7",)
GRID = "grid grid-cols-[2.75rem_repeat(3,minmax(0,1fr))] items-center gap-2"

NO_CAUSE = "Pick at least one cause you love or are open to."
QUIET_SAME = "Quiet hours need a start and an end that differ."
NO_PLACE = "Tell us your main place: a neighborhood, a ZIP code or your location."

# A toggle chip: a visually hidden radio or checkbox, then the pill that shows its state.
CHIP = (
    "flex min-h-11 cursor-pointer select-none items-center justify-center gap-1.5 "
    "rounded-full border border-line bg-white px-3 py-2 text-center text-sm font-bold "
    "leading-tight text-ink transition hover:border-ink-muted "
    "peer-focus-visible:outline peer-focus-visible:outline-2 "
    "peer-focus-visible:outline-offset-2 peer-focus-visible:outline-brand"
)
CHIP_ON = (
    "peer-checked:border-brand peer-checked:bg-brand-soft peer-checked:text-brand-ink"
)
# Love and open in each cause's own color: solid, then its soft tint. Literal class
# strings, so Tailwind's scan of app/ finds them.
CAUSE_ON = {
    "food-insecurity": {
        prefs.LOVE: "peer-checked:border-cause-food peer-checked:bg-cause-food "
        "peer-checked:text-white",
        prefs.OPEN: "peer-checked:border-cause-food peer-checked:bg-cause-food-soft "
        "peer-checked:text-cause-food-ink",
    },
    "environment-parks": {
        prefs.LOVE: "peer-checked:border-cause-parks peer-checked:bg-cause-parks "
        "peer-checked:text-white",
        prefs.OPEN: "peer-checked:border-cause-parks peer-checked:bg-cause-parks-soft "
        "peer-checked:text-cause-parks-ink",
    },
    "animal-shelters": {
        prefs.LOVE: "peer-checked:border-cause-animals peer-checked:bg-cause-animals "
        "peer-checked:text-white",
        prefs.OPEN: "peer-checked:border-cause-animals "
        "peer-checked:bg-cause-animals-soft peer-checked:text-cause-animals-ink",
    },
    "homelessness-services": {
        prefs.LOVE: "peer-checked:border-cause-homeless peer-checked:bg-cause-homeless "
        "peer-checked:text-white",
        prefs.OPEN: "peer-checked:border-cause-homeless "
        "peer-checked:bg-cause-homeless-soft peer-checked:text-cause-homeless-ink",
    },
}
NOT_FOR_ME = (
    "peer-checked:border-ink-muted peer-checked:bg-sand peer-checked:text-ink-muted "
    "peer-checked:line-through"
)


def Chip(kind, name, value, text, checked, on=CHIP_ON, icon=None):
    """kind: radio | checkbox."""
    return Label(
        Input(
            type=kind,
            name=name,
            value=value,
            checked=checked,
            cls="peer sr-only " + ("uk-radio" if kind == "radio" else "uk-checkbox"),
        ),
        Span(icon, text, cls=f"{CHIP} {on}"),
        cls="relative block",
    )


SHARED = "Shared with a nonprofit after you like its opening"


def SharedNote(detail=""):
    """Marks what a nonprofit sees (choices.SHARED_WITH_NONPROFIT); the rest is private."""
    return P(
        Icon(PEOPLE, size=16, cls="mt-0.5"),
        Span(SHARED + detail),
        cls="flex gap-1.5 text-xs font-bold text-brand-ink",
    )


def Section(title, hint, *content, shared=None):
    """shared: None for a private section, else the detail its SharedNote adds."""
    return Div(
        H2(title, cls="font-display text-lg font-extrabold text-ink"),
        P(hint, cls="-mt-2 text-sm text-ink-muted") if hint else None,
        SharedNote(shared) if shared is not None else None,
        *content,
        cls="space-y-3 p-5 " + CARD,
    )


def CauseRow(cause, state):
    """One cause, rated love / open / not for me; no choice leaves it unrated."""
    on = CAUSE_ON.get(cause.slug, {})
    return Fieldset(
        Legend(CauseChip(cause.slug, cause.label), cls="mb-2"),
        Div(
            *[
                Chip(
                    "radio",
                    f"cause_{cause.slug}",
                    value,
                    text,
                    state == value,
                    NOT_FOR_ME if value == prefs.NO else on.get(value, CHIP_ON),
                    Icon(HEART, size=16, filled=True) if value == prefs.LOVE else None,
                )
                for value, text in prefs.CAUSE_STATES
            ],
            cls="grid grid-cols-3 gap-2",
        ),
    )


def Checks(name, options, checked, cls):
    return Div(
        *[Chip("checkbox", name, v, label, v in checked) for v, label in options],
        cls=cls,
    )


def DayGrid(checked):
    """7 days x morning / afternoon / evening, each a checkbox named availability."""
    return Fieldset(
        Legend("Usually free", cls="sr-only"),
        Div(
            Span(),
            *[
                Span(label, cls="text-center text-xs font-bold text-ink-muted")
                for _, label in PARTS
            ],
            cls=GRID,
        ),
        *[
            Div(
                Span(day, cls="font-bold text-ink"),
                *[
                    Label(
                        Input(
                            type="checkbox",
                            name="availability",
                            value=f"{d}_{p}",
                            checked=f"{d}_{p}" in checked,
                            cls="peer sr-only uk-checkbox",
                        ),
                        Span(
                            Span(f"{day} {part.lower()}", cls="sr-only"),
                            Icon(CHECK, size=20, cls="stroke-[3]"),
                            cls="grid h-11 cursor-pointer place-items-center rounded-xl "
                            "border border-line bg-white text-transparent transition "
                            "hover:border-ink-muted peer-checked:border-brand "
                            "peer-checked:bg-brand peer-checked:text-white "
                            "peer-focus-visible:outline peer-focus-visible:outline-2 "
                            "peer-focus-visible:outline-offset-2 "
                            "peer-focus-visible:outline-brand",
                        ),
                        cls="relative block",
                    )
                    for p, part in PARTS
                ],
                cls=GRID,
            )
            for d, day in DAYS
        ],
        cls="space-y-2",
    )


def FreeDates(dates):
    """The dates already given (untick to drop one), and a date field to add one."""
    return Fieldset(
        Legend("Specific dates you're free", cls="uk-form-label mb-1.5"),
        Div(
            *[
                Chip(
                    "checkbox",
                    "free_dates",
                    d,
                    datetime.fromisoformat(d).strftime("%a %b %-d"),
                    True,
                )
                for d in dates
            ],
            cls="mb-2 flex flex-wrap gap-2",
        )
        if dates
        else None,
        Label("Add a date", fr="free_date_new", cls="sr-only"),
        TextInput(
            type="date",
            id="free_date_new",
            name="free_date_new",
            min=data.local_today(),
        )
        if len(dates) < MAX_FREE_DATES
        else None,
        P(
            f"A date counts as free all day. Up to {MAX_FREE_DATES}; past ones drop off.",
            cls="mt-1.5 text-sm text-ink-muted",
        ),
    )


def Group(title, cls, name, options, value):
    """A row of radio chips under a small heading."""
    return Fieldset(
        Legend(title, cls="uk-form-label mb-1.5"),
        Div(
            *[
                Chip("radio", name, str(v), label, str(value) == str(v))
                for v, label in options
            ],
            cls=cls,
        ),
    )


def Hour(name, value):
    return Select(
        *[Option(label, value=str(h), selected=h == value) for h, label in HOURS],
        name=name,
        cls="uk-select",
    )


def LastMinute(u):
    """The opt-in to urgent calls: off unless turned on, and private."""
    return Section(
        "Last-minute calls",
        "When a nonprofit near you is short of people in the next 48 hours and you're "
        "free then, we can text you (or email you, without a number). Off unless you "
        "turn it on. Nonprofits never see these settings.",
        Hidden(name="sections", value="urgent"),
        Chip(
            "checkbox",
            "urgent_on",
            "1",
            "Tell me about last-minute calls",
            u["on"],
        ),
        Group(
            "For shifts starting",
            "grid grid-cols-2 gap-2",
            "urgent_window",
            URGENT_WINDOWS,
            u["window"],
        ),
        Div(
            Field("No texts from", Hour("urgent_quiet_from", u["quiet_from"])),
            Field("Until", Hour("urgent_quiet_to", u["quiet_to"])),
            cls="grid grid-cols-2 gap-2",
        ),
        Group(
            "At most, a week",
            "grid grid-cols-3 gap-2",
            "urgent_per_week",
            URGENT_PER_WEEK,
            u["per_week"],
        ),
    )


def PlaceFields(slot, pf, named=True):
    """One place: its name, where it is (a neighborhood, a ZIP or the device location), how
    the volunteer gets there and how long they'd travel. Field names: place1_*, place2_*.
    named=False (onboarding): no name field, the place is Home."""
    n = f"place{slot + 1}"
    return Fieldset(
        Legend(
            "Your main place" if slot == 0 else "A second place (optional)",
            cls="font-bold text-ink" if named else "sr-only",
        ),
        P(
            "Leave “Neighborhood or ZIP” empty to skip it.",
            cls="text-sm text-ink-muted",
        )
        if slot
        else None,
        Div(
            Label("Name", fr=f"{n}_label", cls="uk-form-label"),
            TextInput(
                id=f"{n}_label",
                name=f"{n}_label",
                value=pf["label"],
                maxlength=str(prefs.LABEL_MAX),
                autocomplete="off",
            ),
            cls="space-y-1.5",
        )
        if named
        else None,
        Div(
            Label("Neighborhood or ZIP", fr=f"{n}_where", cls="uk-form-label"),
            TextInput(
                id=f"{n}_where",
                name=f"{n}_where",
                value=pf["where"],
                list="la-areas",
                maxlength="80",
                autocomplete="off",
                placeholder="e.g. Mar Vista or 90066",
                data_where=n,
            ),
            Hidden(id=f"{n}_lat", name=f"{n}_lat", value=pf["lat"]),
            Hidden(id=f"{n}_lon", name=f"{n}_lon", value=pf["lon"]),
            # static/geo.js shows this when the browser can locate; no display class here,
            # so `hidden` holds until then
            Div(
                Button(
                    Icon(PIN, size=18),
                    "Use my location",
                    type="button",
                    data_locate=n,
                    cls="uk-btn uk-btn-default h-11 gap-2 px-4",
                ),
                hidden=True,
                data_locate_box="1",
            ),
            P(id=f"{n}_status", aria_live="polite", cls="text-sm text-ink-muted"),
            cls="space-y-1.5",
        ),
        Group(
            "Getting there", "grid grid-cols-4 gap-2", f"{n}_mode", MODES, pf["mode"]
        ),
        Group(
            "Up to (minutes)",
            "grid grid-cols-5 gap-1.5",
            f"{n}_minutes",
            MAX_MINUTES,
            pf["minutes"],
        ),
        cls="space-y-3 rounded-2xl bg-cream p-4",
    )


def PrefsPage(f, error=None, saved=False):
    return VolunteerShell(
        "me",
        A(
            Icon(BACK, size=18),
            "Me",
            href="/profile",
            cls="-ml-1 inline-flex min-h-11 items-center gap-1 font-bold text-ink-muted "
            "hover:text-ink",
        ),
        H1("Preferences", cls=TITLE + " !mt-0"),
        P(
            Icon(SHIELD, size=16, cls="mt-0.5 text-ink-muted"),
            Span(
                "These decide which openings you see, and in what order. Your places, "
                "cause ratings, dates, skills, Only-show choices and the kind of work "
                "you want stay private. What's marked shared (your causes, a summary of "
                "when you're free, the languages you speak and how often) a nonprofit "
                "sees only after you like one of its openings (",
                A("Privacy", href="/privacy", cls="underline"),
                ").",
            ),
            cls="-mt-2 mb-4 flex gap-2 text-sm text-ink-muted",
        ),
        Alert(error, cls=AlertT.error + " mb-4") if error else None,
        Alert(
            "Saved. Your deck now follows these preferences.",
            cls=AlertT.success + " mb-4",
        )
        if saved and not error
        else None,
        Form(
            Section(
                "Causes",
                "Love ranks first. Not for me hides every opening for that cause.",
                *[CauseRow(c, f["causes"].get(c.slug)) for c in data.active_causes()],
                shared=": the causes you love or are open to, not how you rated them.",
            ),
            Section(
                "Where you'll help from",
                "Each card shows the time from the nearest of your places. Only you see "
                "them; we keep an area, never an address.",
                *[PlaceFields(i, pf) for i, pf in enumerate(f["place_forms"])],
                Datalist(*[Option(value=n) for n in neighborhoods()], id="la-areas"),
            ),
            Section(
                "When",
                "Tick the times you're usually free: openings then rank first.",
                DayGrid(f["availability"]),
                FreeDates(f["free_dates"]),
                Group(
                    "How far ahead to look",
                    "flex flex-wrap gap-2",
                    "horizon_days",
                    HORIZON,
                    f["horizon_days"],
                ),
                Group(
                    "One-time or recurring",
                    "grid grid-cols-3 gap-2",
                    "commitment",
                    COMMITMENT,
                    f["commitment"],
                ),
                shared=": a summary of the times you tick (weekday or weekend mornings, "
                "afternoons, evenings). Your dates stay private.",
            ),
            Section(
                "Kind of work",
                "Openings that say they fit rank higher; ones that don't say aren't held "
                "back.",
                Hidden(name="sections", value="work"),
                Group(
                    "Indoors or outdoors",
                    "grid grid-cols-3 gap-2",
                    "setting",
                    SETTINGS,
                    f["setting"],
                ),
                Group(
                    "How active",
                    "grid grid-cols-4 gap-1.5",
                    "effort",
                    EFFORTS,
                    f["effort"],
                ),
                Group(
                    "Group size",
                    "grid grid-cols-2 gap-2",
                    "group_size",
                    GROUPS,
                    f["group_size"],
                ),
                Div(
                    Chip(
                        "checkbox",
                        "first_timer",
                        "1",
                        "I'm new: first-timer friendly first",
                        f["first_timer"],
                    ),
                    Chip(
                        "checkbox",
                        "orientation_ok",
                        "1",
                        "A separate orientation session is fine",
                        f["orientation_ok"],
                    ),
                    cls="flex flex-wrap gap-2",
                ),
                Fieldset(
                    Legend(
                        "Languages you speak besides English",
                        cls="uk-form-label mb-1.5",
                    ),
                    SharedNote("."),
                    Checks(
                        "languages", LANGUAGES, f["languages"], "flex flex-wrap gap-2"
                    ),
                ),
            ),
            Section(
                "Only show, or prefer?",
                "Prefer ranks what fits first and still shows the rest. Only show hides "
                "what doesn't fit. Causes that are not for you are always hidden.",
                Hidden(name="sections", value="only"),
                *[
                    Group(
                        label,
                        "grid grid-cols-2 gap-2",
                        f"only_{key}",
                        [("prefer", "Prefer"), ("only", "Only show")],
                        "only" if key in f["dealbreakers"] else "prefer",
                    )
                    for key, label in DEALBREAKERS
                ],
                Chip(
                    "checkbox",
                    "surprise",
                    "1",
                    "Surprise me: now and then, a cause beyond the ones I love",
                    f["surprise"],
                ),
            ),
            Section(
                "How often",
                None,
                Div(
                    *[
                        Chip("radio", "frequency", v, label, f["frequency"] == v)
                        for v, label in FREQUENCY
                    ],
                    cls="flex flex-wrap gap-2",
                ),
                shared=".",
            ),
            Section(
                "What you bring",
                "Openings that need it rank higher.",
                Checks("skills", SKILLS, f["skills"], "flex flex-wrap gap-2"),
            ),
            LastMinute(f["urgent"]),
            Div(BigButton("Save preferences"), cls="cm-savebar z-20 -mx-1 py-3"),
            action="/me/preferences",
            method="post",
            cls="space-y-4",
        ),
        Script(src="/static/geo.js", defer=True),
    )


def form_values(saved):
    """data.preferences() as the page shows it: each place as its form fields."""
    places = saved.pop("places")
    saved["place_forms"] = [
        prefs.place_form(places[i] if i < len(places) else None, i)
        for i in range(MAX_PLACES)
    ]
    return saved


@ar("/me/preferences", methods=["GET"])
def preferences_page(req):
    return PrefsPage(
        form_values(data.preferences(req.scope["uid"])),
        saved=query(req, "saved") == "1",
    )


@ar("/me/preferences", methods=["POST"])
async def preferences_save(req):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_save, req, form)


def _sync_save(req, form):
    uid = req.scope["uid"]
    saved = data.preferences(uid)
    active = [c.slug for c in data.active_causes()]
    # a cause the posted page didn't rate keeps its saved rating (a page loaded before
    # the cause came back); a rated cause always sends its radio
    rated = {
        s: form.get(f"cause_{s}") or saved["causes"][s]
        for s in active
        if form.get(f"cause_{s}") or s in saved["causes"]
    }
    f = {
        # a rating of a cause that is inactive now is kept for when it comes back
        "causes": {
            **{s: v for s, v in saved["causes"].items() if s not in active},
            **rated,
        },
        "availability": form.getlist("availability"),
        "free_dates": form.getlist("free_dates"),
        # A field the form doesn't carry keeps its saved value: a page loaded before a
        # section existed must not reset it. A radio group on the page sends its checked
        # value (and these always have one); a section's checkboxes count only when its
        # "sections" marker came with the form.
        "horizon_days": form.get("horizon_days", str(saved["horizon_days"])),
        "commitment": form.get("commitment", saved["commitment"]),
        "setting": form.get("setting", saved["setting"]),
        "effort": form.get("effort", saved["effort"]),
        "group_size": form.get("group_size", saved["group_size"]),
        **(
            {
                "first_timer": form.get("first_timer") == "1",
                "orientation_ok": form.get("orientation_ok") == "1",
                "languages": form.getlist("languages"),
            }
            if "work" in form.getlist("sections")
            else {k: saved[k] for k in ("first_timer", "orientation_ok", "languages")}
        ),
        "dealbreakers": [
            key
            for key, _ in DEALBREAKERS
            if form.get(f"only_{key}", "only" if key in saved["dealbreakers"] else "")
            == "only"
        ],
        "surprise": form.get("surprise") == "1"
        if "only" in form.getlist("sections")
        else saved["surprise"],
        "skills": form.getlist("skills"),
        "frequency": form.get("frequency", ""),
        "urgent": {
            "on": form.get("urgent_on") == "1",
            **{
                k: form.get(f"urgent_{k}", "")
                for k in ("window", "quiet_from", "quiet_to", "per_week")
            },
        }
        if "urgent" in form.getlist("sections")
        else saved["urgent"],
        "place_forms": [
            {
                k: form.get(f"place{i + 1}_{k}", "")
                for k in ("label", "where", "lat", "lon", "mode", "minutes")
            }
            for i in range(MAX_PLACES)
        ],
    }
    # the page shows dates as chips, so only valid ones go back into it
    dates, date_error = prefs.parse_free_dates(
        f["free_dates"], form.get("free_date_new", ""), data.local_today()
    )
    f["free_dates"] = saved["free_dates"] if date_error else dates
    # resolved from what was posted; an error page shows at most the rounded point
    posted = [dict(pf) for pf in f["place_forms"]]
    for pf in f["place_forms"]:
        pf["lat"], pf["lon"] = shown_point(pf["lat"], pf["lon"])
    u = f["urgent"]
    if (
        u["window"] not in {v for v, _ in URGENT_WINDOWS}
        or str(u["per_week"]) not in {str(v) for v, _ in URGENT_PER_WEEK}
        or not {str(u["quiet_from"]), str(u["quiet_to"])} <= {str(h) for h, _ in HOURS}
        or form.get("urgent_on") not in (None, "1")
    ):
        f["urgent"] = saved["urgent"]  # the page shows what is saved
        return PrefsPage(f, prefs.UNRECOGNIZED)
    u = f["urgent"] = {
        **u,
        **{k: int(u[k]) for k in ("quiet_from", "quiet_to", "per_week")},
    }
    if u["quiet_from"] == u["quiet_to"]:
        return PrefsPage(f, QUIET_SAME)
    if (
        not set(rated.values()) <= {v for v, _ in prefs.CAUSE_STATES}
        or not set(f["availability"]) <= SLOTS
        or f["horizon_days"] not in {str(v) for v, _ in HORIZON}
        or f["commitment"] not in {v for v, _ in COMMITMENT}
        or f["setting"] not in {v for v, _ in SETTINGS}
        or f["effort"] not in {v for v, _ in EFFORTS}
        or f["group_size"] not in {v for v, _ in GROUPS}
        or not set(f["languages"]) <= {v for v, _ in LANGUAGES}
        or any(
            form.get(f"only_{key}", "prefer") not in ("prefer", "only")
            for key, _ in DEALBREAKERS
        )
        or not set(f["skills"]) <= {v for v, _ in SKILLS}
        or f["frequency"] not in {v for v, _ in FREQUENCY} | {""}
    ):
        return PrefsPage(f, prefs.UNRECOGNIZED)
    if not {prefs.LOVE, prefs.OPEN} & set(rated.values()):
        return PrefsPage(f, NO_CAUSE)
    if date_error:
        return PrefsPage(f, date_error)
    places = []
    for i, pf in enumerate(posted):
        current = saved["places"][i] if i < len(saved["places"]) else None
        place, error = prefs.resolve_place(pf, current, i)
        if error:
            return PrefsPage(f, error)
        if place:
            places.append(place)
    if not places or not f["place_forms"][0]["where"].strip():
        return PrefsPage(f, NO_PLACE)
    data.save_preferences(
        uid,
        **{k: v for k, v in f.items() if k not in ("place_forms", "horizon_days")},
        horizon_days=int(f["horizon_days"]),
        places=places,
    )
    req.session.pop(
        "deck_view", None
    )  # the deck's filter sheet starts again from these
    return Redirect("/me/preferences?saved=1")


UNSUBSCRIBE = "Stop last-minute calls"


def _bad_unsubscribe():
    return FtResponse(
        SimplePage(
            UNSUBSCRIBE,
            P(
                "That link isn't valid. You can turn last-minute calls off in Me, then "
                "Preferences.",
                cls="mt-2 text-ink",
            ),
        ),
        status_code=404,
    )


@ar("/urgent/off/{key}", methods=["GET"])
def urgent_off_page(key: str):
    """An urgent email's unsubscribe link: a page that asks, so a mail scanner that
    follows links turns nothing off; the button posts."""
    if data.urgent_off_volunteer(key) is None:
        return _bad_unsubscribe()
    return SimplePage(
        UNSUBSCRIBE,
        P(
            "Stop texts and emails about nonprofits near you that are short of people "
            "at the last minute?",
            cls="mt-2 text-ink",
        ),
        Form(
            BigButton("Turn them off"), action=f"/urgent/off/{key}", method="post",
            cls="mt-4",
        ),
    )  # fmt: skip


@ar("/urgent/off/{key}", methods=["POST"])
def urgent_off(key: str):
    if not data.urgent_off(key):
        return _bad_unsubscribe()
    return SimplePage(
        UNSUBSCRIBE,
        P(
            "Done: no more last-minute calls. You can turn them on again in Me, then "
            "Preferences.",
            cls="mt-2 text-ink",
        ),
    )
