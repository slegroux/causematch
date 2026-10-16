import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fasthtml.common import (
    H1,
    H2,
    H3,
    A,
    Div,
    P,
    Progress,
    Redirect,
    Span,
    Textarea,
)
from fasthtml.common import Form as PlainForm
from monsterui.all import (
    Alert,
    AlertT,
    Button,
    ButtonT,
    Form,
    LabelCheckboxX,
    TextPresets,
)
from starlette.concurrency import run_in_threadpool

from app import data, notify
from app.auth import normalize_phone
from app.choices import (
    AVAILABILITY,
    EFFORT,
    FREQUENCY,
    GROUP,
    LANGUAGES,
    MAX_OPENING_SKILLS,
    SETTING,
    SKILLS,
)
from app.forms import HIDDEN_CHARS_MESSAGE, hidden_chars, query, str_form
from app.geo import zip_latlon
from app.routes.shifts import RepeatForm
from app.ui import (
    CALENDAR,
    CARD,
    ORG_SECTION,
    ORG_TITLE,
    PIN,
    SMALL_LINK,
    Checks,
    Choice,
    Field,
    Icon,
    OrgShell,
    Pill,
    Refusal,
    Router,
    TextInput,
    VolunteerHeader,
)

ar = Router()

LA = ZoneInfo("America/Los_Angeles")
RECURRENCE = [
    ("one_time", "One-time"),
    ("weekly", "Weekly"),
    ("monthly", "Monthly"),
    ("flexible", "Flexible / ongoing"),
]
ORIENTATION = [
    ("no", "No"),
    ("same_day", "Yes, done the same day"),
    ("separate", "Yes, a separate session beforehand (describe below)"),
]
REPLY = [("email", "Email"), ("text", "Text"), ("either", "Either")]
MAX_AHEAD = timedelta(days=366)
BAD_PHONE = "Enter a US phone number, like (310) 555-0123, or leave it blank."


def reply_options(has_phone):
    """Text needs a number to send to."""
    return REPLY if has_phone else REPLY[:1]


def DetailsForm(profile, error=None):
    return Form(
        H2("Your details", cls="font-display text-lg font-extrabold text-ink"),
        P("Volunteers see your name and role.", cls=TextPresets.muted_sm),
        Field(
            'Your name and role, e.g. "Maria Lopez, volunteer coordinator"',
            TextInput(
                name="name",
                value=profile.display_name or "",
                maxlength="80",
                required=True,
            ),
        ),
        Field(
            "Phone (optional)",
            TextInput(
                name="phone",
                type="tel",
                inputmode="tel",
                value=profile.contact_phone or "",
                autocomplete="tel",
            ),
        ),
        Alert(error, cls=AlertT.error) if error else None,
        Button("Save details", cls=ButtonT.default),
        action="/org/details",
        method="post",
        id="details",
        cls="space-y-3 p-5 " + CARD,
    )


LIMITS = {"title": 100, "about": 1000, "neighborhood": 80, "notes": 500}
ADULT_ONLY = (
    "I confirm this opening is for adults (18+) and involves no one-to-one contact "
    "with vulnerable people"
)


def when(starts_at):
    return datetime.fromisoformat(starts_at).strftime("%a %b %-d, %-I:%M %p")


OPENING_STATUS = {
    "open": ("Open", "good"),
    "filled": ("Filled", "info"),
    "closed": ("Closed", "off"),
}


def Meta(icon, text):
    return P(
        Icon(icon, size=16, cls="text-ink-muted"),
        Span(text),
        cls="flex items-center gap-2 text-sm text-ink",
    )


def UrgentLine(o, urgent, accepted):
    """The opening's open urgent call and a way to stop it, or a link to post one."""
    if urgent:
        call = urgent["call"]
        return Div(
            Pill(
                data.urgent_label(call.starts_at, call.needed - urgent["found"]), "wait"
            ),
            P(
                f"{urgent['found']} of {call.needed} found · "
                + (
                    "the first ones who say yes are in"
                    if call.auto_accept
                    else "you decide each one"
                ),
                cls="text-xs font-semibold text-ink-muted",
            ),
            PlainForm(
                Button("Stop urgent call", cls=ButtonT.default),
                action=f"/org/openings/{o.id}/urgent/close",
                method="post",
            ),
            cls="space-y-2 rounded-2xl bg-cream p-3",
        )
    if o.status == "open" and accepted < o.slots:
        return Div(
            A(
                "Short of people? Post an urgent call",
                href=f"/org/openings/{o.id}/urgent",
                cls=SMALL_LINK + " text-sm",
            )
        )
    return None


