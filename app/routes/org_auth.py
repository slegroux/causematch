import logging

from fasthtml.common import A, Form, HttpHeader, P, Redirect, Response
from monsterui.all import TextPresets
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app import data, notify, security
from app.auth import sign_in_staff
from app.config import DEMO_COORDINATOR, demo_matches, is_preview
from app.forms import EMAIL, query, str_form
from app.routes.auth import ProviderSignIn
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
BAD_EMAIL = "Enter your email address."
BAD_LINK = "That sign-in link has expired or was already used. Ask for a new one."


def EmailForm(email="", error=None, notice=None, link=None):
    return SignInPage(
        "Nonprofit sign in",
        DemoSignIn(volunteer=False),
        ProviderSignIn(for_org=True),
        P(
            A("Open your sign-in link", href=link, cls="text-brand font-bold"),
            " (a preview sends no email)",
        )
        if link
        else None,
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
            A("Sign in with your phone", href="/login", cls="text-brand font-bold"),
            cls="mt-8 text-center text-ink-muted",
        ),
    )


@ar("/org/login", methods=["GET"])
def org_login_page():
    return EmailForm()


def _send_link(email):
    """After the response, so known and unknown addresses take the same time to answer.
    The link, or None."""
    profile, _ = data.staff_by_email(email)
    if profile is None:  # unknown, deleted or suspended
        return
    try:
        return notify.send_login_link(email, data.create_login_token(profile.id))
    except Exception as e:  # noqa: BLE001  nothing here may reach the user: it would reveal the account
        log.warning("sending a sign-in link failed: %s", type(e).__name__)


@ar("/org/login/send", methods=["POST"])
async def org_login_send(req):
    form = await str_form(req)
    if form is None:
        return Response("Bad request", status_code=400)
    return await run_in_threadpool(_sync_org_login_send, req, form)


def _sync_org_login_send(req, form):
    email = form.get("email", "").strip().lower()
    if len(email) > 254 or not EMAIL.fullmatch(email):
        return EmailForm(email[:254], BAD_EMAIL)
    return link_sent(req, email)


def link_sent(req, email):
    """What a request for a sign-in link to this (lowercased) address answers: the
    "check your email" page, the link sent after the response, under the link limits. Also
    a staff provider sign-in's answer when the provider doesn't host the mailbox
    (app/routes/oauth.py). A page, a (page, BackgroundTask), or the 429 response."""
    # every valid-shape address is counted, so a refusal says nothing about the account
    if not security.allow(
        (security.LINK_PER_EMAIL, email),
        (security.LINK_PER_IP, security.ip_subject(req)),
    ):
        return security.too_many()
    if is_preview() and isinstance(notify.get_emailer(), notify.FakeEmailer):
        # Nothing is emailed, so the page shows a coordinator's link: anyone with the
        # preview password may sign in as any coordinator, of the demo's data only. An
        # admin's link stays in the log.
        uid = data.coordinator_by_email(email)
        if uid and data.coordinator_org_ids(uid):
            return EmailForm(notice=SENT, link=_send_link(email))
    return EmailForm(notice=SENT), BackgroundTask(_send_link, email)


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


@ar("/org/login/verify", methods=["GET"])
def org_login_confirm(req, sess):
    """Consumes nothing, so a mail scanner that fetches the link can't use it up.

    The emailed link's token moves into the session and the browser is sent to the same
    path without it, so the page and its assets' Referers never carry it; the button's POST
    reads it from the session. Only the link's own request line still shows it. The policy
    is same-origin, not no-referrer: under no-referrer a browser sends `Origin: null` with
    the button's POST, which security.Guard refuses."""
    token = query(req, "token")
    if token:
        sess["login_token"] = token[:200]
        return Redirect("/org/login/verify")
    return HttpHeader("Referrer-Policy", "same-origin"), SignInPage(
        "Sign in to Causematch",
        Form(BigButton("Sign in"), action="/org/login/verify", method="post"),
        intro="Press the button to finish signing in.",
    )


@ar("/org/login/verify", methods=["POST"])
async def org_login_verify(req, sess):
    form = await str_form(req)
    if form is None:
        return Response("Bad request", status_code=400)
    return await run_in_threadpool(_sync_org_login_verify, form, sess)


def _sync_org_login_verify(form, sess):
    token = sess.pop("login_token", "") or form.get("token", "")
    uid = data.consume_login_token(token) if 0 < len(token) <= 200 else None
    profile = data.get_profile(uid) if uid else None
    is_coordinator = bool(
        profile and profile.role == "coordinator" and data.coordinator_org_ids(uid)
    )
    if not (is_coordinator or (profile and profile.role == "admin")):
        return EmailForm(error=BAD_LINK)
    return sign_in_staff(sess, profile, is_coordinator)
