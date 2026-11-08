import logging

from fasthtml.common import H2, A, Div, Form, HttpHeader, P, Redirect
from monsterui.all import TextPresets
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app import data, notify, oauth, security
from app.auth import sign_in
from app.config import DEMO_PHONE, demo_matches, is_preview
from app.forms import EMAIL, bad_request, query, str_form
from app.ui import (
    BigButton,
    CauseChip,
    DemoSignIn,
    Field,
    Refusal,
    Router,
    SignInPage,
    TextInput,
)

ar = Router()
log = logging.getLogger("causematch")
# The demo seed's demo_volunteer, set by main.py (no route imports the demo seed): with
# config.demo_matches on, "Try it as a volunteer" signs in as a fresh copy of it
demo_volunteer = None

INTRO = "Causematch connects you with Los Angeles nonprofits that need a hand."
SUSPENDED = "This account can't sign in. Contact us if that is a mistake."
# Me's add-a-number on a preview: the fake code goes to the demo's fictional numbers only
# (config.demo_number)
PREVIEW_PHONES = "This preview uses demo numbers only, like (310) 555-0100."
# The same answer for every address: it never says whether an account holds it
SENT = "Check your email: if we can sign you in or up with that address, a link is on its way."
BAD_EMAIL = "Enter your email address."
BAD_LINK = "That sign-in link has expired or was already used. Ask for a new one."
TOKEN_KEY = "volunteer_token"  # the link's token, between its GET and the button's POST


def ProviderSignIn(for_org=False):
    """Continue with Apple, then Google: each when a provider serves it (app/oauth.py
    enabled_providers), then the emailed link's heading. for_org: the nonprofit sign-in's,
    for coordinators and admins."""
    providers = oauth.enabled_providers()
    if not providers:
        return None
    return Div(
        *[
            A(
                f"Continue with {label}",
                href=f"/auth/{name}/start" + ("?for=org" if for_org else ""),
                cls="uk-btn uk-btn-primary w-full h-12 text-lg shadow-warm",
            )
            for name, label in providers
        ],
        H2("Or get an emailed link", cls="pt-6 font-bold text-ink"),
        cls="flex flex-col gap-3",  # one button per line, apart: they never run together
    )


def LoginPage(email="", error=None, notice=None, link=None):
    """/login: the demo's buttons, Google and Apple when configured, then the emailed
    link, which signs a volunteer in or, for a new address, up."""
    return SignInPage(
        "Sign in",
        DemoSignIn(),
        ProviderSignIn(),
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
                    placeholder="you@example.com",
                    aria_label="Email",
                    required=True,
                ),
            ),
            BigButton("Email me a link"),
            P(
                "We email you a link to sign in, or to sign up if you're new. "
                "No password.",
                cls=TextPresets.muted_sm,
            ),
            action="/login/send",
            method="post",
            cls="space-y-4",
        ),
        error=error,
        intro=notice or INTRO,
        aside=Div(
            Div(
                *[CauseChip(c.slug, c.label) for c in data.active_causes()],
                cls="flex flex-wrap justify-center gap-2",
            ),
            P(
                "Signing in for a nonprofit? ",
                A("Nonprofit sign in", href="/org/login", cls="text-brand font-bold"),
                cls="text-ink-muted",
            ),
            cls="mt-8 space-y-6 text-center",
        ),
    )


@ar("/login", methods=["GET"])
def login_page():
    return LoginPage()


def _send_link(email):
    """After the response, so every address takes the same time to answer
    (data.volunteer_link_kind decides what is sent). (link, whether a preview may show
    it: not an admin's), or (None, False) when nothing was sent."""
    kind = data.volunteer_link_kind(email)
    try:
        if kind == "staff":  # the nonprofit sign-in's own link, as /org/login sends
            profile, is_coordinator = data.staff_by_email(email)
            token = data.create_login_token(profile.id)
            return notify.send_login_link(email, token), is_coordinator
        if kind:
            token = data.create_email_token(email)
            return notify.send_volunteer_link(email, token, kind == "sign-up"), True
    except Exception as e:  # noqa: BLE001  nothing here may reach the user: it would reveal the account
        log.warning("sending a sign-in link failed: %s", type(e).__name__)
    return None, False