def OpeningRow(o, counts, urgent=None):
    status, tone = OPENING_STATUS.get(o.status, (o.status.title(), "off"))
    pending, accepted = counts.get("pending", 0), counts.get("accepted", 0)
    return Div(
        Div(
            Pill(status, tone),
            # a 44 px target around a small pill
            A(
                Span(
                    f"{pending} waiting for you",
                    cls="rounded-full bg-brand px-2.5 py-1 text-xs font-bold text-white "
                    "group-hover:bg-brand-strong",
                ),
                href="/org/inbox",
                cls="group -my-2 inline-flex min-h-11 items-center",
            )
            if pending
            else None,
            cls="flex flex-wrap items-center gap-1.5",
        ),
        H3(o.title, cls="font-bold leading-snug text-ink"),
        Meta(CALENDAR, when(o.starts_at)),
        Meta(PIN, o.neighborhood),
        Div(
            P(
                Span(f"{accepted} of {o.slots} spots filled"),
                cls="flex justify-between text-xs font-semibold text-ink-muted",
            ),
            # a <progress>: its width comes from value/max, as the CSP forbids a style
            Progress(
                value=str(min(accepted, o.slots)),
                max=str(o.slots),
                aria_label=f"{accepted} of {o.slots} spots filled",
                cls="cm-fill",
            ),
            cls="space-y-1 pt-1",
        )
        if o.slots
        else None,
        UrgentLine(o, urgent, accepted),
        A(
            "Photo and event link",
            href=f"/org/openings/{o.id}/media",
            cls=SMALL_LINK + " text-sm",
        ),
        PlainForm(
            Button("Close opening", cls=ButtonT.default),
            action=f"/org/openings/{o.id}/close",
            method="post",
            cls="pt-1",
        )
        if o.status == "open"
        else None,
        cls="w-full space-y-2 p-4 " + CARD,
    )


@ar("/org", methods=["GET"])
def org_page(req):
    return org_home(req)


def org_home(req, error=""):
    uid = req.scope["uid"]
    profile = data.get_profile(uid)
    by_org = data.org_openings(uid)
    counts = data.opening_counts(uid)
    urgent = data.org_urgent_calls(uid)
    sections = []
    for org, role in data.coordinator_orgs(uid):
        rows = [
            OpeningRow(o, counts.get(o.id, {}), urgent.get(o.id))
            for o in by_org.get(org.id, [])
        ]
        sections.append(
            Div(
                Div(
                    H2(org.name, cls=ORG_SECTION),
                    Pill(role.title(), "off"),
                    cls="flex flex-wrap items-center gap-2",
                ),
                *(rows or [P("No openings yet.", cls=TextPresets.muted_sm)]),
                cls="flex flex-col gap-3",
            )
        )
    named = bool(profile.display_name)
    return OrgShell(
        "openings",
        sum(
            c["pending"] for c in counts.values()
        ),  # data.org_waiting, without a re-read
        Div(
            H1("Your openings", cls=ORG_TITLE),
            A(
                "Post an opening",
                href="/org/openings/new",
                cls="uk-btn " + ButtonT.primary,
            )
            if named
            else None,
            cls="flex flex-wrap items-center justify-between gap-3 pb-4",
        ),
        None
        if named
        else P(
            "Add your name so volunteers know who you are.",
            cls="pb-3 font-semibold",
        ),
        None
        if named
        else Div(DetailsForm(profile, error or None), cls="max-w-md pb-8"),
        Div(
            Div(*sections, cls="flex flex-col gap-8"),
            DetailsForm(profile, error or None) if named else None,
            cls="grid items-start gap-6 lg:grid-cols-[1fr_20rem]",
        ),
    )


def Group(title, *fields):
    """One card of the opening form: a heading and its fields."""
    return Div(
        H2(title, cls=ORG_SECTION),
        *fields,
        cls="space-y-4 p-5 " + CARD,
    )


FIRST_TIMERS = "Good for first-timers: no experience needed"


