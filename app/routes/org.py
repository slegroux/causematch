import json
import re
from datetime import UTC, datetime, timedelta

from fasthtml.common import (
    H1,
    H2,
    H3,
    A,
    Details,
    Div,
    Input,
    P,
    Progress,
    Redirect,
    Script,
    Span,
    Summary,
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
from starlette.responses import JSONResponse

from app import data, notify, prefs
from app.auth import normalize_phone
from app.choices import (
    AVAILABILITY,
    EFFORT,
    FREQUENCY,
    GROUP,
    LANGUAGES,
    MAX_OPENING_SKILLS,
    SETTING,
    SHIFT_LENGTHS,
    SKILLS,
)
from app.forms import HIDDEN_CHARS_MESSAGE, hidden_chars, query, str_form
from app.geo import zip_latlon, zip_neighborhood
from app.match import LA
from app.routes.shifts import RepeatForm
from app.ui import (
    CALENDAR,
    CARD,
    MATCH_STATUS,
    NEXT,
    ORG_SECTION,
    ORG_TITLE,
    PIN,
    SMALL_LINK,
    Checks,
    Choice,
    Field,
    Icon,
    Meta,
    OrgShell,
    Pill,
    Refusal,
    Router,
    TextInput,
    VolunteerHeader,
    status_pill,
    when,
)

ar = Router()

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


def DetailsForm(profile, error=None, back="/org"):
    return Form(
        Input(type="hidden", name="back", value=back),
        H2("Your details", cls=ORG_SECTION),
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


OPENING_STATUS = {
    "open": ("Open", "good"),
    "filled": ("Filled", "info"),
    "closed": ("Closed", "off"),
}


def urgent_possible(starts_at):
    """Whether a shift starting at this local time is within the urgent window."""
    now = datetime.now(LA)
    soon = now + timedelta(hours=data.URGENT_HOURS)
    return now.strftime("%Y-%m-%dT%H:%M") < starts_at <= soon.strftime("%Y-%m-%dT%H:%M")


def UrgentLine(o, urgent, accepted):
    """The opening's open urgent call and a way to stop it."""
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
                Button("Stop last-minute call", cls=ButtonT.default),
                action=f"/org/openings/{o.id}/urgent/close",
                method="post",
            ),
            cls="space-y-2 rounded-2xl bg-cream p-3",
        )
    return None


def UrgentLink(o, urgent, accepted):
    """A link to post an urgent call, while one could still fill the opening."""
    if (
        not urgent
        and o.status == "open"
        and accepted < o.slots
        and urgent_possible(o.starts_at)
    ):
        return A(
            "Short of people? Post a last-minute call",
            href=f"/org/openings/{o.id}/urgent",
            cls=SMALL_LINK + " text-sm",
        )
    return None


def messages_to_answer(n):
    """The dashboard link and the inbox heading for the chats owed a reply."""
    return "1 message to answer" if n == 1 else f"{n} messages to answer"


def reopenable(o):
    """A closed opening whose start is still ahead can take volunteers again."""
    return (
        o.status == "closed"
        and datetime.now(LA).strftime("%Y-%m-%dT%H:%M") < o.starts_at
    )


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
        Meta(CALENDAR, when(o.starts_at, o.duration_minutes)),
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
        Div(
            A(
                "Edit",
                href=f"/org/openings/{o.id}/edit",
                cls="uk-btn " + ButtonT.default,
            )
            if o.status in ("open", "filled")
            else None,
            PlainForm(
                Button("Reopen", cls=ButtonT.default),
                action=f"/org/openings/{o.id}/reopen",
                method="post",
            )
            if reopenable(o)
            else None,
            cls="flex flex-wrap items-center gap-2 pt-1",
        ),
        Details(  # the rarely wanted actions, behind one tap (no script)
            Summary(
                "More",
                Icon(NEXT, size=16, cls="rotate-90"),
                cls="inline-flex min-h-11 cursor-pointer list-none items-center gap-1 "
                "text-sm font-semibold text-ink-muted underline underline-offset-4 "
                "hover:text-ink [&::-webkit-details-marker]:hidden",
            ),
            Div(
                A(
                    "Photo and event link",
                    href=f"/org/openings/{o.id}/media",
                    cls=SMALL_LINK + " text-sm",
                ),
                UrgentLink(o, urgent, accepted),
                PlainForm(
                    Button("Close opening", cls=ButtonT.default),
                    action=f"/org/openings/{o.id}/close",
                    method="post",
                )
                if o.status == "open"
                else None,
                cls="flex flex-col items-start gap-1.5",
            ),
        ),
        cls="w-full space-y-2 p-4 " + CARD,
    )


