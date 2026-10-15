import re

from fasthtml.common import H1, H2, A, Div, P, Redirect, Response, Span
from fasthtml.common import Form as PlainForm
from monsterui.all import (
    Alert,
    AlertT,
    Button,
    ButtonT,
    Container,
    TextPresets,
)
from starlette.concurrency import run_in_threadpool

from app import data, notify
from app.forms import bad_request, query, str_form
from app.routes.deck import DeckArea, when
from app.ui import (
    CALENDAR,
    CARD,
    CHAT,
    HEART,
    SMALL_LINK,
    TITLE,
    Icon,
    Pill,
    Router,
    VolunteerNav,
)

ar = Router()

STATUS = {
    "pending": "Waiting for the nonprofit",
    "accepted": "Accepted",
    "declined": "Not this time",
    "withdrawn": "Withdrawn",
}
TONE = {"pending": "wait", "accepted": "good", "declined": "off", "withdrawn": "off"}


NOTICE = {
    "reported": "Thanks. We read every report.",
    "blocked": "Blocked. Their openings won't show up again.",
}


@ar("/swipe", methods=["POST"])
async def swipe(req):
    form = await str_form(req)
    if form is None or form.get("liked") not in ("0", "1"):
        return bad_request()
    return await run_in_threadpool(_sync_swipe, req, form)


def _sync_swipe(req, form):
    uid = req.scope["uid"]
    raw = form.get("opening_id", "")
    try:
        if not re.fullmatch(r"[0-9]{1,19}", raw):
            raise LookupError("not an opening id")
        m = data.swipe_created(uid, int(raw), form["liked"] == "1")
    except LookupError:
        pass  # closed or gone since the card was drawn: just show the next one
    else:
        if m:
            notify.on_new_like(m.id)
    if req.headers.get("HX-Request"):
        return DeckArea(uid)
    return Redirect("/deck")


def MatchRow(m):
    return Div(
        Div(
            Pill(STATUS[m["status"]], TONE[m["status"]]),
            Pill("Blocked", "off") if m["blocked"] else None,
            cls="flex flex-wrap gap-1.5",
        ),
        H2(m["title"], cls="font-display text-lg font-extrabold leading-snug text-ink"),
        P(m["org"], cls="text-sm font-semibold text-ink-muted"),
        P(
            Icon(CALENDAR, size=16, cls="text-ink-muted"),
            Span(f"{when(m['starts_at'])} · {m['neighborhood']}"),
            cls="flex items-center gap-2 text-sm text-ink",
        ),
        A(
            Icon(CHAT, size=18),
            "Chat",
            href=f"/chat/{m['id']}",
            cls="uk-btn uk-btn-primary mt-2 w-full gap-2",
        )
        if m["status"] == "accepted"
        else None,
        Div(
            A("Withdraw", href=f"/matches/{m['id']}/withdraw", cls=SMALL_LINK)
            if m["status"] in ("pending", "accepted")
            else None,
            A("Report", href=f"/matches/{m['id']}/report", cls=SMALL_LINK),
            None
            if m["blocked"]
            else A("Block", href=f"/matches/{m['id']}/block", cls=SMALL_LINK),
            cls="mt-1 flex flex-wrap gap-x-5 border-t border-line pt-1 text-sm",
        ),
        cls="w-full space-y-1.5 p-4 " + CARD,
    )


def NoMatches():
    return Div(
        Span(
            Icon(HEART, size=28),
            cls="mx-auto grid h-14 w-14 place-items-center rounded-full bg-brand-soft "
            "text-brand",
        ),
        P("No matches yet. Like an opening to start one.", cls="text-ink-muted"),
        A("Find openings", href="/deck", cls="uk-btn uk-btn-primary px-6"),
        cls="space-y-3 px-6 py-10 text-center " + CARD,
    )


@ar("/matches", methods=["GET"])
def matches_page(req):
    rows = data.my_matches(req.scope["uid"])
    notice = query(req, "notice")
    return Container(
        VolunteerNav("matches"),
        Div(
            H1("Your matches", cls=TITLE),
            A("History", href="/history", cls=SMALL_LINK + " text-sm"),
            cls="flex items-center justify-between gap-4",
        ),
        Alert(NOTICE[notice], cls=AlertT.info + " mb-4") if notice in NOTICE else None,
        Div(
            *([MatchRow(m) for m in rows] or [NoMatches()]),
            cls="flex flex-col gap-4 pb-8",
        ),
        cls="max-w-md pb-8",
    )


@ar("/matches/{match_id}/withdraw", methods=["GET"])
def withdraw_confirm(req, match_id: int):
    try:
        m = data.my_match(req.scope["uid"], match_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    if m["status"] not in ("pending", "accepted"):
        return Redirect("/matches")
    return Container(
        VolunteerNav("matches"),
        Div(
            H1(
                f"Withdraw from {m['title']}?",
                cls="font-display text-xl font-extrabold leading-snug text-ink",
            ),
            P(
                "You won't be able to like this opening again.",
                cls=TextPresets.muted_sm,
            ),
            PlainForm(
                Button("Withdraw", cls=ButtonT.destructive),
                A("Keep it", href="/matches", cls=SMALL_LINK + " ml-4"),
                action=f"/matches/{m['id']}/withdraw",
                method="post",
                cls="flex items-center pt-2",
            ),
            cls="mt-4 space-y-3 p-5 " + CARD,
        ),
        cls="max-w-md pb-8",
    )


@ar("/matches/{match_id}/withdraw", methods=["POST"])
def withdraw(req, match_id: int):
    try:
        data.withdraw(req.scope["uid"], match_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    except ValueError:
        pass  # already withdrawn or declined: nothing to do
    return Redirect("/matches")