def KindOfWork(f):
    """The opening's optional kind-of-work fields; volunteers who want it rank it higher."""
    return (
        Field(
            "Indoors or outdoors? (optional)",
            Choice("setting", SETTING, f.get("setting", ""), "Not said"),
        ),
        Field(
            "How physically active? (optional)",
            Choice("effort", EFFORT, f.get("effort", ""), "Not said"),
        ),
        Field(
            "Group size (optional)",
            Choice("group_size", GROUP, f.get("group_size", ""), "Not said"),
        ),
        LabelCheckboxX(
            FIRST_TIMERS,
            name="first_timer_ok",
            value="1",
            checked=bool(f.get("first_timer_ok")),
        ),
        Field(
            "Languages that help (optional)",
            Checks("languages", LANGUAGES, f.get("languages", ())),
        ),
    )


def OpeningForm(uid, f=None, error=None):
    f = f or {}
    my_orgs = data.coordinator_orgs(uid)
    org_id = f.get("org_id") or (str(my_orgs[0][0].id) if len(my_orgs) == 1 else "")
    form = Form(
        Group(
            "The opening",
            Field(
                "Organization",
                Choice("org_id", [(str(o.id), o.name) for o, _ in my_orgs], org_id),
            ),
            Field(
                'Opening title, e.g. "Saturday pantry sorting"',
                TextInput(name="title", value=f.get("title", ""), required=True),
            ),
            Field(
                "What will volunteers do?",
                Textarea(
                    f.get("about", ""),
                    name="about",
                    rows=4,
                    cls="uk-textarea",
                    required=True,
                ),
            ),
            Field(
                "Cause",
                Choice(
                    "cause",
                    [(c.slug, c.label) for c in data.active_causes()],
                    f.get("cause", ""),
                ),
            ),
        ),
        Group(
            "Where and when",
            Div(
                Field(
                    "ZIP code",
                    TextInput(
                        name="zip",
                        value=f.get("zip", ""),
                        inputmode="numeric",
                        maxlength="5",
                        required=True,
                    ),
                ),
                Field(
                    "Neighborhood",
                    TextInput(
                        name="neighborhood",
                        value=f.get("neighborhood", ""),
                        required=True,
                    ),
                ),
                cls="grid gap-4 sm:grid-cols-2",
            ),
            Field(
                "Date and time of the next shift",
                TextInput(
                    name="starts_at",
                    type="datetime-local",
                    value=f.get("starts_at", ""),
                    required=True,
                ),
            ),
            Field(
                "Does it repeat?",
                Choice("recurrence", RECURRENCE, f.get("recurrence", "")),
            ),
        ),
        Group(
            "Volunteers",
            Field(
                "How many volunteers do you need?",
                TextInput(
                    name="slots",
                    type="number",
                    min="1",
                    max="200",
                    inputmode="numeric",
                    value=f.get("slots", ""),
                    required=True,
                ),
            ),
            Field(
                "Is an orientation or training required before the first shift?",
                Choice(
                    "orientation_required",
                    ORIENTATION,
                    f.get("orientation_required", ""),
                ),
            ),
            Field(
                f"Skills that help (optional, up to {MAX_OPENING_SKILLS})",
                Checks("skills", SKILLS, f.get("skills", ())),
            ),
            Field(
                "Anything volunteers should know? Shoes, physical demands, parking. (optional)",
                Textarea(f.get("notes", ""), name="notes", rows=3, cls="uk-textarea"),
            ),
            # cm-adult-box (static/theme.css): the note is always there, read with the
            # checkbox (aria-describedby), and stands out once the cause is homelessness
            # services (:has(), or .cm-homeless on a form drawn with that cause)
            Div(
                P(
                    "Homelessness services: group shifts only, like meal service, "
                    "hygiene kits, donation sorting or events. Not case management, "
                    "mentoring, or anything that needs a background check.",
                    id="homeless-note",
                    cls="cm-homeless-note text-sm",
                ),
                LabelCheckboxX(
                    ADULT_ONLY,
                    name="adult_only_ok",
                    value="1",
                    checked=bool(f.get("adult_only_ok")),
                    aria_describedby="homeless-note",
                ),
                cls="cm-adult-box space-y-3 rounded-2xl border border-line p-4",
            ),
        ),
        Group("Kind of work", *KindOfWork(f)),
        Group(
            "Reaching you",
            Field(
                "Best way to send you interested volunteers?",
                Choice(
                    "reply_channel",
                    reply_options(bool(data.get_profile(uid).contact_phone)),
                    f.get("reply_channel", ""),
                ),
            ),
        ),
        Button("Post opening", cls=ButtonT.primary + " w-full h-12 text-lg"),
        action="/org/openings/new",
        method="post",
        # without :has() (static/theme.css), a redrawn form still shows the note
        cls="space-y-4"
        + (" cm-homeless" if f.get("cause") == "homelessness-services" else ""),
    )
    return OrgShell(
        "openings",
        data.org_waiting(uid),
        Div(
            H1("Post an opening", cls=ORG_TITLE),
            P(
                "Volunteers who fit see it after you post it. You can close it any time.",
                cls=TextPresets.muted_sm + " mt-1 mb-4",
            ),
            Alert(error, cls=AlertT.error + " mb-4") if error else None,
            form,
            cls="mx-auto max-w-2xl",
        ),
    )


