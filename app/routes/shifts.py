from fasthtml.common import H1, H2, A, Div, P, Redirect
from fasthtml.common import Form as PlainForm
from monsterui.all import (
    Alert,
    AlertT,
    Button,
    ButtonT,
    Form,
    TextPresets,
)
from starlette.concurrency import run_in_threadpool

from app import data
from app.forms import query, str_form
from app.ui import (
    CARD,
    ORG_SECTION,
    ORG_TITLE,
    SMALL_LINK,
    TITLE,
    Field,
    OrgShell,
    Pill,
    Refusal,
    Router,
    TextInput,
    VolunteerHeader,
    VolunteerShell,
    when,
)

ar = Router()

SHIFT_STATUS = {  # status: (label, PILL tone)
    "scheduled": ("Not marked yet", "wait"),
    "completed": ("Attended", "good"),
    "no_show": ("No-show", "off"),
}
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
        Pill(
            status or SHIFT_STATUS[s["status"]][0], tone or SHIFT_STATUS[s["status"]][1]
        ),
        P(s["title"], cls="font-bold leading-snug text-ink"),
        VolunteerHeader(s["name"], s["photo_key"]) if for_org else None,
        P(
            f"{when(s['starts_at'])}" + ("" if for_org else f" - {s['org']}"),
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
        H2(title, cls=ORG_SECTION),
        *(rows or [P(empty, cls=TextPresets.muted_sm)]),
        cls="flex flex-col gap-3 pb-6",
    )


@ar("/org/shifts", methods=["GET"])
def org_shifts_page(req):
    notice = query(req, "notice")
    uid = req.scope["uid"]
    box = data.org_shifts(uid)
    return OrgShell(
        "shifts",
        data.org_waiting(uid),
        H1("Shifts", cls=ORG_TITLE + " pb-4"),
        Alert(NOTICE[notice], cls=AlertT.warning + " mb-4")
        if notice in NOTICE
        else None,
        Div(
            Div(
                Section(
                    "Did they come?",
                    [ShiftCard(s, MarkForm(s), repeat_of(s)) for s in box["to_mark"]],
                    "Nothing to mark.",
                ),
                Section(
                    "Marked this week (one correction allowed)",
                    [
                        ShiftCard(s, MarkForm(s, corrected=True), repeat_of(s))
                        for s in box["marked"]
                    ],
                    "Nothing marked this week.",
                ),
            ),
            # not started yet: nothing to mark, so "Upcoming" rather than "Not marked yet"
            Section(
                "Upcoming",
                [
                    ShiftCard(
                        s,
                        Div(
                            A(
                                "Chat",
                                href=f"/org/chat/{s['match_id']}",
                                cls="uk-btn uk-btn-default",
                            ),
                            repeat_of(s),
                            CancelForm(s["id"]),
                            cls="flex flex-wrap gap-2 pt-1",
                        ),
                        status="Upcoming",
                        tone="info",
                    )
                    for s in box["upcoming"]
                ],
                "No upcoming shifts.",
            ),
            cls="grid items-start gap-x-8 lg:grid-cols-2",
        ),
    )


@ar("/org/shifts/{shift_id}/mark", methods=["POST"])
async def mark(req, shift_id: int):
    form = await str_form(req)
    result = form.get("result") if form else None
    if result not in ("attended", "no_show"):
        return Refusal(400)
    return await run_in_threadpool(_sync_mark, req, result, shift_id)


def _sync_mark(req, result, shift_id: int):
    try:
        data.complete_shift(req.scope["uid"], shift_id, result == "attended")
    except PermissionError:
        return Refusal(403)
    except ValueError:
        return Redirect("/org/shifts?notice=unavailable")
    return Redirect("/org/shifts")


@ar("/org/shifts/{shift_id}/cancel", methods=["POST"])
def cancel(req, shift_id: int):
    try:
        data.cancel_shift(req.scope["uid"], shift_id)
    except PermissionError:
        return Refusal(403)
    except ValueError:
        return Redirect("/org/shifts?notice=unavailable")
    return Redirect("/org/shifts")


@ar("/org/matches/{match_id}/shifts/repeat", methods=["POST"])
def repeat(req, match_id: int):
    try:
        data.repeat_shift(req.scope["uid"], match_id)
    except PermissionError:
        return Refusal(403)
    except ValueError:
        return Redirect("/org/shifts?notice=repeat")
    return Redirect("/org/shifts")


def ShiftForm(uid, match_id, head, error=None, value=""):
    return OrgShell(
        "inbox",
        data.org_waiting(uid),
        Div(
            H1("Schedule another shift", cls=ORG_TITLE),
            P(
                f"{head['title']} with {head['other']}",
                cls=TextPresets.muted_sm + " mt-1 mb-4",
            ),
            Alert(error, cls=AlertT.error + " mb-4") if error else None,
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
                Div(
                    Button("Schedule", cls=ButtonT.primary),
                    A("Cancel", href="/org/inbox", cls=SMALL_LINK),
                    cls="flex items-center gap-4",
                ),
                action=f"/org/matches/{match_id}/shifts/new",
                method="post",
                cls="space-y-3 p-5 " + CARD,
            ),
            cls="max-w-md",
        ),
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
        return Refusal(403)
    return ShiftForm(req.scope["uid"], match_id, head)


@ar("/org/matches/{match_id}/shifts/new", methods=["POST"])
async def new_shift_save(req, match_id: int):
    form = await str_form(req)
    if form is None:
        return Refusal(400)
    return await run_in_threadpool(_sync_new_shift_save, req, form, match_id)


def _sync_new_shift_save(req, form, match_id: int):
    uid = req.scope["uid"]
    starts_at = form.get("starts_at", "").strip()
    try:
        head = _writable_head(uid, match_id)
        data.add_shift(uid, match_id, starts_at)
    except PermissionError:
        return Refusal(403)
    except ValueError as e:
        return ShiftForm(
            uid, match_id, head, f"Couldn't schedule that: {e}.", starts_at
        )
    return Redirect("/org/shifts")


@ar("/history", methods=["GET"])
def history(req):
    mine = data.my_shifts(req.scope["uid"])
    return VolunteerShell(
        "me",
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
    )