def NeedsYou(waiting, to_mark):
    """The strip atop the dashboard: what waits for the coordinator, each part a link to
    where it is dealt with; each part hidden at 0, the strip when both are."""
    if not (waiting or to_mark):
        return None
    link = (
        "inline-flex min-h-11 items-center gap-2 font-bold text-brand-ink underline "
        "underline-offset-4 hover:text-brand"
    )
    return Div(
        P("Needs you", cls="text-sm font-bold text-ink-muted"),
        Div(
            A(
                f"{waiting} volunteer{'' if waiting == 1 else 's'} waiting",
                Span("→", aria_hidden="true"),
                Span(" Inbox", cls="sr-only"),
                href="/org/inbox",
                cls=link,
            )
            if waiting
            else None,
            A(
                f"{to_mark} shift{'' if to_mark == 1 else 's'} to mark",
                Span("→", aria_hidden="true"),
                Span(" Shifts", cls="sr-only"),
                href="/org/shifts",
                cls=link,
            )
            if to_mark
            else None,
            cls="flex flex-wrap items-center gap-x-6",
        ),
        cls="mb-4 rounded-2xl bg-cream px-4 py-2",
    )


def waiting_first(openings, counts):
    """An org's openings in dashboard order: the open ones with volunteers waiting, then
    the other open ones, each soonest start first; then the closed, newest first (as
    data.org_openings gives them)."""
    live = [o for o in openings if o.status == "open"]
    live.sort(key=lambda o: (not counts.get(o.id, {}).get("pending"), o.starts_at))
    return live + [o for o in openings if o.status != "open"]


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
            for o in waiting_first(by_org.get(org.id, []), counts)
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
    unanswered = len(data.org_unanswered(uid))
    pending = sum(c["pending"] for c in counts.values())
    to_mark = data.org_to_mark(uid)
    return OrgShell(
        "openings",
        # data.org_badges, without re-reading the counts
        {"inbox": pending + unanswered, "shifts": to_mark},
        NeedsYou(pending, to_mark) if named else None,
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
        A(
            messages_to_answer(unanswered),
            href="/org/inbox",
            cls="mb-4 inline-flex min-h-11 items-center rounded-full bg-brand px-4 "
            "text-sm font-bold text-white hover:bg-brand-strong",
        )
        if unanswered
        else None,
        Alert(error, cls=AlertT.error + " mb-4") if error and named else None,
        None
        if named
        else P(
            "Add your name so volunteers know who you are.",
            cls="pb-3 font-semibold",
        ),
        None
        if named
        else Div(DetailsForm(profile, error or None), cls="max-w-md pb-8"),
        Div(*sections, cls="flex flex-col gap-8"),
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


def Locked(label, value, why):
    """A fact of the opening that the edit form shows but doesn't change, and why."""
    return Div(
        P(label, cls="text-sm font-bold text-ink"),
        P(value, cls="text-ink"),
        P(why, cls=TextPresets.muted_sm),
        cls="space-y-1",
    )


def opening_fields(o):
    """The post form's fields as a posted opening has them, to prefill the edit form."""
    causes = json.loads(o.causes or "[]")
    return {
        "org_id": str(o.org_id),
        "title": o.title,
        "about": o.about,
        "cause": causes[0] if len(causes) == 1 else "",
        "neighborhood": o.neighborhood or "",
        "starts_at": o.starts_at,
        "duration_minutes": str(o.duration_minutes or ""),
        "recurrence": o.recurrence or "",
        "slots": str(o.slots),
        "orientation_required": o.orientation_required or "",
        "notes": o.notes or "",
        "reply_channel": o.reply_channel or "",
        "adult_only_ok": "1",
        "setting": o.setting or "",
        "effort": o.effort or "",
        "group_size": o.group_size or "",
        "first_timer_ok": o.first_timer_ok,
        "languages": json.loads(o.languages or "[]"),
        "skills": json.loads(o.skills or "[]"),
    }


def OpeningForm(uid, f=None, error=None, editing=None):
    """The post form, or with editing (an opening) the same form prefilled, which saves
    to that opening. There the org, the place and the repeat are shown, not asked; the
    date, time and cause only while data.opening_locks lets them change."""
    f = f or {}
    locks = data.opening_locks(editing.id) if editing else {}
    cause_fixed = bool(editing) and (
        locks["cause"] or len(json.loads(editing.causes or "[]")) != 1
    )
    my_orgs = data.coordinator_orgs(uid)
    org_id = f.get("org_id") or (str(my_orgs[0][0].id) if len(my_orgs) == 1 else "")
    reply_choices = reply_options(bool(data.get_profile(uid).contact_phone))
    reply_channel = (
        f.get("reply_channel")
        if f.get("reply_channel") in dict(reply_choices)
        else (reply_choices[0][0] if len(reply_choices) == 1 else "")
    )
    reply_field = (
        Input(type="hidden", name="reply_channel", value=reply_channel)
        if len(reply_choices) == 1
        else Group(
            "Reaching you",
            Field(
                "Best way to send you interested volunteers?",
                Choice("reply_channel", reply_choices, reply_channel),
            ),
        )
    )
    form = Form(
        Group(
            "The opening",
            Input(type="hidden", name="org_id", value=org_id)
            if len(my_orgs) == 1 or editing
            else Field(
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
            Locked(
                "Cause",
                ", ".join(data.cause_labels(json.loads(editing.causes or "[]"))),
                "It can't change once volunteers have liked this opening, because "
                "they were matched on it."
                if locks["cause"]
                else "This opening has more than one cause, which this form can't edit.",
            )
            if cause_fixed
            else Field(
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
            Locked(
                "Where",
                editing.neighborhood or "Los Angeles County",
                "The place can't change once posted. Post a new opening for another place.",
            )
            if editing
            else Div(
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
                        placeholder="Filled in from the ZIP code; you can change it",
                    ),
                ),
                cls="grid gap-4 sm:grid-cols-2",
            ),
            Locked(
                "Date and time",
                when(editing.starts_at, editing.duration_minutes),
                "It can't change once volunteers have liked this opening, because they "
                "chose it for that time. Close it and post a new one to move it.",
            )
            if editing and locks["when"]
            else Field(
                "Date and time of the next shift",
                TextInput(
                    name="starts_at",
                    type="datetime-local",
                    value=f.get("starts_at", ""),
                    required=True,
                ),
            ),
            None
            if editing and locks["when"]
            else Field(
                "How long is a shift?",
                Choice(
                    "duration_minutes",
                    SHIFT_LENGTHS,
                    f.get("duration_minutes", ""),
                    required=True,
                ),
            ),
            Locked(
                "Repeats",
                label_of(RECURRENCE, editing.recurrence) or "Not said",
                "It can't change once posted.",
            )
            if editing
            else Field(
                "Does it repeat?",
                # no preset: it decides who the opening fits (match.commitment_fits)
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
                    min=str(max(1, locks.get("accepted", 0))),
                    max="200",
                    inputmode="numeric",
                    value=f.get("slots", ""),
                    required=True,
                ),
                P(
                    f"{locks['accepted']} accepted so far, so it can't go lower.",
                    cls=TextPresets.muted_sm,
                )
                if locks.get("accepted")
                else None,
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
        reply_field,
        Details(
            Summary(
                "More details (optional)",
                cls="cursor-pointer py-3 font-bold text-ink",  # a 44 px tap target
            ),
            Div(
                H2("Kind of work", cls=ORG_SECTION),
                *KindOfWork(f),
                Field(
                    f"Skills that help (optional, up to {MAX_OPENING_SKILLS})",
                    Checks("skills", SKILLS, f.get("skills", ())),
                ),
                cls="mt-4 space-y-4",
            ),
            open=bool(
                any(
                    f.get(k)
                    for k in ("setting", "effort", "group_size", "first_timer_ok")
                )
                or f.get("languages")
                or f.get("skills")
            ),
            cls="p-5 " + CARD,
        ),
        Button(
            "Save changes" if editing else "Post opening",
            cls=ButtonT.primary + " w-full h-12 text-lg",
        ),
        A("Cancel", href="/org", cls=SMALL_LINK + " block text-center"),
        action=f"/org/openings/{editing.id}/edit" if editing else "/org/openings/new",
        method="post",
        # without :has() (static/theme.css), a redrawn form still shows the note
        cls="space-y-4"
        + (" cm-homeless" if f.get("cause") == "homelessness-services" else ""),
    )
    return OrgShell(
        "openings",
        data.org_badges(uid),
        Div(
            H1("Edit opening" if editing else "Post an opening", cls=ORG_TITLE),
            P(
                "Volunteers already accepted are told in their chat what you change."
                if editing
                else "Volunteers who fit see it after you post it. You can close it any time.",
                cls=TextPresets.muted_sm + " mt-1 mb-4",
            ),
            Alert(error, cls=AlertT.error + " mb-4") if error else None,
            form,
            None
            if editing
            else Script(
                src="/static/zip.js", defer=True
            ),  # the neighborhood, from the ZIP
            cls="mx-auto max-w-2xl",
        ),
    )


@ar("/org/details", methods=["POST"])
async def save_details(req):
    form = await str_form(req)
    if form is None:
        return Refusal(400)
    return await run_in_threadpool(_sync_save_details, req, form)


def _sync_save_details(req, form):
    uid = req.scope["uid"]
    back = form.get("back")
    back = back if back in ("/org", "/org/organization") else "/org"
    name, phone = form.get("name", "").strip(), form.get("phone", "").strip()
    error = None
    if not name or len(name) > 80:
        error = "Enter your name and role (80 characters at most)."
    elif hidden_chars(name):
        error = HIDDEN_CHARS_MESSAGE
    elif phone and not normalize_phone(phone):
        error = BAD_PHONE
    if error:
        if back == "/org":
            return org_home(req, error)
        # here, not at the top: org_profile imports this module
        from app.routes.org_profile import OrganizationPage

        return OrganizationPage(uid, details_error=error)
    data.set_coordinator_details(uid, name, normalize_phone(phone) if phone else "")
    return Redirect(back)


@ar("/org/zip", methods=["GET"])
def zip_neighborhood_json(req):
    """{"neighborhood": ...} for the opening form's ZIP (static/zip.js), "" when unknown:
    a lookup in a local table, nothing stored."""
    zip_code = query(req, "zip")
    hood = zip_neighborhood(zip_code) if re.fullmatch(r"[0-9]{5}", zip_code) else None
    return JSONResponse({"neighborhood": hood or ""})


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


def opening_input(form):
    """The post form's answers, trimmed, by field name."""
    text = lambda k: form.get(k, "").strip()
    return {
        **{k: text(k) for k in LIMITS},
        "skills": form.getlist("skills"),
        "org_id": text("org_id"),
        "cause": text("cause"),
        "zip": text("zip"),
        "starts_at": text("starts_at"),
        "duration_minutes": text("duration_minutes"),
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


def opening_error(f, profile, skip=()):
    """(the first thing wrong with the post form's answers as a message, else None; the
    ZIP's (lat, lon); the start as a datetime). skip: parts an edit leaves alone, of
    "cause", "where" (ZIP), "when" (start and length), "repeat", "orientation" and "reply", whose
    answers aren't checked (an edit keeps what the opening has, even a seed's blank)."""
    if not re.fullmatch(r"[0-9]{1,18}", f["org_id"]):
        return "Please choose your organization.", None, None
    if any(len(f[k]) > n for k, n in LIMITS.items()):
        return "One of your answers is too long. Please shorten it.", None, None
    if any(hidden_chars(f[k], multiline=k in ("about", "notes")) for k in LIMITS):
        return HIDDEN_CHARS_MESSAGE, None, None
    if not f["title"] or not f["about"]:
        return "Please fill in the title and what volunteers will do.", None, None
    if "cause" not in skip and f["cause"] not in {c.slug for c in data.active_causes()}:
        return "Please pick a cause from the list.", None, None
    latlon = start = None
    if "where" not in skip:
        if not re.fullmatch(r"[0-9]{5}", f["zip"]):
            return "Enter a five-digit ZIP code, like 90012.", None, None
        latlon = zip_latlon(f["zip"])
        if not latlon:
            return (
                "Openings must be in Los Angeles County for now. Check the ZIP code.",
                None,
                None,
            )
        f["neighborhood"] = f["neighborhood"] or zip_neighborhood(f["zip"])
    if "when" not in skip:
        try:
            start = datetime.strptime(f["starts_at"], "%Y-%m-%dT%H:%M")
        except ValueError:
            return "Please enter the date and time of the next shift.", None, None
        now = datetime.now(LA)
        local = start.replace(tzinfo=LA)
        if local.astimezone(UTC).astimezone(LA).replace(tzinfo=None) != start:
            return (
                "That time doesn't exist (clocks skip it). Pick another time.",
                None,
                None,
            )
        if local <= now:
            return "The next shift has to be in the future.", None, None
        if local > now + MAX_AHEAD:
            return "The next shift must be within the next 12 months.", None, None
        if f["duration_minutes"] not in {v for v, _ in SHIFT_LENGTHS}:
            return "Please say how long a shift is.", None, None
    if "repeat" not in skip and f["recurrence"] not in {v for v, _ in RECURRENCE}:
        return "Please say whether the opening repeats.", None, None
    if not re.fullmatch(r"[0-9]{1,3}", f["slots"]) or not 1 <= int(f["slots"]) <= 200:
        return "Volunteers needed must be a number from 1 to 200.", None, None
    if (
        not set(f["skills"]) <= {v for v, _ in SKILLS}
        or len(set(f["skills"])) > MAX_OPENING_SKILLS
    ):
        return prefs.UNRECOGNIZED, None, None
    if "orientation" not in skip and f["orientation_required"] not in {
        v for v, _ in ORIENTATION
    }:
        return "Please say whether an orientation is required.", None, None
    if (
        f["setting"] not in {v for v, _ in SETTING} | {""}
        or f["effort"] not in {v for v, _ in EFFORT} | {""}
        or f["group_size"] not in {v for v, _ in GROUP} | {""}
        or f["first_timer_ok"] not in (None, "1")
        or not set(f["languages"]) <= {v for v, _ in LANGUAGES}
    ):
        return prefs.UNRECOGNIZED, None, None
    if "reply" not in skip and f["reply_channel"] not in {
        v for v, _ in reply_options(bool(profile.contact_phone))
    }:
        return (
            "Please choose how we should send you interested volunteers. Text needs a phone number in your details.",
            None,
            None,
        )
    if f["adult_only_ok"] != "1":
        return (
            "Please confirm the opening is for adults with no one-to-one contact.",
            None,
            None,
        )
    return None, latlon, start


def _sync_new_opening_save(req, form):
    uid = req.scope["uid"]
    profile = data.get_profile(uid)
    if not profile.display_name:
        return Redirect("/org")
    f = opening_input(form)
    error, latlon, start = opening_error(f, profile)
    if error:
        return OpeningForm(uid, f, error)
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
            duration_minutes=int(f["duration_minutes"]),
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


@ar("/org/openings/{opening_id}/edit", methods=["GET"])
def edit_opening_page(req, opening_id: int):
    uid = req.scope["uid"]
    try:
        o = data.coordinator_opening(uid, opening_id)
    except PermissionError:
        return Refusal(403)
    if o.status == "closed":  # reopen it first
        return Redirect("/org")
    return OpeningForm(uid, opening_fields(o), editing=o)


@ar("/org/openings/{opening_id}/edit", methods=["POST"])
async def edit_opening_save(req, opening_id: int):
    form = await str_form(req)
    if form is None:
        return Refusal(400)
    return await run_in_threadpool(_sync_edit_opening_save, req, opening_id, form)


def _sync_edit_opening_save(req, opening_id, form):
    uid = req.scope["uid"]
    try:
        o = data.coordinator_opening(uid, opening_id)
        locks = data.opening_locks(o.id)
        fixed = opening_fields(o)
        f = opening_input(form)
        # what the form doesn't ask or may not change is the opening's own, unchecked
        skip = {"where", "repeat"}
        if f["orientation_required"] == fixed["orientation_required"]:
            skip.add("orientation")
        if f["reply_channel"] == fixed["reply_channel"]:
            skip.add("reply")
        f["org_id"], f["recurrence"] = fixed["org_id"], fixed["recurrence"]
        if locks["cause"] or not fixed["cause"]:
            skip.add("cause")
        if locks["when"]:
            f["starts_at"], f["duration_minutes"] = (
                fixed["starts_at"],
                fixed["duration_minutes"],
            )
        if (f["starts_at"], f["duration_minutes"]) == (
            fixed["starts_at"],
            fixed["duration_minutes"],
        ):
            skip.add("when")  # unchanged: it may even have started by now
        error, _, start = opening_error(f, data.get_profile(uid), skip)
        if error:
            return OpeningForm(uid, {**fixed, **f}, error, editing=o)
        changes = {
            "title": f["title"],
            "about": f["about"],
            "notes": f["notes"],
            "slots": int(f["slots"]),
            "orientation_required": f["orientation_required"],
            "reply_channel": f["reply_channel"],
            "skills": f["skills"],
            "setting": f["setting"],
            "effort": f["effort"],
            "group_size": f["group_size"],
            "first_timer_ok": f["first_timer_ok"] == "1",
            "languages": f["languages"],
        }
        if "cause" not in skip:
            changes["cause"] = f["cause"]
        if "when" not in skip:
            changes["starts_at"] = start.isoformat(timespec="minutes")
            changes["duration_minutes"] = int(f["duration_minutes"])
        try:
            data.update_opening(uid, o.id, **changes)
        except ValueError as e:
            return OpeningForm(uid, {**fixed, **f}, str(e), editing=o)
    except PermissionError:
        return Refusal(403)
    return Redirect("/org")


@ar("/org/openings/{opening_id}/reopen", methods=["POST"])
def reopen(req, opening_id: int):
    try:
        data.reopen_opening(req.scope["uid"], opening_id)
    except PermissionError:
        return Refusal(403)
    except ValueError:  # not closed, or started: the dashboard shows it as it is
        pass
    return Redirect("/org")


@ar("/org/openings/{opening_id}/close", methods=["POST"])
def close(req, opening_id: int):
    try:
        data.close_opening(req.scope["uid"], opening_id)
    except PermissionError:
        return Refusal(403)
    return Redirect("/org")


URGENT_ERRORS = {
    "not open": "This opening is filled or closed, so it can't take a last-minute call.",
    "already": "This opening already has a last-minute call. Stop it first to post another.",
    "time": "Pick a time in the next 48 hours.",
    "limit": f"You can post {data.URGENT_PER_DAY} last-minute calls a day. Try again tomorrow.",
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
    # the opening's own start when it is soon enough
    default = o.starts_at if urgent_possible(o.starts_at) else ""
    return OrgShell(
        "openings",
        data.org_badges(uid),
        A("Back to your openings", href="/org", cls=SMALL_LINK + " mb-2 text-sm"),
        H1("Last-minute call", cls=ORG_TITLE),
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
            Button("Post last-minute call", cls=ButtonT.primary),
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
            return fail(prefs.UNRECOGNIZED)
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


INBOX_NOTICE = {
    "decided": "Someone already decided that one.",
    "full": "That opening is full. You can decline instead, or post another opening.",
    "reported": "Thanks. A Causematch admin will review the report.",
    "blocked": "Blocked.",
    "late": "Too late to undo that one.",
}


def Fact(term, value):
    return P(Span(f"{term}: ", cls="font-bold"), value, cls="text-sm text-ink")


def ShiftFacts(m):
    """What the like is for: the shift's date and time, the spots left, and whether it
    falls in the volunteer's usual times (nothing under a block)."""
    return Div(
        P(
            when(m["starts_at"], m["duration_minutes"]),
            f" · {m['spots_left']} of {m['slots']} spots left",
            cls="text-sm font-semibold text-ink",
        ),
        P(
            "Fits their usual times" if m["fits"] else "Outside their usual times",
            cls="text-sm text-ink-muted",
        )
        if m["fits"] is not None
        else None,
        cls="mt-1",
    )


def Declined(m):
    """The note after a Decline, until the undo window closes: nothing was sent yet."""
    return Div(
        P(
            f"Declined. {m['name']} hasn't been told yet.",
            cls="text-sm font-semibold text-ink",
        ),
        PlainForm(
            Button(
                "Undo",
                cls="min-h-11 px-2 text-sm font-semibold text-brand-ink underline "
                "underline-offset-4 hover:text-brand",
            ),
            action=f"/org/matches/{m['id']}/undo",
            method="post",
        ),
        cls="flex flex-wrap items-center justify-between gap-x-3 p-4 " + CARD,
    )


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
                ShiftFacts(m),
                cls="min-w-0 flex-1",
            ),
            status_pill(MATCH_STATUS, m["status"], "org"),
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
            # plain text, apart from Accept: a mis-tap costs one Undo, not a lost volunteer
            Button(
                "Decline",
                name="decision",
                value="decline",
                cls="ml-auto min-h-11 px-3 text-sm font-semibold text-ink-muted "
                "underline underline-offset-4 hover:text-ink",
            ),
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


def UnansweredRow(c):
    """A chat whose latest message is the volunteer's: who, for what, what they said."""
    return A(
        VolunteerHeader(c["name"], c["photo_key"]),
        P(f"About: {c['opening']}", cls=TextPresets.muted_sm),
        P(c["body"], cls="line-clamp-2 whitespace-pre-line text-ink"),
        Span("Reply", cls="text-sm font-bold text-brand underline"),
        href=f"/org/chat/{c['id']}",
        cls="block space-y-2 p-4 hover:border-brand " + CARD,
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
    chat_id = query(req, "match")
    chat_id = chat_id if chat_id.isascii() and chat_id.isdigit() else ""
    declined = query(req, "declined")
    uid = req.scope["uid"]
    box = data.org_inbox(uid)
    # the decline just made, while the volunteer has still been told nothing of it
    undo = [m for m in box["decided"] if m["undoable"] and str(m["id"]) == declined]
    unanswered = data.org_unanswered(uid)
    return OrgShell(
        "inbox",
        data.org_badges(uid),
        H1("Volunteer inbox", cls=ORG_TITLE + " pb-4"),
        Alert(INBOX_NOTICE[notice], cls=AlertT.warning + " mb-4")
        if notice in INBOX_NOTICE
        else None,
        Alert(
            "Accepted · ",
            A("Say hi", href=f"/org/chat/{chat_id}", cls=SMALL_LINK),
            cls=AlertT.success + " mb-4",
        )
        if notice == "accepted" and chat_id
        else None,
        Div(
            InboxSection(
                messages_to_answer(len(unanswered)),
                [UnansweredRow(c) for c in unanswered],
            ),
            cls="pb-8 lg:max-w-[calc(50%-1rem)]",
        )
        if unanswered
        else None,
        Div(
            InboxSection(
                "Waiting for you",
                [*map(Declined, undo), *map(InboxRow, box["pending"])],
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
    return await run_in_threadpool(_sync_decide, req, decision, match_id)


def _sync_decide(req, decision, match_id: int):
    try:
        data.decide(req.scope["uid"], match_id, decision == "accept", undoable=True)
    except PermissionError:
        return Refusal(403)
    except ValueError as e:
        return Redirect(
            f"/org/inbox?notice={'full' if 'slots' in str(e) else 'decided'}"
        )
    if decision == "accept":
        notify.after(req, notify.on_accepted, match_id)
        return Redirect(f"/org/inbox?notice=accepted&match={match_id}")
    return Redirect(f"/org/inbox?declined={match_id}")


@ar("/org/matches/{match_id}/undo", methods=["POST"])
async def undo_decline(req, match_id: int):
    return await run_in_threadpool(_sync_undo, req, match_id)


def _sync_undo(req, match_id: int):
    try:
        data.undo_decline(req.scope["uid"], match_id)
    except PermissionError:
        return Refusal(403)
    except ValueError:
        return Redirect("/org/inbox?notice=late")
    return Redirect("/org/inbox")