@ar("/org/details", methods=["POST"])
async def save_details(req):
    form = await req.form()
    if any(not isinstance(v, str) for _, v in form.multi_items()):
        return Refusal(400)
    return await run_in_threadpool(_sync_save_details, req, form)


def _sync_save_details(req, form):
    uid = req.scope["uid"]
    name, phone = form.get("name", "").strip(), form.get("phone", "").strip()
    if not name or len(name) > 80:
        return org_home(req, "Enter your name and role (80 characters at most).")
    if hidden_chars(name):
        return org_home(req, HIDDEN_CHARS_MESSAGE)
    e164 = normalize_phone(phone) if phone else ""
    if phone and not e164:
        return org_home(req, BAD_PHONE)
    data.set_coordinator_details(uid, name, e164)
    return Redirect("/org")


@ar("/org/openings/new", methods=["GET"])
def new_opening_page(req):
    if not data.get_profile(req.scope["uid"]).display_name:
        return Redirect("/org")  # volunteers see who is asking: name first
    return OpeningForm(req.scope["uid"])


@ar("/org/openings/new", methods=["POST"])
async def new_opening_save(req):
    form = await str_form(req)
    if form is None:
        return Refusal(400)
    return await run_in_threadpool(_sync_new_opening_save, req, form)


def _sync_new_opening_save(req, form):
    uid = req.scope["uid"]
    profile = data.get_profile(uid)
    if not profile.display_name:
        return Redirect("/org")
    text = lambda k: form.get(k, "").strip()
    f = {
        **{k: text(k) for k in LIMITS},
        "skills": form.getlist("skills"),
        "org_id": text("org_id"),
        "cause": text("cause"),
        "zip": text("zip"),
        "starts_at": text("starts_at"),
        "recurrence": text("recurrence"),
        "slots": text("slots"),
        "orientation_required": text("orientation_required"),
        "reply_channel": text("reply_channel"),
        "adult_only_ok": form.get("adult_only_ok"),
        "setting": text("setting"),
        "effort": text("effort"),
        "group_size": text("group_size"),
        "first_timer_ok": form.get("first_timer_ok"),
        "languages": form.getlist("languages"),
    }

    def fail(msg):
        return OpeningForm(uid, f, msg)

    if not re.fullmatch(r"[0-9]{1,18}", f["org_id"]):
        return fail("Please choose your organization.")
    if any(len(f[k]) > n for k, n in LIMITS.items()):
        return fail("One of your answers is too long. Please shorten it.")
    if any(hidden_chars(f[k], multiline=k in ("about", "notes")) for k in LIMITS):
        return fail(HIDDEN_CHARS_MESSAGE)
    if not f["title"] or not f["about"] or not f["neighborhood"]:
        return fail(
            "Please fill in the title, what volunteers will do, and the neighborhood."
        )
    if f["cause"] not in {c.slug for c in data.active_causes()}:
        return fail("Please pick a cause from the list.")
    if not re.fullmatch(r"[0-9]{5}", f["zip"]):
        return fail("Enter a five-digit ZIP code, like 90012.")
    latlon = zip_latlon(f["zip"])
    if not latlon:
        return fail(
            "Openings must be in Los Angeles County for now. Check the ZIP code."
        )
    try:
        start = datetime.strptime(f["starts_at"], "%Y-%m-%dT%H:%M")
    except ValueError:
        return fail("Please enter the date and time of the next shift.")
    now = datetime.now(LA)
    local = start.replace(tzinfo=LA)
    if local.astimezone(UTC).astimezone(LA).replace(tzinfo=None) != start:
        return fail("That time doesn't exist (clocks skip it). Pick another time.")
    if local <= now:
        return fail("The next shift has to be in the future.")
    if local > now + MAX_AHEAD:
        return fail("The next shift must be within the next 12 months.")
    if f["recurrence"] not in {v for v, _ in RECURRENCE}:
        return fail("Please say whether the opening repeats.")
    if not re.fullmatch(r"[0-9]{1,3}", f["slots"]) or not 1 <= int(f["slots"]) <= 200:
        return fail("Volunteers needed must be a number from 1 to 200.")
    if (
        not set(f["skills"]) <= {v for v, _ in SKILLS}
        or len(set(f["skills"])) > MAX_OPENING_SKILLS
    ):
        return fail("Something in that form wasn't recognized. Please try again.")
    if f["orientation_required"] not in {v for v, _ in ORIENTATION}:
        return fail("Please say whether an orientation is required.")
    if (
        f["setting"] not in {v for v, _ in SETTING} | {""}
        or f["effort"] not in {v for v, _ in EFFORT} | {""}
        or f["group_size"] not in {v for v, _ in GROUP} | {""}
        or f["first_timer_ok"] not in (None, "1")
        or not set(f["languages"]) <= {v for v, _ in LANGUAGES}
    ):
        return fail("Something in that form wasn't recognized. Please try again.")
    if f["reply_channel"] not in {
        v for v, _ in reply_options(bool(profile.contact_phone))
    }:
        return fail(
            "Please choose how we should send you interested volunteers. Text needs a phone number in your details."
        )
    if f["adult_only_ok"] != "1":
        return fail(
            "Please confirm the opening is for adults with no one-to-one contact."
        )
    try:
        data.create_opening(
            uid,
            int(f["org_id"]),
            title=f["title"],
            about=f["about"],
            cause=f["cause"],
            lat=latlon[0],
            lon=latlon[1],
            neighborhood=f["neighborhood"],
            starts_at=start.isoformat(timespec="minutes"),
            recurrence=f["recurrence"],
            slots=int(f["slots"]),
            orientation_required=f["orientation_required"],
            skills=f["skills"],
            notes=f["notes"],
            reply_channel=f["reply_channel"],
            setting=f["setting"],
            effort=f["effort"],
            group_size=f["group_size"],
            first_timer_ok=f["first_timer_ok"] == "1",
            languages=f["languages"],
        )
    except PermissionError:
        return Refusal(403)
    return Redirect("/org")


