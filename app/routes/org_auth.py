import logging

from fasthtml.common import A, Form, HttpHeader, P, Redirect, Response
from monsterui.all import TextPresets
from starlette.concurrency import run_in_threadpool

from app import data, notify, security
from app.auth import sign_in_staff
from app.config import (
    DEMO_ADMIN,
    DEMO_COORDINATOR,
    demo_address,
    demo_matches,
    is_preview,
)
from app.forms import EMAIL, query, str_form
from app.routes.auth import (
    BAD_EMAIL,
    BAD_LINK,
    ConfirmPage,
    ProviderSignIn,
    StagingLink,
    asked_for,
)
from app.ui import (
    BigButton,
    DemoSignIn,
    Field,
    Refusal,
    Router,
    SignInPage,
    TextInput,
)

ar = Router()
log = logging.getLogger("causematch")

SENT = "If that address belongs to a coordinator, a sign-in link is on its way."


def EmailForm(email="", error=None, notice=None, link=None):
    return SignInPage(
        "Nonprofit sign in",
        DemoSignIn(volunteer=False),
        ProviderSignIn(for_org=True),
        StagingLink(link),
        Form(
            Field(
                "Email",
                TextInput(
                    name="email",
                    value=email,
                    type="email",
                    inputmode="email",
                    autocomplete="email",
                    placeholder="you@nonprofit.org",
                    aria_label="Email",
                    required=True,
                ),
            ),
            BigButton("Email me a link"),
            P("We email you a sign-in link. No password.", cls=TextPresets.muted_sm),
            action="/org/login/send",
            method="post",
            cls="space-y-4",
        ),
        error=error,
        intro=notice or "Sign in to post volunteer openings.",
        aside=P(
            "Volunteering? ",
            A("Volunteer sign in", href="/login", cls="text-brand font-bold"),
            cls="mt-8 text-center text-ink-muted",
        ),
    )


@ar("/org/login", methods=["GET"])
def org_login_page():
    return EmailForm()


def _send_link(email):
    """After the response (notify.after), so known and unknown addresses take the same
    time to answer. The link, or None."""
    profile, _ = data.staff_by_email(email)
    if profile is None:  # unknown, deleted or suspended
        return
    try:
        return notify.send_login_link(email, data.create_login_token(profile.id))
    except Exception as e:  # noqa: BLE001  nothing here may reach the user: it would reveal the account
        log.warning("sending a sign-in link failed: %s", type(e).__name__)


@ar("/org/login/send", methods=["POST"])
async def org_login_send(req, sess):
    form = await str_form(req)
    if form is None:
        return Response("Bad request", status_code=400)
    return await run_in_threadpool(_sync_org_login_send, req, form, sess)


def _sync_org_login_send(req, form, sess):
    raw = form.get("email", "").strip()
    # not ASCII is refused before lowercasing, which would turn the Kelvin sign into a "k"
    if len(raw) > 254 or not raw.isascii() or not EMAIL.fullmatch(raw):
        return EmailForm(raw[:254], BAD_EMAIL)
    return link_sent(req, raw.lower(), sess)


def link_sent(req, email, sess):
    """What a request for a sign-in link to this (lowercased) address answers: the
    "check your email" page, the link sent after the response (notify.after), under the
    link limits. Also a staff provider sign-in's answer when the provider doesn't host the
    mailbox (app/routes/oauth.py). A page or the 429 response."""
    # every valid-shape address is counted, so a refusal says nothing about the account
    if not security.allow(*security.link_limits(req, email, notify.smtp_for(email))):
        return security.too_many()
    if is_preview() and not notify.staging_links() and not demo_address(email):
        # A preview with a real emailer for everyone stays demo-only: any other address
        # gets an unknown address's answer and nothing is sent
        return EmailForm(notice=SENT)
    # with SMTP, a preview emails a real address's link (notify.StagingEmailer)
    staging = notify.staging_links() and not notify.real_mail_to(email)
    asked_for(sess, email)
    if staging:
        # Staging: nothing is emailed, so the page shows a coordinator's link, any
        # address's: anyone with the preview password may sign in as any of them. An
        # admin's too while "Try it as an admin" makes anyone an admin (demo_matches);
        # else it stays in the fake emailer. An unknown address gets the notice alone.
        profile, is_coordinator = data.staff_by_email(email)
        if profile and (is_coordinator or demo_matches()):
            return EmailForm(notice=SENT, link=_send_link(email))
    notify.after(req, _send_link, email, limiter=notify.LINK_LIMITER)
    return EmailForm(notice=SENT)


