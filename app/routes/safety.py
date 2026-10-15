"""Report and block from both sides, account deletion, and the privacy page."""

from fasthtml.common import (
    H1,
    H2,
    A,
    APIRouter,
    Li,
    P,
    Redirect,
    Response,
    Textarea,
    Ul,
)
from fasthtml.common import Form as PlainForm
from monsterui.all import AT, Button, ButtonT, Container, LabelCheckboxX, TextPresets

from app import data
from app.choices import SHARED_WITH_NONPROFIT
from app.config import contact_email
from app.forms import bad_request, str_form
from app.ui import Choice, Field, Page, VolunteerNav

ar = APIRouter()

BAD_REPORT = "Choose a reason. Details can be up to 1000 characters."


def ReportPage(action, back, target, error=None, f=None):
    f = f or {}
    return Page(
        f"Report {target}",
        PlainForm(
            Field(
                "What happened?",
                Choice("reason", data.REASONS, f.get("reason", "")),
            ),
            Field(
                "Details (optional, up to 1000 characters)",
                Textarea(
                    f.get("details", ""),
                    name="details",
                    rows=5,
                    maxlength=str(data.MAX_DETAILS),
                    cls="uk-textarea",
                ),
            ),
            LabelCheckboxX(
                f"Also block {target}",
                name="also_block",
                value="1",
                checked=bool(f.get("also_block")),
            ),
            Button("Send report", cls=ButtonT.primary + " w-full"),
            P("A Causematch admin reads every report.", cls=TextPresets.muted_sm),
            A("Cancel", href=back, cls=AT.muted),
            action=action,
            method="post",
            cls="space-y-3",
        ),
        error=error,
    )


def BlockPage(action, back, title, effects):
    return Page(
        title,
        Ul(*[Li(e) for e in effects], cls="uk-list uk-list-disc pb-4"),
        PlainForm(
            Button("Block", cls=ButtonT.destructive),
            A("Cancel", href=back, cls=AT.muted + " ml-4"),
            action=action,
            method="post",
        ),
    )


async def _submit(req, match_id, report, block):
    form = await str_form(req)
    if form is None:
        return None, bad_request()
    f = {
        "reason": form.get("reason", ""),
        "details": form.get("details", ""),
        "also_block": form.get("also_block"),
    }
    try:
        report(req.scope["uid"], match_id, f["reason"], f["details"])
        if f["also_block"] == "1":
            block(req.scope["uid"], match_id)
    except PermissionError:
        return f, Response("Forbidden", status_code=403)
    except ValueError:
        return f, None
    return f, True


# --- volunteer side --------------------------------------------------------------------------


def _org_of(uid, match_id):
    return data.my_match(uid, match_id)["org"]


