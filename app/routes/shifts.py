import re
from datetime import UTC, datetime

from fasthtml.common import H1, H2, A, Div, P, Redirect, Response, Textarea
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

from app import data, notify
from app.choices import SHIFT_LENGTHS
from app.config import public_base_url
from app.forms import query, str_form
from app.ui import (
    CARD,
    ORG_SECTION,
    ORG_TITLE,
    SMALL_LINK,
    TITLE,
    Choice,
    Field,
    OrgFace,
    OrgShell,
    Pill,
    Refusal,
    Router,
    TextInput,
    VolunteerHeader,
    VolunteerShell,
    calendar_event,
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
        Div(
            None if for_org else OrgFace(s["org"], s["logo"], s["photo"], s["slug"]),
            Div(
                P(s["title"], cls="font-bold leading-snug text-ink"),
                VolunteerHeader(s["name"], s["photo_key"]) if for_org else None,
                P(
                    f"{when(s['starts_at'], s['duration_minutes'])}"
                    + ("" if for_org else f" - {s['org']}"),
                    cls=TextPresets.muted_sm,
                ),
                cls="min-w-0 space-y-2",
            ),
            cls="flex items-center gap-3",
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
                        # "Still coming?" answered yes from the reminder's link
                        status="Confirmed" if s["confirmed"] else "Upcoming",
                        tone="good" if s["confirmed"] else "info",
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


def ShiftForm(uid, match_id, head, error=None, value="", minutes=None):
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
                Field(
                    "How long is the shift?",
                    Choice(
                        "duration_minutes",
                        SHIFT_LENGTHS,
                        # the opening's length unless the coordinator chose another
                        minutes
                        if minutes is not None
                        else str(head["duration_minutes"] or ""),
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
    minutes = form.get("duration_minutes", "").strip()
    try:
        head = _writable_head(uid, match_id)
        data.add_shift(uid, match_id, starts_at, minutes or None)
    except PermissionError:
        return Refusal(403)
    except ValueError as e:
        return ShiftForm(
            uid, match_id, head, f"Couldn't schedule that: {e}.", starts_at, minutes
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
        uid=req.scope["uid"],
    )


# A volunteer's own upcoming shift: "Still coming?" (the reminder's link) and "Can't make
# this one" (Matches, the chat). Like the .ics, any other shift id is a 404.


def _own_shift(uid, shift_id, read=data.shift_event):
    try:
        return read(uid, shift_id)
    except PermissionError:
        return None


def ShiftQuestion(uid, s, title, *body):
    return VolunteerShell(
        "matches",
        Div(
            H1(title, cls="font-display text-xl font-extrabold leading-snug text-ink"),
            P(
                f"{when(s['starts_at'], s['duration_minutes'])} with {s['org']}",
                cls=TextPresets.muted_sm,
            ),
            *body,
            cls="mt-4 space-y-3 p-5 " + CARD,
        ),
        uid=uid,
    )


def CancelShiftPage(uid, s, error=None, reason=""):
    return ShiftQuestion(
        uid,
        s,
        f"Can't make {s['title']}?",
        P(
            f"This gives up your spot at {s['org']}. They'll be told right away. You "
            "won't be able to like this opening again."
            if s["gives_up_spot"]
            else f"Only this shift is cancelled: you stay matched, and {s['org']} hears "
            "from us right away.",
            cls="text-sm text-ink",
        ),
        PlainForm(
            Field(
                f"Want to say why? (optional, up to {data.MAX_CANCEL_REASON} characters)",
                Textarea(
                    reason,
                    name="reason",
                    rows=3,
                    maxlength=str(data.MAX_CANCEL_REASON),
                    cls="uk-textarea",
                ),
            ),
            Alert(error, cls=AlertT.error) if error else None,
            Div(
                Button("Cancel this shift", cls=ButtonT.destructive),
                A("Keep it", href="/matches", cls=SMALL_LINK),
                cls="flex items-center gap-4",
            ),
            action=f"/shifts/{s['id']}/cancel",
            method="post",
            cls="space-y-3 pt-2",
        ),
    )


NOT_FOUND = "Not found"


@ar("/shifts/{shift_id}/cancel", methods=["GET"])
def cancel_mine_page(req, shift_id: int):
    s = _own_shift(req.scope["uid"], shift_id, data.shift_to_cancel)
    if s is None:
        return Response(NOT_FOUND, status_code=404)
    return CancelShiftPage(req.scope["uid"], s)


@ar("/shifts/{shift_id}/cancel", methods=["POST"])
async def cancel_mine(req, shift_id: int):
    form = await str_form(req)
    if form is None:
        return Refusal(400)
    return await run_in_threadpool(_sync_cancel_mine, req, form, shift_id)


def _sync_cancel_mine(req, form, shift_id: int):
    uid = req.scope["uid"]
    reason = form.get("reason", "")
    try:
        done = data.cancel_my_shift(uid, shift_id, reason)
    except PermissionError:
        return Response(NOT_FOUND, status_code=404)
    except ValueError:
        s = _own_shift(uid, shift_id, data.shift_to_cancel)
        if s is None:
            return Response(NOT_FOUND, status_code=404)
        return CancelShiftPage(
            uid,
            s,
            "That reason holds a character we can't show, or is too long.",
            reason[: data.MAX_CANCEL_REASON],
        )
    notify.on_shift_cancelled(shift_id, done)
    return Redirect(f"/matches?notice={'gave_up' if done['ended'] else 'cancelled'}")


@ar("/shifts/{shift_id}/confirm", methods=["GET"])
def confirm_page(req, shift_id: int):
    uid = req.scope["uid"]
    s = _own_shift(uid, shift_id)
    if s is None:
        return Response(NOT_FOUND, status_code=404)
    return ShiftQuestion(
        uid,
        s,
        f"Still coming to {s['title']}?",
        P("You told them you're coming. Thanks!", cls="text-sm font-semibold text-ink")
        if s["confirmed_at"]
        else None,
        Div(
            None
            if s["confirmed_at"]
            else PlainForm(
                Button("Yes, I'll be there", cls=ButtonT.primary),
                action=f"/shifts/{s['id']}/confirm",
                method="post",
            ),
            A("Can't make it", href=f"/shifts/{s['id']}/cancel", cls=SMALL_LINK),
            cls="flex flex-wrap items-center gap-4 pt-2",
        ),
    )


@ar("/shifts/{shift_id}/confirm", methods=["POST"])
def confirm(req, shift_id: int):
    try:
        data.confirm_my_shift(req.scope["uid"], shift_id)
    except PermissionError:
        return Response(NOT_FOUND, status_code=404)
    return Redirect("/matches?notice=confirmed")


# GET /shifts/{id}.ics: one VEVENT, written by hand (RFC 5545). The start is wall clock
# with a TZID and the zone's own VTIMEZONE (US rules since 2007), which iPhone Calendar
# imports as Los Angeles time.
VTIMEZONE = (
    "BEGIN:VTIMEZONE",
    "TZID:America/Los_Angeles",
    "BEGIN:DAYLIGHT",
    "TZOFFSETFROM:-0800",
    "TZOFFSETTO:-0700",
    "TZNAME:PDT",
    "DTSTART:19700308T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "TZOFFSETFROM:-0700",
    "TZOFFSETTO:-0800",
    "TZNAME:PST",
    "DTSTART:19701101T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
    "END:STANDARD",
    "END:VTIMEZONE",
)


def ics_text(value):
    """A TEXT value: backslash, semicolon, comma and newline escaped (RFC 5545 3.3.11)."""
    value = value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return re.sub(r"\r\n|\r|\n", r"\\n", value)


def ics_fold(line):
    """Lines of at most 75 octets, continued with CRLF and a space; never inside a
    UTF-8 character."""
    out, part = [], ""
    for ch in line:
        if len((part + ch).encode()) > (75 if not out else 74):
            out.append(part)
            part = ""
        part += ch
    return "\r\n ".join([*out, part])


def ics(s):
    e = calendar_event(s)
    host = public_base_url().split("://", 1)[1]
    lines = (
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Causematch//Shifts//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        *VTIMEZONE,
        "BEGIN:VEVENT",
        f"UID:shift-{s['id']}@{host}",
        "DTSTAMP:" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        f"DTSTART;TZID=America/Los_Angeles:{e['start']}",
        f"DTEND;TZID=America/Los_Angeles:{e['end']}",
        "SUMMARY:" + ics_text(e["title"]),
        "LOCATION:" + ics_text(e["place"]),
        "DESCRIPTION:" + ics_text(e["details"]),
        "END:VEVENT",
        "END:VCALENDAR",
    )
    return "".join(ics_fold(line) + "\r\n" for line in lines)


@ar("/shifts/{shift_id}.ics", methods=["GET"])
def shift_ics(req, shift_id: int):
    """Only the volunteer on the shift's match, while it is upcoming; 404 for anyone else,
    and for a cancelled or past shift."""
    try:
        s = data.shift_event(req.scope["uid"], shift_id)
    except PermissionError:
        return Response("Not found", status_code=404)
    return Response(
        ics(s),
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="shift-{s["id"]}.ics"',
            "Cache-Control": "private, no-store",
        },
    )