@ar("/org/openings/{opening_id}/close", methods=["POST"])
def close(req, opening_id: int):
    try:
        data.close_opening(req.scope["uid"], opening_id)
    except PermissionError:
        return Refusal(403)
    return Redirect("/org")


URGENT_ERRORS = {
    "not open": "This opening is filled or closed, so it can't take an urgent call.",
    "already": "This opening already has an urgent call. Stop it first to post another.",
    "time": "Pick a time in the next 48 hours.",
    "limit": f"You can post {data.URGENT_PER_DAY} urgent calls a day. Try again tomorrow.",
}
AUTO_ACCEPT = (
    "Accept the first ones who say yes, up to that number, without asking me "
    "(they go straight on the schedule)"
)


def UrgentPage(uid, opening_id, f=None, error=None):
    """Post "need N more for <time within 48 hours>" on an open opening."""
    o = data.coordinator_opening(uid, opening_id)
    spots = data.open_spots(uid, opening_id)
    f = f or {}
    now = datetime.now(LA)
    soon = now + timedelta(hours=data.URGENT_HOURS)
    # the opening's own start when it is soon enough
    default = (
        o.starts_at
        if now.strftime("%Y-%m-%dT%H:%M")
        < o.starts_at
        <= soon.strftime("%Y-%m-%dT%H:%M")
        else ""
    )
    return OrgShell(
        "openings",
        data.org_waiting(uid),
        A("Back to your openings", href="/org", cls=SMALL_LINK + " mb-2 text-sm"),
        H1("Urgent call", cls=ORG_TITLE),
        P(o.title, cls="pb-4 font-semibold text-ink-muted"),
        Form(
            P(
                "Short of people in the next 48 hours? Volunteers nearby who are free "
                "and turned on last-minute calls see this opening first, and get a "
                f"text or email. You can post {data.URGENT_PER_DAY} a day.",
                cls=TextPresets.muted_sm,
            ),
            Field(
                "When do you need them? (within 48 hours)",
                TextInput(
                    name="starts_at",
                    type="datetime-local",
                    value=f.get("starts_at", default),
                    required=True,
                ),
            ),
            Field(
                f"How many more people? (up to {spots}, the open spots)",
                TextInput(
                    name="needed",
                    type="number",
                    inputmode="numeric",
                    min="1",
                    max=str(max(spots, 1)),
                    value=f.get("needed", "1"),
                    required=True,
                ),
            ),
            LabelCheckboxX(
                AUTO_ACCEPT,
                name="auto_accept",
                value="1",
                checked=f.get("auto_accept") == "1",
            ),
            Alert(error, cls=AlertT.error) if error else None,
            Button("Post urgent call", cls=ButtonT.primary),
            action=f"/org/openings/{o.id}/urgent",
            method="post",
            cls="max-w-md space-y-4 p-5 " + CARD,
        ),
    )


