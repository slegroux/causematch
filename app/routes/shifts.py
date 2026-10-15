from fasthtml.common import H1, H2, A, Div, P, Redirect, Response
from fasthtml.common import Form as PlainForm
from monsterui.all import (
    AT,
    Alert,
    AlertT,
    Button,
    ButtonT,
    Container,
    Form,
    TextPresets,
)
from starlette.concurrency import run_in_threadpool

from app import data
from app.forms import bad_request, query, str_form
from app.routes.deck import when
from app.ui import CARD, TITLE, Field, Page, Pill, Router, TextInput, VolunteerNav

ar = Router()

SHIFT_STATUS = {
    "scheduled": "Not marked yet",
    "completed": "Attended",
    "no_show": "No-show",
}
SHIFT_TONE = {"scheduled": "wait", "completed": "good", "no_show": "off"}
NOTICE = {
    "unavailable": (
        "That didn't go through. A shift can be marked once it has started, corrected "
        "once within 7 days, and cancelled only before it starts."
    ),
    "repeat": "Couldn't add next week's shift (it needs a weekly opening, and a date still ahead).",
}


def ShiftCard(s, *actions, for_org=True, status=None, tone=None):
    """for_org=True: a coordinator's card, naming the volunteer; else the org's name.
    status and tone override the pill's text and color (PILL in app/ui.py)."""
    return Div(
        Pill(status or SHIFT_STATUS[s["status"]], tone or SHIFT_TONE[s["status"]]),
        P(s["title"], cls="font-bold leading-snug text-ink"),
        P(
            f"{when(s['starts_at'])} - {s['first_name'] if for_org else s['org']}",
            cls=TextPresets.muted_sm,
        ),
        *actions,
        cls="w-full space-y-2 p-4 " + CARD,
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


def RepeatForm(match_id):
    return PlainForm(
        Button("Same time next week", cls=ButtonT.default),
        action=f"/org/matches/{match_id}/shifts/repeat",
        method="post",
    )


def CancelForm(shift_id):
    return PlainForm(
        Button("Cancel this shift", cls=ButtonT.default),
        action=f"/org/shifts/{shift_id}/cancel",
        method="post",
    )


def repeat_of(s):
    """The next-week button, on a weekly opening's latest shift only."""
    return RepeatForm(s["match_id"]) if s["weekly"] and s.get("latest") else None


def Section(title, rows, empty):
    return Div(
        H2(title, cls="font-display text-lg font-extrabold text-ink"),
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
            [ShiftCard(s, MarkForm(s), repeat_of(s)) for s in box["to_mark"]],
            "Nothing to mark.",
        ),
        Section(
            "Upcoming",
            [
                ShiftCard(
                    s,
                    A("Chat", href=f"/org/chat/{s['match_id']}", cls=AT.muted),
                    repeat_of(s),
                    CancelForm(s["id"]),
                )
                for s in box["upcoming"]
            ],
            "No upcoming shifts.",
        ),
        Section(
            "Marked this week (one correction allowed)",
            [
                ShiftCard(s, MarkForm(s, corrected=True), repeat_of(s))
                for s in box["marked"]
            ],
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
    return await run_in_threadpool(_sync_mark, req, form, shift_id)


def _sync_mark(req, form, shift_id: int):
    result = form.get("result") if form else None
    try:
        data.complete_shift(req.scope["uid"], shift_id, result == "attended")
    except PermissionError:
        return Response("Forbidden", status_code=403)
    except ValueError:
        return Redirect("/org/shifts?notice=unavailable")
    return Redirect("/org/shifts")


@ar("/org/shifts/{shift_id}/cancel", methods=["POST"])
def cancel(req, shift_id: int):
    try:
        data.cancel_shift(req.scope["uid"], shift_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    except ValueError:
        return Redirect("/org/shifts?notice=unavailable")
    return Redirect("/org/shifts")


@ar("/org/matches/{match_id}/shifts/repeat", methods=["POST"])
def repeat(req, match_id: int):
    try:
        data.repeat_shift(req.scope["uid"], match_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    except ValueError:
        return Redirect("/org/shifts?notice=repeat")
    return Redirect("/org/shifts")


def ShiftForm(match_id, head, error=None, value=""):
    return Page(
        "Schedule another shift",
        P(f"{head['title']} with {head['other']}", cls=TextPresets.muted_sm + " mb-4"),
        Form(
            Field(
                "Date and time (Los Angeles time)",
                TextInput(
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
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_new_shift_save, req, form, match_id)


def _sync_new_shift_save(req, form, match_id: int):
    uid = req.scope["uid"]
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
        VolunteerNav("me"),
        H1("Your history", cls=TITLE),
        Section(
            "Upcoming",
            [
                ShiftCard(s, for_org=False, status="Scheduled", tone="info")
                for s in mine["upcoming"]
            ],
            "No upcoming shifts. Like an opening to get one.",
        ),
        Section(
            "Past",
            [
                ShiftCard(
                    s,
                    for_org=False,
                    status="Waiting for the nonprofit to confirm"
                    if s["status"] == "scheduled"
                    else None,
                )
                for s in mine["past"]
            ],
            "Nothing yet.",
        ),
        cls="max-w-md pb-8",
    )
