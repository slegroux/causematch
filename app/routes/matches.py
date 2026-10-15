from fasthtml.common import H1, A, APIRouter, Div, P, Redirect, Response
from fasthtml.common import Form as PlainForm
from monsterui.all import (
    AT,
    Button,
    ButtonT,
    Card,
    Container,
    DivFullySpaced,
    Label,
    TextPresets,
)

from app import data
from app.routes.deck import DeckArea, when
from app.ui import VolunteerNav

ar = APIRouter()

STATUS = {
    "pending": "Waiting for the nonprofit",
    "accepted": "Accepted",
    "declined": "Not this time",
    "withdrawn": "Withdrawn",
}


@ar("/swipe", methods=["POST"])
def swipe(req, opening_id: int, liked: str):
    uid = req.scope["uid"]
    if liked not in ("0", "1"):
        return Response("Bad request", status_code=400)
    try:
        data.swipe(uid, opening_id, liked == "1")
    except LookupError:
        pass  # closed or gone since the card was drawn: just show the next one
    if req.headers.get("HX-Request"):
        return DeckArea(uid)
    return Redirect("/deck")


def MatchRow(m):
    return Card(
        DivFullySpaced(P(m["title"], cls="font-semibold"), Label(STATUS[m["status"]])),
        P(m["org"], cls=TextPresets.muted_sm),
        P(f"{when(m['starts_at'])} - {m['neighborhood']}", cls=TextPresets.muted_sm),
        A("Withdraw", href=f"/matches/{m['id']}/withdraw", cls=AT.muted)
        if m["status"] in ("pending", "accepted")
        else None,
        cls="w-full",
    )


@ar("/matches", methods=["GET"])
def matches_page(req):
    rows = data.my_matches(req.scope["uid"])
    return Container(
        VolunteerNav(),
        H1("Your matches", cls="text-2xl font-bold my-4"),
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
        VolunteerNav(),
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
