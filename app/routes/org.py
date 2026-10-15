import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fasthtml.common import (
    H1,
    H2,
    A,
    APIRouter,
    Div,
    P,
    Redirect,
    Response,
    Textarea,
)
from fasthtml.common import Form as PlainForm
from monsterui.all import (
    AT,
    Alert,
    AlertT,
    Button,
    ButtonT,
    Card,
    Container,
    DivFullySpaced,
    Form,
    Label,
    LabelCheckboxX,
    TextPresets,
)
from starlette.concurrency import run_in_threadpool

from app import data, notify
from app.auth import normalize_phone
from app.choices import AVAILABILITY, FREQUENCY, MAX_OPENING_SKILLS, SKILLS
from app.forms import bad_request, query, str_form
from app.geo import city_zip_latlon
from app.routes.shifts import RepeatForm
from app.ui import Checks, Choice, Field, Page, TextInput, body_wrap

ar = APIRouter(body_wrap=body_wrap)  # the demo banner, when it is on

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
        cls="space-y-3 max-w-md pb-8",
    )


LIMITS = {"title": 100, "about": 1000, "neighborhood": 80, "notes": 500}
ADULT_ONLY = (
    "I confirm this opening is for adults (18+) and involves no one-to-one contact "
    "with vulnerable people"
)


def when(starts_at):
    return datetime.fromisoformat(starts_at).strftime("%a %b %-d, %-I:%M %p")


def OpeningRow(o):
    return Card(
        DivFullySpaced(P(o.title, cls="font-semibold"), Label(o.status)),
        P(
            f"{when(o.starts_at)} - {o.neighborhood} - {o.slots} spots",
            cls=TextPresets.muted_sm,
        ),
        PlainForm(
            Button("Close opening", cls=ButtonT.default),
            action=f"/org/openings/{o.id}/close",
            method="post",
        )
        if o.status == "open"
        else None,
        cls="w-full",
    )


@ar("/org", methods=["GET"])
def org_page(req):
    return org_home(req)


def org_home(req, error=""):
    uid = req.scope["uid"]
    profile = data.get_profile(uid)
    by_org = data.org_openings(uid)
    sections = []
    for org, role in data.coordinator_orgs(uid):
        rows = [OpeningRow(o) for o in by_org.get(org.id, [])]
        sections.append(
            Div(
                H2(org.name, cls="text-xl font-semibold"),
                P(role, cls=TextPresets.muted_sm),
                *(rows or [P("No openings yet.", cls=TextPresets.muted_sm)]),
                cls="flex flex-col gap-3",
            )
        )
    return Container(
        DivFullySpaced(
            P("Your openings", cls="text-2xl font-bold my-4"),
            PlainForm(
                Button("Sign out", cls=ButtonT.ghost), action="/logout", method="post"
            ),
        ),
        None
        if profile.display_name
        else P("Add your name so volunteers know who you are.", cls="font-semibold"),
        None if profile.display_name else DetailsForm(profile, error or None),
        Div(*sections, cls="flex flex-col gap-8 pb-4"),
        Div(
            A(
                "Post an opening",
                href="/org/openings/new",
                cls="uk-btn " + ButtonT.primary,
            ),
            A("Volunteer inbox", href="/org/inbox", cls="uk-btn " + ButtonT.default),
            A("Shifts", href="/org/shifts", cls="uk-btn " + ButtonT.default),
            cls="pb-8 flex gap-3",
        ),
        H2("Your details", cls="text-xl font-semibold")
        if profile.display_name
        else None,
        DetailsForm(profile, error or None) if profile.display_name else None,
        cls="max-w-3xl",
    )


def OpeningForm(uid, f=None, error=None):
    f = f or {}
    my_orgs = data.coordinator_orgs(uid)
    org_id = f.get("org_id") or (str(my_orgs[0][0].id) if len(my_orgs) == 1 else "")
    return Page(
        "Post an opening",
        Form(
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
                    name="neighborhood", value=f.get("neighborhood", ""), required=True
                ),
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
            LabelCheckboxX(
                ADULT_ONLY,
                name="adult_only_ok",
                value="1",
                checked=bool(f.get("adult_only_ok")),
            ),
            Field(
                "Best way to send you interested volunteers?",
                Choice(
                    "reply_channel",
                    reply_options(bool(data.get_profile(uid).contact_phone)),
                    f.get("reply_channel", ""),
                ),
            ),
            Button("Post opening", cls=ButtonT.primary + " w-full"),
            action="/org/openings/new",
            method="post",
        ),
        error=error,
        intro="Volunteers who fit see it after you post it. You can close it any time.",
    )


@ar("/org/details", methods=["POST"])
async def save_details(req):
    form = await req.form()
    if any(not isinstance(v, str) for _, v in form.multi_items()):
        return Response("Bad request", status_code=400)
    return await run_in_threadpool(_sync_save_details, req, form)