@ar("/org/openings/{opening_id}/urgent", methods=["GET"])
def urgent_page(req, opening_id: int):
    try:
        return UrgentPage(req.scope["uid"], opening_id)
    except PermissionError:
        return Refusal(403)


@ar("/org/openings/{opening_id}/urgent", methods=["POST"])
async def urgent_save(req, opening_id: int):
    form = await str_form(req)
    if form is None:
        return Refusal(400)
    return await run_in_threadpool(_sync_urgent_save, req, opening_id, form)


def _sync_urgent_save(req, opening_id, form):
    uid = req.scope["uid"]
    f = {
        "starts_at": form.get("starts_at", "").strip(),
        "needed": form.get("needed", "").strip(),
        "auto_accept": form.get("auto_accept"),
    }
    try:
        spots = data.open_spots(uid, opening_id)

        def fail(msg):
            return UrgentPage(uid, opening_id, f, msg)

        try:
            start = datetime.strptime(f["starts_at"], "%Y-%m-%dT%H:%M")
        except ValueError:
            return fail(URGENT_ERRORS["time"])
        local = start.replace(tzinfo=LA)
        if local.astimezone(UTC).astimezone(LA).replace(tzinfo=None) != start:
            return fail("That time doesn't exist (clocks skip it). Pick another time.")
        needs = f"How many more people: a number from 1 to {spots}."
        if not re.fullmatch(r"[0-9]{1,3}", f["needed"]):
            return fail(needs)
        if f["auto_accept"] not in (None, "1"):
            return fail("Something in that form wasn't recognized. Please try again.")
        try:
            data.post_urgent_call(
                uid,
                opening_id,
                start.isoformat(timespec="minutes"),
                int(f["needed"]),
                f["auto_accept"] == "1",
            )
        except ValueError as e:
            return fail(URGENT_ERRORS.get(str(e), needs))
    except PermissionError:
        return Refusal(403)
    return Redirect("/org")


@ar("/org/openings/{opening_id}/urgent/close", methods=["POST"])
def urgent_close(req, opening_id: int):
    try:
        data.close_urgent_call(req.scope["uid"], opening_id)
    except PermissionError:
        return Refusal(403)
    return Redirect("/org")


def label_of(options, value):
    return dict(options).get(value, "")


INBOX_STATUS = {
    "pending": "Waiting",
    "accepted": "Accepted",
    "declined": "Declined",
    "withdrawn": "Withdrawn",
}
INBOX_NOTICE = {
    "decided": "Someone already decided that one.",
    "full": "That opening is full. You can decline instead, or post another opening.",
    "reported": "Thanks. A Causematch admin will review the report.",
    "blocked": "Blocked.",
}


INBOX_TONE = {
    "pending": "wait",
    "accepted": "good",
    "declined": "off",
    "withdrawn": "off",
}


def Fact(term, value):
    return P(Span(f"{term}: ", cls="font-bold"), value, cls="text-sm text-ink")


