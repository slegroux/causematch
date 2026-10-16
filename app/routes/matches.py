import re

from fasthtml.common import H1, H2, A, Div, P, Redirect, Span
from fasthtml.common import Form as PlainForm
from monsterui.all import (
    Alert,
    AlertT,
    Button,
    ButtonT,
    TextPresets,
)
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app import data, notify
from app.forms import bad_request, query, str_form
from app.routes.deck import DeckArea, deck_view
from app.ui import (
    CALENDAR,
    CARD,
    CHAT,
    HEART,
    SMALL_LINK,
    TITLE,
    EventLink,
    Icon,
    MatchesDot,
    Meta,
    Pill,
    Refusal,
    Router,
    VolunteerShell,
    when,
)

ar = Router()

STATUS = {  # status: (label, PILL tone)
    "pending": ("Waiting for the nonprofit", "wait"),
    "accepted": ("Accepted", "good"),
    "declined": ("Not this time", "off"),
    "withdrawn": ("Withdrawn", "off"),
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
    today = form.get("today") == "1"  # the deck's "I'm free today" switch
    view = deck_view(req)
    notice = None
    try:
        if not re.fullmatch(r"[0-9]{1,19}", raw):
            raise LookupError("not an opening id")
        call = form.get("call_id", "")
        m = data.swipe_created(
            uid,
            int(raw),
            form["liked"] == "1",
            today,
            view,
            int(call) if re.fullmatch(r"[0-9]{1,18}", call) else None,
        )
    except data.CallPosted as e:
        # an urgent call went up since the card was drawn: show its card, record nothing
        if not req.headers.get("HX-Request"):
            return Redirect(f"/deck?card={e.call.opening_id}")
        return DeckArea(
            uid,
            today,
            view,
            f"This opening now needs people {data.urgent_when(e.call.starts_at)}. "
            "Have a look before you say yes.",
            first=e.call.opening_id,
        )
    except LookupError:
        # closed or gone since the card was drawn: say so, and show what's next
        notice = "That opening isn't available any more."
    else:
        if m and m.status == "accepted":  # an urgent call took them: off to the chat
            notify.on_auto_accepted(m.id)
            if req.headers.get("HX-Request"):
                return Response(headers={"HX-Redirect": f"/chat/{m.id}?notice=in"})
            return Redirect(f"/chat/{m.id}?notice=in")
        if m:
            notify.on_new_like(m.id)
            notice = f"Sent to {data.org_name_of(m.opening_id)}. We'll text you when they say yes."
    if req.headers.get("HX-Request"):
        return DeckArea(uid, today, view, notice), MatchesDot(
            data.has_pending_like(uid), oob=True
        )
    return Redirect("/deck?today=1" if today else "/deck")


def MatchRow(m):
    return Div(
        Div(
            Pill(*STATUS[m["status"]]),
            Pill("Blocked", "off") if m["blocked"] else None,
            cls="flex flex-wrap gap-1.5",
        ),
        H2(m["title"], cls="font-display text-lg font-extrabold leading-snug text-ink"),
        P(m["org"], cls="text-sm font-semibold text-ink-muted"),
        Meta(CALENDAR, f"{when(m['starts_at'])} · {m['neighborhood']}"),
        EventLink(m.get("event_url")),
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
    return VolunteerShell(
        "matches",
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
    )


@ar("/matches/{match_id}/withdraw", methods=["GET"])
def withdraw_confirm(req, match_id: int):
    try:
        m = data.my_match(req.scope["uid"], match_id)
    except PermissionError:
        return Refusal(403)
    if m["status"] not in ("pending", "accepted"):
        return Redirect("/matches")
    return VolunteerShell(
        "matches",
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
    )


@ar("/matches/{match_id}/withdraw", methods=["POST"])
def withdraw(req, match_id: int):
    try:
        data.withdraw(req.scope["uid"], match_id)
    except PermissionError:
        return Refusal(403)
    except ValueError:
        pass  # already withdrawn or declined: nothing to do
    return Redirect("/matches")
