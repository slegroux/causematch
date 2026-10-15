import re

from fasthtml.common import H1, A, APIRouter, Div, P, Redirect, Response
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
    Label,
    TextPresets,
)
from starlette.concurrency import run_in_threadpool

from app import data, notify
from app.forms import bad_request, query, str_form
from app.routes.deck import DeckArea, when
from app.ui import VolunteerNav, body_wrap

ar = APIRouter(body_wrap=body_wrap)  # the demo banner, when it is on

STATUS = {
    "pending": "Waiting for the nonprofit",
    "accepted": "Accepted",
    "declined": "Not this time",
    "withdrawn": "Withdrawn",
}


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
    return Card(
        DivFullySpaced(P(m["title"], cls="font-semibold"), Label(STATUS[m["status"]])),
        P(m["org"], cls=TextPresets.muted_sm),
        P(f"{when(m['starts_at'])} - {m['neighborhood']}", cls=TextPresets.muted_sm),
        A("Chat", href=f"/chat/{m['id']}", cls=AT.muted + " mr-4")
        if m["status"] == "accepted"
        else None,
        A("Withdraw", href=f"/matches/{m['id']}/withdraw", cls=AT.muted + " mr-4")
        if m["status"] in ("pending", "accepted")
        else None,
        A("Report", href=f"/matches/{m['id']}/report", cls=AT.muted + " mr-4"),
        Label("Blocked")
        if m["blocked"]
        else A("Block", href=f"/matches/{m['id']}/block", cls=AT.muted),
        cls="w-full",
    )


@ar("/matches", methods=["GET"])
def matches_page(req):
    rows = data.my_matches(req.scope["uid"])
    notice = query(req, "notice")
    return Container(
        VolunteerNav("matches"),
        H1("Your matches", cls="text-2xl font-bold my-4"),
        Alert(NOTICE[notice], cls=AlertT.info + " mb-4") if notice in NOTICE else None,
        A("History", href="/history", cls=AT.muted + " block pb-4"),
        Div(
            *(
                [MatchRow(m) for m in rows]
                or [P("No matches yet. Like an opening to start one.")]
            ),
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
        H1(f"Withdraw from {m['title']}?", cls="text-2xl font-bold my-4"),
        P(
            "You won't be able to like this opening again.",
            cls=TextPresets.muted_sm + " mb-4",
        ),
        PlainForm(
            Button("Withdraw", cls=ButtonT.destructive),
            A("Keep it", href="/matches", cls=AT.muted + " ml-4"),
            action=f"/matches/{m['id']}/withdraw",
            method="post",
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