def _sync_save_details(req, form):
    uid = req.scope["uid"]
    name, phone = form.get("name", "").strip(), form.get("phone", "").strip()
    if not name or len(name) > 80:
        return org_home(req, "Enter your name and role (80 characters at most).")
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
        return bad_request()
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
    }

    def fail(msg):
        return OpeningForm(uid, f, msg)

    if not re.fullmatch(r"[0-9]{1,18}", f["org_id"]):
        return fail("Please choose your organization.")
    if any(len(f[k]) > n for k, n in LIMITS.items()):
        return fail("One of your answers is too long. Please shorten it.")
    if not f["title"] or not f["about"] or not f["neighborhood"]:
        return fail(
            "Please fill in the title, what volunteers will do, and the neighborhood."
        )
    if f["cause"] not in {c.slug for c in data.active_causes()}:
        return fail("Please pick a cause from the list.")
    latlon = city_zip_latlon(f["zip"])
    if not latlon:
        return fail(
            "Openings must be in the City of Los Angeles for now. Check the ZIP code."
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
        )
    except PermissionError:
        return Response("Forbidden", status_code=403)
    return Redirect("/org")


@ar("/org/openings/{opening_id}/close", methods=["POST"])
def close(req, opening_id: int):
    try:
        data.close_opening(req.scope["uid"], opening_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
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


def InboxRow(m):
    accept = (
        Label("Full")
        if m["full"]
        else Button("Accept", name="decision", value="accept", cls=ButtonT.primary)
    )
    return Card(
        DivFullySpaced(
            P(m["first_name"], cls="font-semibold"), Label(INBOX_STATUS[m["status"]])
        ),
        P(f"Interested in: {m['opening']}", cls=TextPresets.muted_sm),
        P("Causes: " + (", ".join(m["causes"]) or "none given")),
        P(
            "Available: "
            + (", ".join(label_of(AVAILABILITY, a) for a in m["availability"]) or "any")
        ),
        P(f"Neighborhood: {m['neighborhood'] or 'not given'}"),
        P(f"Wants to help: {label_of(FREQUENCY, m['frequency']) or 'not given'}"),
        A("Chat", href=f"/org/chat/{m['id']}", cls=AT.muted + " mr-4")
        if m["status"] in ("accepted", "withdrawn")
        else None,
        A(
            "Schedule another shift",
            href=f"/org/matches/{m['id']}/shifts/new",
            cls=AT.muted,
        )
        if m["status"] == "accepted"
        else None,
        RepeatForm(m["id"]) if m["status"] == "accepted" and m["weekly"] else None,
        A("Report", href=f"/org/matches/{m['id']}/report", cls=AT.muted + " mr-4"),
        A("Block", href=f"/org/matches/{m['id']}/block", cls=AT.muted),
        PlainForm(
            accept,
            Button("Decline", name="decision", value="decline", cls=ButtonT.default),
            action=f"/org/matches/{m['id']}/decide",
            method="post",
            cls="flex gap-3",
        )
        if m["status"] == "pending"
        else None,
        cls="w-full",
    )


@ar("/org/inbox", methods=["GET"])
def inbox(req):
    notice = query(req, "notice")
    box = data.org_inbox(req.scope["uid"])
    return Container(
        H1("Volunteer inbox", cls="text-2xl font-bold my-4"),
        Alert(INBOX_NOTICE[notice], cls=AlertT.warning + " mb-4")
        if notice in INBOX_NOTICE
        else None,
        H2("Waiting for you", cls="text-xl font-semibold"),
        Div(
            *([InboxRow(m) for m in box["pending"]] or [P("No one is waiting.")]),
            cls="flex flex-col gap-3 pb-6",
        ),
        H2("Decided", cls="text-xl font-semibold"),
        Div(
            *([InboxRow(m) for m in box["decided"]] or [P("Nothing yet.")]),
            cls="flex flex-col gap-3 pb-6",
        ),
        H2("Past matches", cls="text-xl font-semibold") if box["past"] else None,
        Div(
            *[InboxRow(m) for m in box["past"]],
            cls="flex flex-col gap-3 pb-8",
        )
        if box["past"]
        else None,
        cls="max-w-3xl",
    )


@ar("/org/matches/{match_id}/decide", methods=["POST"])
async def decide(req, match_id: int):
    form = await str_form(req)
    decision = form.get("decision") if form else None
    if decision not in ("accept", "decline"):
        return bad_request()
    return await run_in_threadpool(_sync_decide, req, form, match_id)


def _sync_decide(req, form, match_id: int):
    decision = form.get("decision") if form else None
    try:
        data.decide(req.scope["uid"], match_id, decision == "accept")
    except PermissionError:
        return Response("Forbidden", status_code=403)
    except ValueError as e:
        return Redirect(
            f"/org/inbox?notice={'full' if 'slots' in str(e) else 'decided'}"
        )
    if decision == "accept":
        notify.on_accepted(match_id)
    return Redirect("/org/inbox")
