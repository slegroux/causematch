"""Report and block from both sides, account deletion, and the privacy page."""

from fasthtml.common import (
    H1,
    H2,
    A,
    Div,
    Li,
    P,
    Redirect,
    Response,
    Span,
    Textarea,
    Ul,
)
from fasthtml.common import Button as PlainButton
from fasthtml.common import Form as PlainForm
from monsterui.all import (
    AT,
    Alert,
    AlertT,
    Button,
    ButtonT,
    LabelCheckboxX,
    TextPresets,
)
from starlette.concurrency import run_in_threadpool

from app import data
from app.choices import SHARED_WITH_NONPROFIT
from app.config import contact_email
from app.forms import bad_request, str_form
from app.ui import (
    CARD,
    CLOCK,
    HEART,
    NEXT,
    ORG_SECTION,
    ORG_TITLE,
    PERSON,
    SHIELD,
    SIGN_OUT,
    SMALL_LINK,
    TITLE,
    Avatar,
    Choice,
    Field,
    Icon,
    OrgShell,
    Refusal,
    Router,
    SimplePage,
    VolunteerShell,
)

ar = Router()

BAD_REPORT = "Choose a reason. Details can be up to 1000 characters."


def Shell(uid, back, title, *content, error=None):
    """A report or block page in the side's own shell: the nonprofit's top nav when it
    goes back to the inbox, else the volunteer's tab bar."""
    org = back.startswith("/org")
    body = (
        H1(title, cls=(ORG_TITLE + " pb-4") if org else TITLE),
        Alert(error, cls=AlertT.error + " mb-4") if error else None,
        Div(*content, cls="space-y-3 p-5 " + CARD),
    )
    if org:
        return OrgShell("inbox", data.org_waiting(uid), Div(*body, cls="max-w-md"))
    return VolunteerShell("matches", *body, uid=uid)


def ReportPage(uid, action, back, target, error=None, f=None):
    f = f or {}
    return Shell(
        uid,
        back,
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
            A("Cancel", href=back, cls=SMALL_LINK),
            action=action,
            method="post",
            cls="space-y-3",
        ),
        error=error,
    )


