import re
from datetime import datetime
from zoneinfo import ZoneInfo

from fasthtml.common import H2, APIRouter, Div, Input, P, Redirect, Response, Textarea
from fasthtml.common import Form as PlainForm
from monsterui.all import (
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

from app import data
from app.geo import zip_latlon
from app.ui import Choice, Field, Page

ar = APIRouter()

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
def org_home(req):
    uid = req.scope["uid"]
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
        Div(*sections, cls="flex flex-col gap-8 pb-4"),
        Div(
            Button(
                "Post an opening",
                cls=ButtonT.primary,
                onclick="location='/org/openings/new'",
            ),
            cls="pb-8",
        ),
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
                Input(name="title", value=f.get("title", ""), required=True),
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
                Input(
                    name="zip",
                    value=f.get("zip", ""),
                    inputmode="numeric",
                    maxlength="5",
                    required=True,
                ),
            ),
            Field(
                "Neighborhood",
                Input(
                    name="neighborhood", value=f.get("neighborhood", ""), required=True
                ),
            ),
            Field(
                "Date and time of the next shift",
                Input(
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
                Input(
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
                Choice("reply_channel", REPLY, f.get("reply_channel", "")),
            ),
            Button("Post opening", cls=ButtonT.primary + " w-full"),
            action="/org/openings/new",
            method="post",
        ),
        error=error,
        intro="Volunteers who fit see it after you post it. You can close it any time.",
    )


@ar("/org/openings/new", methods=["GET"])
def new_opening_page(req):
    return OpeningForm(req.scope["uid"])


@ar("/org/openings/new", methods=["POST"])
async def new_opening_save(req):
    uid = req.scope["uid"]
    form = await req.form()
    if any(not isinstance(v, str) for _, v in form.multi_items()):
        return Response("Bad request", status_code=400)
    text = lambda k: form.get(k, "").strip()
    f = {
        **{k: text(k) for k in LIMITS},
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
    latlon = zip_latlon(f["zip"])
    if not latlon:
        return fail(
            "Causematch is only in Los Angeles County for now. Check the ZIP code."
        )
    try:
        start = datetime.strptime(f["starts_at"], "%Y-%m-%dT%H:%M")
    except ValueError:
        return fail("Please enter the date and time of the next shift.")
    if start <= datetime.now(LA).replace(tzinfo=None):
        return fail("The next shift has to be in the future.")
    if f["recurrence"] not in {v for v, _ in RECURRENCE}:
        return fail("Please say whether the opening repeats.")
    if not re.fullmatch(r"[0-9]{1,3}", f["slots"]) or not 1 <= int(f["slots"]) <= 200:
        return fail("Volunteers needed must be a number from 1 to 200.")
    if f["orientation_required"] not in {v for v, _ in ORIENTATION}:
        return fail("Please say whether an orientation is required.")
    if f["reply_channel"] not in {v for v, _ in REPLY}:
        return fail("Please choose how we should send you interested volunteers.")
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