@ar("/login/send", methods=["POST"])
async def send(req):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_send, req, form)


def _sync_send(req, form):
    raw = form.get("email", "").strip()
    # not ASCII is refused before lowercasing, which would turn the Kelvin sign into a "k"
    if len(raw) > 254 or not raw.isascii() or not EMAIL.fullmatch(raw):
        return LoginPage(raw[:254], BAD_EMAIL)
    email = raw.lower()
    # every valid-shape address is counted, in the nonprofit link's own buckets, so a
    # refusal says nothing about the account and the two pages share one budget
    if not security.allow(
        (security.LINK_PER_EMAIL, email),
        (security.LINK_PER_IP, security.ip_subject(req)),
    ):
        return security.too_many()
    if is_preview() and isinstance(notify.get_emailer(), notify.FakeEmailer):
        # As /org/login on a preview (org_auth.link_sent): nothing is emailed, so the page
        # shows the link, for the demo's data only. An admin's stays in the log.
        link, shown = _send_link(email)
        return LoginPage(notice=SENT, link=link if shown else None)
    return LoginPage(notice=SENT), BackgroundTask(_send_link, email)


@ar("/login/verify", methods=["GET"])
def confirm(req, sess):
    """Consumes nothing, so a mail scanner that fetches the link can't use it up: the
    token moves into the session and the browser comes back here without it, as on
    /org/login/verify (see org_auth.org_login_confirm for the Referrer-Policy)."""
    token = query(req, "token")
    if token:
        sess[TOKEN_KEY] = token[:200]
        return Redirect("/login/verify")
    return HttpHeader("Referrer-Policy", "same-origin"), SignInPage(
        "Sign in to Causematch",
        Form(BigButton("Sign in"), action="/login/verify", method="post"),
        intro="Press the button to finish signing in.",
    )


@ar("/login/verify", methods=["POST"])
async def verify(req, sess):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_verify, form, sess)


def _sync_verify(form, sess):
    """Uses the token up, then signs in the volunteer who signed up with its address, or
    creates them (data.volunteer_for_identity, as a first Google sign-in does): a deleted
    account's suspension and org blocks on the address come back."""
    token = sess.pop(TOKEN_KEY, "") or form.get("token", "")
    email = data.consume_email_token(token) if 0 < len(token) <= 200 else None
    # checked again: the address may have become staff's, or suspended, since it was sent
    if not email or data.volunteer_link_kind(email) not in ("sign-in", "sign-up"):
        return LoginPage(error=BAD_LINK)
    claims = oauth.Claims(
        provider=data.EMAIL_SIGN_IN,
        subject=email,
        email=email,
        email_verified=True,  # the link reached this mailbox
        given_name="",
        family_name="",
        picture_url="",
    )
    profile = data.get_profile(data.volunteer_for_identity(claims))
    if profile is None or profile.role != "volunteer":  # suspended by an admin
        sess.clear()
        return LoginPage(error=SUSPENDED)
    return sign_in(sess, profile)


@ar("/login/demo", methods=["POST"])
def demo_sign_in(req, sess):
    """ "Try it as a volunteer" (config.demo_matches only; production answers 403): a fresh
    copy of the demo volunteer. Counted against the address's sign-in link limit, as the
    nonprofit's demo is, so it can't make copies in a loop."""
    if not (demo_matches() and demo_volunteer):
        return Refusal(403)
    if not security.allow(
        (security.DEMO_PER_IP, security.ip_subject(req)),
        (security.LINK_PER_IP, security.ip_subject(req)),
    ):
        return security.too_many()
    return sign_in(sess, data.get_profile(demo_volunteer(DEMO_PHONE)))


@ar("/logout", methods=["POST"])
def logout(sess):
    profile = data.get_profile(sess["uid"]) if sess.get("uid") else None
    if profile:
        data.sign_out_everywhere(profile.id)  # open chat sockets and other tabs end too
    sess.clear()
    return Redirect(
        "/org/login"
        if profile and profile.role in ("coordinator", "admin")
        else "/login"
    )