def InboxRow(m):
    accept = (
        Pill("Full", "off")
        if m["full"]
        else Button("Accept", name="decision", value="accept", cls=ButtonT.primary)
    )
    return Div(
        Div(
            Div(
                VolunteerHeader(m["name"], m["photo_key"], m["pronouns"]),
                P(f"Interested in: {m['opening']}", cls=TextPresets.muted_sm + " mt-1"),
                cls="min-w-0 flex-1",
            ),
            Pill(INBOX_STATUS[m["status"]], INBOX_TONE[m["status"]]),
            cls="flex items-start gap-3",
        ),
        P(m["bio"], cls="whitespace-pre-line text-sm text-ink") if m["bio"] else None,
        Div(
            Fact("Interests", ", ".join(m["interests"])) if m["interests"] else None,
            Fact("Causes", ", ".join(m["causes"]) or "none given"),
            Fact(
                "Available",
                ", ".join(label_of(AVAILABILITY, a) for a in m["availability"])
                or "any",
            ),
            Fact("Speaks", ", ".join(m["languages"])) if m["languages"] else None,
            Fact("Neighborhood", m["neighborhood"] or "not given"),
            Fact("Wants to help", label_of(FREQUENCY, m["frequency"]) or "not given"),
            cls="space-y-1 rounded-2xl bg-cream p-3",
        ),
        PlainForm(
            accept,
            Button("Decline", name="decision", value="decline", cls=ButtonT.default),
            action=f"/org/matches/{m['id']}/decide",
            method="post",
            cls="flex flex-wrap items-center gap-3",
        )
        if m["status"] == "pending"
        else None,
        RepeatForm(m["id"]) if m["status"] == "accepted" and m["weekly"] else None,
        # links in a flex row with a gap: never run together ("shiftReport")
        Div(
            A("Chat", href=f"/org/chat/{m['id']}", cls=SMALL_LINK)
            if m["status"] in ("accepted", "withdrawn")
            else None,
            A(
                "Schedule another shift",
                href=f"/org/matches/{m['id']}/shifts/new",
                cls=SMALL_LINK,
            )
            if m["status"] == "accepted"
            else None,
            A("Report", href=f"/org/matches/{m['id']}/report", cls=SMALL_LINK),
            A("Block", href=f"/org/matches/{m['id']}/block", cls=SMALL_LINK),
            cls="flex flex-wrap gap-x-5 border-t border-line pt-1 text-sm",
        ),
        cls="w-full space-y-3 p-4 " + CARD,
    )


def InboxSection(title, rows, empty=None):
    if not rows and empty is None:
        return None
    return Div(
        H2(title, cls=ORG_SECTION),
        *(rows or [P(empty, cls=TextPresets.muted_sm)]),
        cls="flex flex-col gap-3",
    )


@ar("/org/inbox", methods=["GET"])
def inbox(req):
    notice = query(req, "notice")
    uid = req.scope["uid"]
    box = data.org_inbox(uid)
    return OrgShell(
        "inbox",
        data.org_waiting(uid),
        H1("Volunteer inbox", cls=ORG_TITLE + " pb-4"),
        Alert(INBOX_NOTICE[notice], cls=AlertT.warning + " mb-4")
        if notice in INBOX_NOTICE
        else None,
        Div(
            InboxSection(
                "Waiting for you",
                [InboxRow(m) for m in box["pending"]],
                "No one is waiting.",
            ),
            Div(
                InboxSection(
                    "Decided", [InboxRow(m) for m in box["decided"]], "Nothing yet."
                ),
                InboxSection("Past matches", [InboxRow(m) for m in box["past"]]),
                cls="flex flex-col gap-8",
            ),
            cls="grid items-start gap-8 lg:grid-cols-2",
        ),
    )


@ar("/org/matches/{match_id}/decide", methods=["POST"])
async def decide(req, match_id: int):
    form = await str_form(req)
    decision = form.get("decision") if form else None
    if decision not in ("accept", "decline"):
        return Refusal(400)
    return await run_in_threadpool(_sync_decide, req, form, match_id)


def _sync_decide(req, form, match_id: int):
    decision = form.get("decision") if form else None
    try:
        data.decide(req.scope["uid"], match_id, decision == "accept")
    except PermissionError:
        return Refusal(403)
    except ValueError as e:
        return Redirect(
            f"/org/inbox?notice={'full' if 'slots' in str(e) else 'decided'}"
        )
    if decision == "accept":
        notify.on_accepted(match_id)
    return Redirect("/org/inbox")
