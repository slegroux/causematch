"""Report and block from both sides, account deletion, and the privacy page."""

from fasthtml.common import (
    H1,
    H2,
    A,
    APIRouter,
    Div,
    Li,
    P,
    Redirect,
    Response,
    Textarea,
    Ul,
)
from fasthtml.common import Form as PlainForm
from monsterui.all import AT, Button, ButtonT, Container, LabelCheckboxX, TextPresets
from starlette.concurrency import run_in_threadpool

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


async def _submit(req, match_id, report, block, done, page):
    """Read the form, then run every data call off the event loop. `done` is where a saved
    report goes; `page(uid, match_id, error, values)` redraws the form."""
    form = await str_form(req)
    if form is None:
        return bad_request()
    f = {
        "reason": form.get("reason", ""),
        "details": form.get("details", ""),
        "also_block": form.get("also_block"),
    }
    return await run_in_threadpool(
        _submit_sync, req.scope["uid"], match_id, f, report, block, done, page
    )


def _submit_sync(uid, match_id, f, report, block, done, page):
    try:
        report(uid, match_id, f["reason"], f["details"])
        if f["also_block"] == "1":
            block(uid, match_id)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    except ValueError as e:
        error = str(e).capitalize() if str(e).startswith("you can") else BAD_REPORT
        try:
            return page(uid, match_id, error, f)
        except PermissionError:
            return Response("Forbidden", status_code=403)
    return Redirect(done)


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
    return await _submit(
        req,
        match_id,
        data.report_org,
        data.block_org,
        "/matches?notice=reported",
        lambda uid, mid, error, f: ReportPage(
            f"/matches/{mid}/report", "/matches", _org_of(uid, mid), error, f
        ),
    )


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
    return await _submit(
        req,
        match_id,
        data.report_volunteer,
        data.block_volunteer,
        "/org/inbox?notice=reported",
        lambda uid, mid, error, f: ReportPage(
            f"/org/matches/{mid}/report",
            "/org/inbox",
            _volunteer_of(uid, mid),
            error,
            f,
        ),
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

EFFECTIVE = "October 15, 2026"


def Section(title, *paras):
    return Div(
        H2(title, cls="text-lg font-semibold mt-6"),
        *[P(p, cls="mt-2") for p in paras],
    )


@ar("/privacy", methods=["GET"])
def privacy():
    contact = contact_email()
    return Container(
        H1("Privacy", cls="text-2xl font-bold my-4"),
        P("Draft, pending legal review.", cls="font-semibold text-red-600"),
        P(f"Effective {EFFECTIVE}.", cls=TextPresets.muted_sm),
        Section(
            "What we collect",
            "Volunteers: your mobile number; your first name; an email if you give one; "
            "your ZIP code (we turn it into an approximate location we never show), your "
            "neighborhood and how far you will travel; the causes and skills you pick, "
            "other causes you type, languages other than English and whether you "
            "volunteered last year; when you are free and how often you'd like to help; "
            "how you heard of Causematch; and your confirmation that you are 18 or over.",
            "Activity: the openings you like or pass, your matches, your chat messages, "
            "and your shifts, including whether a nonprofit marked you as attended or as "
            "a no-show.",
            "Nonprofits: a coordinator's email, name and optional phone, and the openings "
            "they post.",
            "Reports and blocks: if you report someone or block them, we keep what you "
            "wrote and who it was about.",
            "Your browser keeps one cookie that signs you in. We use no advertising or "
            "analytics cookies.",
        ),
        Section(
            "Who sees what",
            SHARED_WITH_NONPROFIT,
            "Your chat messages are seen by you and by the coordinators of the nonprofit "
            "you matched with. Causematch staff can see reports, including what the "
            "reporter wrote, and the pilot's aggregate statistics.",
        ),
        Section(
            "Who else handles your data",
            "Twilio sends your sign-in codes and our texts. Our email provider (Resend) "
            "sends coordinators' sign-in links and notices. Google Cloud hosts the app and "
            "keeps its backups. Each sees only what it needs to do that job. We don't sell "
            "your data or share it for advertising.",
        ),
        Section(
            "Texts and email",
            "We text your sign-in code, a note when a nonprofit says yes, a reminder the "
            "day before a shift and a note when a nonprofit sends a chat message. Reply "
            "STOP to end texts. We email coordinators their sign-in link and a note when "
            "volunteers like an opening.",
        ),
        Section(
            "How long we keep it, and what stays after you delete",
            "We keep your account until you delete it. Volunteers can delete theirs any "
            "time from Profile; coordinators ask us to delete theirs. Deleting erases your "
            "name, phone, email, location, neighborhood and the free text you typed. Your "
            "matches are withdrawn and your future shifts cancelled, and your chat messages "
            'are replaced with "[message removed]".',
            "What remains, without your name: your answers (causes, skills, availability, "
            "how often, how you heard of us), your likes and your match and shift history "
            "including attendance, kept for the pilot's statistics. The nonprofit keeps its "
            "own messages to you. Reports you filed stay with the details you wrote, and "
            "reports about you stay.",
            "If you were blocked or reported, or suspended, when you delete your account, "
            "we also keep a one-way keyed hash of your phone number (not the number). If "
            "the same number signs up again, the blocks still apply, and a suspension "
            "stays in force. We keep nothing of this kind for an account with no blocks "
            "or reports.",
        ),
        Section(
            "Seeing or correcting your data",
            f"You can't edit your answers in the app yet. Write to {contact} and we'll "
            "send you what we hold or fix it, or delete your account and sign up again.",
        ),
        Section(
            "Do Not Track",
            "We don't track you across other sites or services, so we have nothing to "
            "change when your browser sends a Do Not Track signal.",
        ),
        Div(
            H2("Questions", cls="text-lg font-semibold mt-6"),
            P(
                "Write to ",
                A(contact, href=f"mailto:{contact}", cls=AT.muted),
                ".",
                cls="mt-2",
            ),
        ),
        P(A("Back", href="/", cls=AT.muted), cls="pt-8"),
        cls="max-w-md pb-8",
    )
