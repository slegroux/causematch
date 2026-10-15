from fasthtml.common import H1, H2, A, APIRouter, Div, Input, P, Redirect, Response
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
    TextPresets,
)

from app import data
from app.forms import bad_request, query, str_form
from app.routes.deck import when
from app.ui import Field, Page, VolunteerNav

ar = APIRouter()

SHIFT_STATUS = {
    "scheduled": "Not marked yet",
    "completed": "Attended",
    "no_show": "No-show",
}
NOTICE = {
    "unavailable": "That shift can't be marked right now (it hasn't started, or it was already corrected).",
}


def ShiftCard(s, actions=None, volunteer=True):
    """volunteer=True: a coordinator's card, naming the volunteer; else the org's name."""
    status = SHIFT_STATUS[s["status"]]
    if not volunteer and s["status"] == "scheduled":
        status = "Waiting for the nonprofit to confirm"
    return Card(
        DivFullySpaced(P(s["title"], cls="font-semibold"), Label(status)),
        P(
            f"{when(s['starts_at'])} - {s['first_name'] if volunteer else s['org']}",
            cls=TextPresets.muted_sm,
        ),
        actions,
        cls="w-full",
    )


def MarkForm(s, corrected=False):
    attended = Button("Attended", name="result", value="attended", cls=ButtonT.primary)
    no_show = Button("No-show", name="result", value="no_show", cls=ButtonT.default)
    if corrected:  # the one correction: offer only the other answer
        buttons = [no_show if s["status"] == "completed" else attended]
    else:
        buttons = [attended, no_show]
    return PlainForm(
        *buttons,
        action=f"/org/shifts/{s['id']}/mark",
        method="post",
        cls="flex gap-3",
    )


def Section(title, rows, empty):
    return Div(
        H2(title, cls="text-xl font-semibold"),
        *(rows or [P(empty, cls=TextPresets.muted_sm)]),
        cls="flex flex-col gap-3 pb-6",
    )


@ar("/org/shifts", methods=["GET"])
def org_shifts_page(req):
    notice = query(req, "notice")
    box = data.org_shifts(req.scope["uid"])
    return Container(
        H1("Shifts", cls="text-2xl font-bold my-4"),
        Alert(NOTICE[notice], cls=AlertT.warning + " mb-4")
        if notice in NOTICE
        else None,
        Section(
            "Did they come?",
            [ShiftCard(s, MarkForm(s)) for s in box["to_mark"]],
            "Nothing to mark.",
        ),
        Section(
            "Upcoming",
            [
                ShiftCard(
                    s,
                    A("Chat", href=f"/org/chat/{s['match_id']}", cls=AT.muted),
                )
                for s in box["upcoming"]
            ],
            "No upcoming shifts.",
        ),
        Section(
            "Marked this week (one correction allowed)",
            [ShiftCard(s, MarkForm(s, corrected=True)) for s in box["marked"]],
            "Nothing marked this week.",
        ),
        A("Back to your openings", href="/org", cls=AT.muted + " pb-8 block"),
        cls="max-w-3xl",
    )


@ar("/org/shifts/{shift_id}/mark", methods=["POST"])
async def mark(req, shift_id: int):
    form = await str_form(req)
    result = form.get("result") if form else None
    if result not in ("attended", "no_show"):
        return bad_request()
    try:
        data.complete_shift(req.scope["uid"], shift_id, result == "attended")
    except PermissionError:
        return Response("Forbidden", status_code=403)
    except ValueError:
        return Redirect("/org/shifts?notice=unavailable")
    return Redirect("/org/shifts")


def ShiftForm(match_id, head, error=None, value=""):
    return Page(
        "Schedule another shift",
        P(f"{head['title']} with {head['other']}", cls=TextPresets.muted_sm + " mb-4"),
        Form(
            Field(
                "Date and time (Los Angeles time)",
                Input(
                    name="starts_at",
                    type="datetime-local",
                    value=value,
                    required=True,
                ),
            ),
            Button("Schedule", cls=ButtonT.primary),
            A("Cancel", href="/org/inbox", cls=AT.muted + " ml-4"),
            action=f"/org/matches/{match_id}/shifts/new",
            method="post",
            cls="space-y-3",
        ),
        error=error,
    )


def _writable_head(uid, match_id):
    """The chat header of an accepted match of the coordinator's org; PermissionError else."""
    head = data.chat_header(uid, match_id)
    if head["role"] != "coordinator" or not head["writable"]:
        raise PermissionError("no shifts on this match")
    return head


@ar("/org/matches/{match_id}/shifts/new", methods=["GET"])
def new_shift_page(req, match_id: int):
    try:
        head = _writable_head(req.scope["uid"], match_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    return ShiftForm(match_id, head)


@ar("/org/matches/{match_id}/shifts/new", methods=["POST"])
async def new_shift_save(req, match_id: int):
    uid = req.scope["uid"]
    form = await str_form(req)
    if form is None:
        return bad_request()
    starts_at = form.get("starts_at", "").strip()
    try:
        head = _writable_head(uid, match_id)
        data.add_shift(uid, match_id, starts_at)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    except ValueError as e:
        return ShiftForm(match_id, head, f"Couldn't schedule that: {e}.", starts_at)
    return Redirect("/org/shifts")


@ar("/history", methods=["GET"])
def history(req):
    mine = data.my_shifts(req.scope["uid"])
    return Container(
        VolunteerNav(),
        H1("Your shifts", cls="text-2xl font-bold my-4"),
        Section(
            "Upcoming",
            [ShiftCard(s, volunteer=False) for s in mine["upcoming"]],
            "No upcoming shifts. Like an opening to get one.",
        ),
        Section(
            "Past",
            [ShiftCard(s, volunteer=False) for s in mine["past"]],
            "Nothing yet.",
        ),
        cls="max-w-md pb-8",
    )