@ar("/org/login/demo", methods=["POST"])
def demo_sign_in(req, sess):
    """ "Try it as a nonprofit" (config.demo_matches only; production answers 403): the
    demo coordinator, with a normal coordinator session. Counted against the address's
    sign-in link limit."""
    if not demo_matches():
        return Refusal(403)
    if not security.allow((security.LINK_PER_IP, security.ip_subject(req))):
        return security.too_many()
    uid = data.coordinator_by_email(DEMO_COORDINATOR)
    profile = data.get_profile(uid) if uid else None
    if not (profile and data.coordinator_org_ids(uid)):
        return Refusal(403)  # a demo without its coordinator
    return sign_in_staff(sess, profile, True)


@ar("/org/login/demo-admin", methods=["POST"])
def demo_admin_sign_in(req, sess):
    """ "Try it as an admin" (config.demo_matches only; production answers 403): the demo
    admin, with a normal admin session, so a presenter can show the admin pages on demo
    data. Counted against the same sign-in link limit as the other demo buttons."""
    if not demo_matches():
        return Refusal(403)
    if not security.allow((security.LINK_PER_IP, security.ip_subject(req))):
        return security.too_many()
    uid = data.admin_by_email(DEMO_ADMIN)
    profile = data.get_profile(uid) if uid else None
    if profile is None:
        return Refusal(403)  # a demo without its admin
    return sign_in_staff(sess, profile, False)


@ar("/org/login/verify", methods=["GET"])
def org_login_confirm(req, sess):
    """Consumes nothing, so a mail scanner that fetches the link can't use it up.

    The emailed link's token moves into the session and the browser is sent to the same
    path without it, so the page and its assets' Referers never carry it; the button's POST
    reads it from the session. Only the link's own request line still shows it. The page
    (auth.ConfirmPage) peeks at the token to name the account it signs in to."""
    token = query(req, "token")
    if token:
        sess["login_token"] = token[:200]
        return Redirect("/org/login/verify")
    uid, _ = data.peek_login_token(sess.get("login_token", ""))
    profile = data.get_profile(uid) if uid else None
    if profile is None or not profile.email:
        return HttpHeader("Referrer-Policy", "same-origin"), EmailForm(error=BAD_LINK)
    return ConfirmPage("/org/login/verify", profile.email, sess, profile.id)


@ar("/org/login/verify", methods=["POST"])
async def org_login_verify(req, sess):
    form = await str_form(req)
    if form is None:
        return Response("Bad request", status_code=400)
    return await run_in_threadpool(_sync_org_login_verify, req, form, sess)


def _sync_org_login_verify(req, form, sess):
    token = sess.pop("login_token", "") or form.get("token", "")
    uid = data.consume_login_token(token) if 0 < len(token) <= 200 else None
    profile = data.get_profile(uid) if uid else None
    is_coordinator = bool(
        profile and profile.role == "coordinator" and data.coordinator_org_ids(uid)
    )
    if not (is_coordinator or (profile and profile.role == "admin")):
        return EmailForm(error=BAD_LINK)
    # the link reached this inbox: on a staging preview its mail goes out from now on
    notify.prove(profile.email, profile.id)
    # the link's sign-in only: not the demo buttons', nor Google's or Apple's
    notify.after(req, notify.send_sign_in_notice, profile.email, notify.now())
    return sign_in_staff(sess, profile, is_coordinator)