def BlockPage(uid, action, back, title, effects):
    return Shell(
        uid,
        back,
        title,
        Ul(*[Li(e) for e in effects], cls="uk-list uk-list-disc"),
        PlainForm(
            Button("Block", cls=ButtonT.destructive),
            A("Cancel", href=back, cls=SMALL_LINK),
            action=action,
            method="post",
            cls="flex items-center gap-4 pt-2",
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
        return Refusal(403)
    except ValueError as e:
        error = str(e).capitalize() if str(e).startswith("you can") else BAD_REPORT
        try:
            return page(uid, match_id, error, f)
        except PermissionError:
            return Refusal(403)
    return Redirect(done)


# --- volunteer side --------------------------------------------------------------------------


def _org_of(uid, match_id):
    return data.my_match(uid, match_id)["org"]


@ar("/matches/{match_id}/report", methods=["GET"])
def report_org_page(req, match_id: int):
    try:
        org = _org_of(req.scope["uid"], match_id)
    except PermissionError:
        return Refusal(403)
    return ReportPage(req.scope["uid"], f"/matches/{match_id}/report", "/matches", org)


@ar("/matches/{match_id}/report", methods=["POST"])
async def report_org_save(req, match_id: int):
    return await _submit(
        req,
        match_id,
        data.report_org,
        data.block_org,
        "/matches?notice=reported",
        lambda uid, mid, error, f: ReportPage(
            uid, f"/matches/{mid}/report", "/matches", _org_of(uid, mid), error, f
        ),
    )


@ar("/matches/{match_id}/block", methods=["GET"])
def block_org_page(req, match_id: int):
    try:
        org = _org_of(req.scope["uid"], match_id)
    except PermissionError:
        return Refusal(403)
    return BlockPage(
        req.scope["uid"],
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
        return Refusal(403)
    return Redirect("/matches?notice=blocked")


# --- coordinator side ------------------------------------------------------------------------


def _volunteer_of(uid, match_id):
    return data.org_match_volunteer(uid, match_id)


@ar("/org/matches/{match_id}/report", methods=["GET"])
def report_volunteer_page(req, match_id: int):
    try:
        who = _volunteer_of(req.scope["uid"], match_id)
    except PermissionError:
        return Refusal(403)
    return ReportPage(
        req.scope["uid"], f"/org/matches/{match_id}/report", "/org/inbox", who
    )


@ar("/org/matches/{match_id}/report", methods=["POST"])
async def report_volunteer_save(req, match_id: int):
    return await _submit(
        req,
        match_id,
        data.report_volunteer,
        data.block_volunteer,
        "/org/inbox?notice=reported",
        lambda uid, mid, error, f: ReportPage(
            uid,
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
        return Refusal(403)
    return BlockPage(
        req.scope["uid"],
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
        return Refusal(403)
    return Redirect("/org/inbox?notice=blocked")


# --- account deletion ------------------------------------------------------------------------


def HubRow(icon, label, href=None, danger=False, sub=None):
    """One row of the Me hub: a link with a chevron, or (without href) a sign-out
    button, which acts rather than opens a page. sub: a muted line under the label."""
    cls = (
        "flex min-h-14 w-full items-center gap-3 px-4 py-2 text-left font-bold "
        "hover:bg-cream " + ("text-red-700" if danger else "text-ink")
    )
    inner = (
        Span(
            Icon(icon, size=20),
            cls="grid h-9 w-9 shrink-0 place-items-center rounded-full "
            + ("bg-red-50" if danger else "bg-sand"),
        ),
        Span(
            label,
            Span(sub, cls="block text-sm font-normal text-ink-muted") if sub else None,
            cls="flex-1",
        ),
        Icon(NEXT, size=18, cls="text-ink-muted") if href else None,
    )
    if href:
        return A(*inner, href=href, cls=cls)
    return PlainForm(
        PlainButton(*inner, type="submit", cls=cls), action="/logout", method="post"
    )


@ar("/profile", methods=["GET"])
def profile_page(req):
    me = data.get_profile(req.scope["uid"])
    name = (me.display_name or "").strip()
    return VolunteerShell(
        "me",
        H1("Me", cls=TITLE),
        Div(
            Avatar(name, me.photo_key),
            Div(
                H2(name or "Your profile", cls="font-display text-lg font-extrabold"),
            ),
            cls="flex items-center gap-4 p-4 " + CARD,
        ),
        Div(
            HubRow(PERSON, "Your profile", "/me/profile", sub="What nonprofits see"),
            HubRow(
                HEART, "Preferences", "/me/preferences", sub="What you see, and when"
            ),
            HubRow(
                CLOCK,
                "Shifts and hours",
                "/history",
                sub="Upcoming, past, confirmed hours",
            ),
            HubRow(SHIELD, "Privacy policy", "/privacy"),
            HubRow(SIGN_OUT, "Sign out"),
            cls="mt-4 divide-y divide-line overflow-hidden " + CARD,
        ),
        uid=req.scope["uid"],
    )


@ar("/profile/delete", methods=["GET"])
def delete_confirm(req):
    return VolunteerShell(
        "me",
        H1("Delete your account?", cls=TITLE),
        Div(
            Ul(
                Li("Your name, phone, email and location are erased."),
                Li(
                    "Your pending and accepted matches are withdrawn and shifts cancelled."
                ),
                Li('Your chat messages are replaced with "[message removed]".'),
                Li("You are signed out everywhere. This can't be undone."),
                cls="uk-list uk-list-disc",
            ),
            PlainForm(
                Button("Delete my account", cls=ButtonT.destructive),
                A("Keep it", href="/profile", cls=SMALL_LINK + " ml-4"),
                action="/profile/delete",
                method="post",
                cls="flex items-center pt-2",
            ),
            cls="space-y-3 p-5 " + CARD,
        ),
        uid=req.scope["uid"],
    )


@ar("/profile/delete", methods=["POST"])
def delete_save(req, sess):
    try:
        data.delete_account(req.scope["uid"])
    except PermissionError:
        return Refusal(403)
    sess.clear()
    return Redirect("/login")


# --- privacy ---------------------------------------------------------------------------------

EFFECTIVE = "October 31, 2026"


def Section(title, *paras):
    return Div(
        H2(title, cls=ORG_SECTION + " mt-6"),
        *[P(p, cls="mt-2 leading-relaxed") for p in paras],
    )


@ar("/privacy", methods=["GET"])
def privacy():
    contact = contact_email()
    return SimplePage(
        "Privacy",
        Div(
            P("Draft, pending legal review.", cls="font-bold text-red-700"),
            P(f"Effective {EFFECTIVE}.", cls=TextPresets.muted_sm),
            cls="flex flex-wrap items-center gap-3 pt-2",
        ),
        Section(
            "What we collect",
            "Volunteers: your mobile number, if you sign in with one or add one; your "
            "first name and, if you give them, "
            "your last name, pronouns, a short bio, your interests and a photo; an "
            "email if you give one; the neighborhood you show nonprofits; the other "
            "causes you type and whether you volunteered last year; how you heard of "
            "Causematch; and your confirmation that you are 18 or over.",
            "Your match preferences: up to two places you help from (each a neighborhood "
            "or ZIP area, or your device's location rounded to about 1 km: never an "
            "address), how you get there and how far you'd go; how much you care about "
            "each cause; when you're usually free and specific free dates; how far ahead "
            "to look and one-time or recurring; the kind of work, languages and skills "
            "you pick; how often you'd like to help; which of these only show or only "
            "rank openings; and whether you want last-minute calls, for which shifts, "
            "your quiet hours and how many a week.",
            "Causematch uses your answers (your causes, places, when you're free and the "
            "rest) to match you with openings.",
            "If you sign in with Google or Apple, that company tells us your account's id "
            "there, its email (with Apple, a relay address if you chose to hide yours) and "
            "your name. We keep the id, the email and your first name. Google also sends "
            'a link to your profile picture, and we use the picture only if you tick "Use '
            'my Google photo" when you sign up. We never get your Google or Apple '
            "password. When a coordinator signs in with Google or Apple, we only check "
            "that the email is the one we have on file, and keep nothing from it.",
            "Activity: the openings you like or pass, your matches, your chat messages, "
            "and your shifts, including whether a nonprofit marked you as attended or as "
            "a no-show.",
            "Nonprofits: a coordinator's email, name and optional phone, the openings "
            "they post, and the organization's logo, photos, about text, website and "
            "event links, which anyone signed in can see. Someone who asks to manage a "
            "nonprofit gives the same, plus the "
            "organization's EIN and name; we blank their name, email and phone 90 days "
            "after we approve or reject the request.",
            "Reports and blocks: if you report someone or block them, we keep what you "
            "wrote and who it was about.",
            "Your browser keeps one cookie that signs you in and, for up to 10 minutes "
            "during a Google or Apple sign-in, one that ties their answer to your "
            "browser. We use no advertising or analytics cookies.",
        ),
        Section(
            "Who sees what",
            SHARED_WITH_NONPROFIT,
            "Your chat messages are seen by you and by the coordinators of the nonprofit "
            "you matched with. Causematch staff can see reports, including what the "
            "reporter wrote, and the pilot's aggregate statistics.",
        ),
        Section(
            "What stays private, and what a nonprofit sees",
            "Only you see these: your places and your device's location, how you rated "
            "each cause, the days and times you ticked and your specific dates, how far "
            "ahead you look, one-time or recurring, the kind of work and skills you "
            "picked, which preferences only show or only rank, whether you need your hours "
            "confirmed, Surprise me, and your last-minute call settings. They decide which openings you see and in what "
            "order.",
            'A nonprofit sees only what "Who sees what" lists, and only after you like '
            "one of its openings: your photo, your first name and last initial (all of your "
            "last name once it accepts you), pronouns, bio and interests, the causes you "
            "love or are open to, a general summary of when you're free (weekday or "
            "weekend mornings, afternoons, evenings), the languages you speak, how often "
            "you'd like to help and the neighborhood you show nonprofits (you can "
            "change it under Me, then Your profile).",
        ),
        Section(
            "Hours certificates",
            "When you get a certificate on Shifts and hours, we keep a signed copy of it: your "
            "full name, the nonprofit's name, EIN and website, each confirmed shift (date, "
            "role and hours), the coordinators who confirmed them, the period, the total, "
            "the issue date and a signature code.",
            "Anyone with the certificate's check link or QR code, signed in or not, sees "
            "your full name, the nonprofit and its EIN, the period, the total hours, the "
            "issue date and the signature code, and whether the nonprofit has corrected "
            "the hours since. They don't see your shifts, phone or email. The link doesn't "
            "expire: it works until you withdraw the certificate, or get a new one for the "
            "same nonprofit and period, which withdraws the old one. Coordinators' names appear on "
            "certificates and stay there after a coordinator's account is deleted.",
            "Withdrawing a certificate, or deleting your account, erases the name, the "
            "shifts and the signature, and the check link then says only that it was "
            "withdrawn. What remains, without your name: your account's number, the "
            "nonprofit, the period, the total hours and the issue date.",
        ),
        Section(
            "Who else handles your data",
            "Twilio sends your sign-in codes and our texts. Our email provider (Resend) "
            "sends coordinators' sign-in links and notices, and volunteers' notices when "
            "we can't text them. If you sign in with Google or Apple, that company "
            "confirms who you are, so it knows you use Causematch. Google Cloud hosts the "
            "app and keeps its backups. Each sees only what it needs to do that job. We "
            "don't sell your data or share it for advertising.",
        ),
        Section(
            "Texts and email",
            "We text your sign-in code, a note when a nonprofit says yes, a reminder the "
            "day before a shift, a note when a nonprofit sends a chat message and, only "
            "if you turn them on, last-minute calls (at most 3 a week, never in your "
            "quiet hours). Without a number we can text, these come by email if we have "
            "your email. Reply STOP to end texts. We email coordinators their sign-in "
            "link and a note when volunteers like an opening.",
        ),
        Section(
            "How long we keep it, and what stays after you delete",
            "We keep your account until you delete it. Volunteers can delete theirs any "
            "time from Me; coordinators ask us to delete theirs. Deleting erases your "
            "name, phone, email, connected Google or Apple accounts, places, specific "
            "free dates, neighborhood, photo and the free text you typed. Your "
            "matches are withdrawn and your future shifts cancelled, and your chat messages "
            'are replaced with "[message removed]".',
            "What remains, without your name: your answers (causes and how you rated "
            "them, skills, when you're usually free, how often, the other preferences "
            "above except your places, and how you heard of us), your likes and your "
            "match and shift history "
            "including attendance, kept for the pilot's statistics. The nonprofit keeps its "
            "own messages to you. Reports you filed stay with the details you wrote, and "
            "reports about you stay.",
            "If you were blocked or reported, or suspended, when you delete your account, "
            "we also keep a one-way keyed hash of your phone number (not the number). If "
            "the same number signs up again, the blocks still apply, and a suspension "
            "stays in force. If you had connected Google or Apple, we keep the same kind "
            "of hash of that account's id, in the same cases, so a block or suspension "
            "follows that account too. We keep nothing of this kind for an account with "
            "no blocks or reports.",
        ),
        Section(
            "Seeing or correcting your data",
            "You can change your photo, name, pronouns, bio, interests and the "
            "neighborhood nonprofits see yourself under "
            "Me, then Your profile, and your match preferences under Me, then "
            "Preferences. The Google or Apple accounts you sign in with are listed "
            "under Me, then Your profile, where you can connect or disconnect them, as "
            "long as one way to sign in is left. "
            f"For anything else, write to {contact} and we'll send you what "
            "we hold or fix it, or delete your account and sign up again.",
        ),
        Section(
            "Do Not Track",
            "We don't track you across other sites or services, so we have nothing to "
            "change when your browser sends a Do Not Track signal.",
        ),
        Div(
            H2("Questions", cls=ORG_SECTION + " mt-6"),
            P(
                "Write to ",
                A(contact, href=f"mailto:{contact}", cls=AT.muted),
                ".",
                cls="mt-2",
            ),
        ),
        P(A("Back", href="/", cls=SMALL_LINK), cls="pt-6"),
    )


# --- not found -------------------------------------------------------------------------------


def not_found(req, exc):
    """main.py's 404 handler: the shared look, and a way back. A missing static file
    stays a plain-text 404: no page for a script or a stylesheet to fetch."""
    if req.url.path.startswith("/static/"):
        return Response("Not Found", status_code=404)
    return SimplePage(
        "Page not found",
        P("That page doesn't exist, or it moved.", cls="mt-2 text-ink"),
        P(A("Back to Causematch", href="/", cls=AT.muted), cls="mt-4"),
    )