@ar("/matches/{match_id}/report", methods=["GET"])
def report_org_page(req, match_id: int):
    try:
        org = _org_of(req.scope["uid"], match_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    return ReportPage(f"/matches/{match_id}/report", "/matches", org)


@ar("/matches/{match_id}/report", methods=["POST"])
async def report_org_save(req, match_id: int):
    f, result = await _submit(req, match_id, data.report_org, data.block_org)
    if result is True:
        return Redirect("/matches?notice=reported")
    if result is not None:
        return result
    try:
        org = _org_of(req.scope["uid"], match_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    return ReportPage(f"/matches/{match_id}/report", "/matches", org, BAD_REPORT, f)


@ar("/matches/{match_id}/block", methods=["GET"])
def block_org_page(req, match_id: int):
    try:
        org = _org_of(req.scope["uid"], match_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    return BlockPage(
        f"/matches/{match_id}/block",
        "/matches",
        f"Block {org}?",
        [
            f"Their openings leave your deck and you can't match with {org} again.",
            "Your pending and accepted matches with them are withdrawn and your future shifts cancelled.",
            "Your chat with them stays readable but you can't send more messages.",
            "Only a Causematch admin can undo this.",
        ],
    )


@ar("/matches/{match_id}/block", methods=["POST"])
def block_org_save(req, match_id: int):
    try:
        data.block_org(req.scope["uid"], match_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    return Redirect("/matches?notice=blocked")


# --- coordinator side ------------------------------------------------------------------------


def _volunteer_of(uid, match_id):
    return data.org_match_volunteer(uid, match_id)


@ar("/org/matches/{match_id}/report", methods=["GET"])
def report_volunteer_page(req, match_id: int):
    try:
        who = _volunteer_of(req.scope["uid"], match_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    return ReportPage(f"/org/matches/{match_id}/report", "/org/inbox", who)


@ar("/org/matches/{match_id}/report", methods=["POST"])
async def report_volunteer_save(req, match_id: int):
    f, result = await _submit(
        req, match_id, data.report_volunteer, data.block_volunteer
    )
    if result is True:
        return Redirect("/org/inbox?notice=reported")
    if result is not None:
        return result
    try:
        who = _volunteer_of(req.scope["uid"], match_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    return ReportPage(
        f"/org/matches/{match_id}/report", "/org/inbox", who, BAD_REPORT, f
    )


@ar("/org/matches/{match_id}/block", methods=["GET"])
def block_volunteer_page(req, match_id: int):
    try:
        who = _volunteer_of(req.scope["uid"], match_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    return BlockPage(
        f"/org/matches/{match_id}/block",
        "/org/inbox",
        f"Block {who}?",
        [
            f"{who}'s waiting likes on your openings are declined and an accepted match is closed.",
            f"Any future like from {who} on your openings is dropped without telling them.",
            "Chat stays readable but no one can send more messages.",
            "Only a Causematch admin can undo this.",
        ],
    )


@ar("/org/matches/{match_id}/block", methods=["POST"])
def block_volunteer_save(req, match_id: int):
    try:
        data.block_volunteer(req.scope["uid"], match_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    return Redirect("/org/inbox?notice=blocked")


# --- account deletion ------------------------------------------------------------------------


@ar("/profile", methods=["GET"])
def profile_page(req):
    return Container(
        VolunteerNav(),
        H1("Your profile", cls="text-2xl font-bold my-4"),
        P(
            "Deleting your account removes your name, phone, email and location and "
            "withdraws your matches.",
            cls=TextPresets.muted_sm + " mb-4",
        ),
        A(
            "Delete my account",
            href="/profile/delete",
            cls="uk-btn " + ButtonT.destructive,
        ),
        P(A("Privacy policy", href="/privacy", cls=AT.muted), cls="pt-8"),
        cls="max-w-md pb-8",
    )


@ar("/profile/delete", methods=["GET"])
def delete_confirm(req):
    return Container(
        VolunteerNav(),
        H1("Delete your account?", cls="text-2xl font-bold my-4"),
        Ul(
            Li("Your name, phone, email and location are erased."),
            Li("Your pending and accepted matches are withdrawn and shifts cancelled."),
            Li('Your chat messages are replaced with "[message removed]".'),
            Li("You are signed out everywhere. This can't be undone."),
            cls="uk-list uk-list-disc pb-4",
        ),
        PlainForm(
            Button("Delete my account", cls=ButtonT.destructive),
            A("Keep it", href="/profile", cls=AT.muted + " ml-4"),
            action="/profile/delete",
            method="post",
        ),
        cls="max-w-md pb-8",
    )


@ar("/profile/delete", methods=["POST"])
def delete_save(req, sess):
    try:
        data.delete_account(req.scope["uid"])
    except PermissionError:
        return Response("Forbidden", status_code=403)
    sess.clear()
    return Redirect("/login")


# --- privacy ---------------------------------------------------------------------------------


@ar("/privacy", methods=["GET"])
def privacy():
    return Container(
        H1("Privacy", cls="text-2xl font-bold my-4"),
        H2("What we collect", cls="text-lg font-semibold mt-4"),
        P(
            "Volunteers: your mobile number, first name, optionally an email, your ZIP code "
            "and neighborhood, the causes and skills you pick, when you are free, how often "
            "you'd like to help, and how you heard of us. Nonprofits: a coordinator's email, "
            "name and optional phone, and the openings they post."
        ),
        H2("Who sees what", cls="text-lg font-semibold mt-4"),
        P(SHARED_WITH_NONPROFIT),
        P(
            "Chat messages are seen by the two sides of the match and nobody else. A "
            "Causematch admin sees a message only if it is part of a report.",
            cls="mt-2",
        ),
        H2("Texts and email", cls="text-lg font-semibold mt-4"),
        P(
            "We text your sign-in code, a note when a nonprofit says yes, a reminder the "
            "day before a shift and a note about a new chat message. Reply STOP to end texts. "
            "We email coordinators their sign-in link and a note when volunteers like an opening."
        ),
        H2("How long we keep it", cls="text-lg font-semibold mt-4"),
        P(
            "We keep your account while you use Causematch. Chat messages stay for the "
            "nonprofit's record unless you delete your account."
        ),
        H2("Deleting your account", cls="text-lg font-semibold mt-4"),
        P(
            "Volunteers can delete their account any time from Profile. Your name, phone, "
            "email and location are erased, your matches are withdrawn and your messages are "
            'replaced with "[message removed]". Coordinators ask us to delete theirs.'
        ),
        H2("Questions", cls="text-lg font-semibold mt-4"),
        P(
            "Write to ",
            A(contact_email(), href=f"mailto:{contact_email()}", cls=AT.muted),
            ".",
        ),
        P(A("Back", href="/", cls=AT.muted), cls="pt-8"),
        cls="max-w-md pb-8",
    )
