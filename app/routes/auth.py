import hashlib
import hmac
import logging
import time

from fasthtml.common import H2, A, Button, Div, Form, Hidden, HttpHeader, P, Redirect
from monsterui.all import Alert, AlertT, TextPresets
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app import data, notify, oauth, security
from app.auth import sign_in
from app.config import (
    DEMO_PHONE,
    demo_address,
    demo_matches,
    is_preview,
    phone_hash_key,
)
from app.forms import EMAIL, abuse_key, bad_request, query, str_form
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
SENT = "If we can sign you in or up with that address, a link is on its way."
BAD_EMAIL = "Enter your email address."
BAD_LINK = "That sign-in link has expired or was already used. Ask for a new one."
TOKEN_KEY = "volunteer_token"  # the link's token, between its GET and the button's POST
# The mailbox this browser last asked for a link to (both pages), as [keyed digest, unix
# time]: the session is signed, not encrypted, so the address itself is never kept in it
LINK_FOR = "link_for"
NOT_ASKED = (
    "This browser didn't ask for this link. If someone sent it to you, stop here: "
    "continuing would sign you in to their account."
)
SIGNED_IN = "This browser is signed in to another account. Continuing signs it out."


def _mailbox_digest(email):
    key = phone_hash_key().encode()
    return hmac.new(key, abuse_key(email).encode(), hashlib.sha256).hexdigest()[:32]


def asked_for(sess, email):
    """Remember that this browser asked for a link to this address (ConfirmPage), as a
    keyed digest of its mailbox and when."""
    sess[LINK_FOR] = [_mailbox_digest(email), int(time.time())]


def _asked(sess, email):
    """Whether this browser asked for a link to this address within a link's lifetime."""
    found = sess.get(LINK_FOR)
    if not (isinstance(found, list) and len(found) == 2):
        return False
    digest, at = found
    fresh = isinstance(at, int) and 0 <= time.time() - at <= data.TOKEN_MINUTES * 60
    return fresh and hmac.compare_digest(str(digest), _mailbox_digest(email))


def ConfirmPage(action, email, sess, owner):
    """An emailed link's confirm page (both sign-ins): the account it signs in to by
    address, and a warning when this browser didn't ask for the link or is signed in as
    someone else, since pressing the button on a link someone sent you would sign you in
    as them (login CSRF). owner: the profile id the link signs in to, None for a new one.
    Referrer-Policy same-origin, not no-referrer: under no-referrer a browser sends
    `Origin: null` with the button's POST, which security.Guard refuses."""
    warnings = [
        None if _asked(sess, email) else NOT_ASKED,
        SIGNED_IN if sess.get("uid") and sess.get("uid") != owner else None,
    ]
    return HttpHeader("Referrer-Policy", "same-origin"), SignInPage(
        "Sign in to Causematch",
        *[Alert(w, cls=AlertT.warning) for w in warnings if w],
        Form(BigButton("Sign in"), action=action, method="post"),
        intro=f"Sign in as {email}",
    )


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


def LoginPage(email="", error=None):
    """/login: the demo's buttons, Google and Apple when configured, then the emailed
    link, which signs a volunteer in or, for a new address, up."""
    return SignInPage(
        "Sign in",
        DemoSignIn(),
        ProviderSignIn(),
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
        intro=INTRO,
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


def SentPage(email, link=None):
    """ "Check your email", after the form (the same page for every address, so it never
    says whether an account holds it): the address it went to, a button that sends the link
    again to it (the same route, so the same limits), and the way back to the form."""
    return SignInPage(
        "Check your email",
        P(email, cls="break-all text-center text-lg font-bold text-ink"),
        P(
            A("Open your sign-in link", href=link, cls="text-brand font-bold"),
            " (a preview sends no email)",
        )
        if link
        else None,
        Form(
            Hidden(name="email", value=email),
            Button(
                "Send it again",
                type="submit",
                cls="uk-btn uk-btn-default w-full h-12 text-lg",
            ),
            action="/login/send",
            method="post",
        ),
        P(
            A("Use another address", href="/login", cls="text-brand font-bold"),
            cls="text-center",
        ),
        P(
            f"The link works once and expires in {data.TOKEN_MINUTES} minutes.",
            cls=TextPresets.muted_sm + " text-center",
        ),
        intro=SENT,
    )


@ar("/login", methods=["GET"])
def login_page():
    return LoginPage()


def _send_link(email):
    """After the response, so every address takes the same time to answer
    (data.volunteer_link_kind decides what is sent). (link, whether a preview may show
    it: not an admin's), or (None, False) when nothing was sent."""
    kind = data.volunteer_link_kind(email)
    if kind == "sign-up" and not security.allow((security.SIGNUP_ALL, "all")):
        return None, False  # past the app-wide ceiling (logged by name): nothing sent
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
async def send(req, sess):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_send, req, form, sess)


def _sync_send(req, form, sess):
    raw = form.get("email", "").strip()
    # not ASCII is refused before lowercasing, which would turn the Kelvin sign into a "k"
    if len(raw) > 254 or not raw.isascii() or not EMAIL.fullmatch(raw):
        return LoginPage(raw[:254], BAD_EMAIL)
    return link_sent(req, raw.lower(), sess)


def link_sent(req, email, sess):
    """What a request for a volunteer's link to this (lowercased, valid) address answers:
    the "check your email" page, the link sent after the response, under the link limits.
    Also a volunteer's Google or Apple sign-in's answer when its address is staff's
    (app/routes/oauth.py). A page, a (page, BackgroundTask), or the 429 response."""
    # every valid-shape address is counted, in the nonprofit link's own buckets, so a
    # refusal says nothing about the account and the two pages share one budget
    smtp = isinstance(notify.get_emailer(), notify.SMTPEmailer)
    if not security.allow(*security.link_limits(req, email, smtp)):
        return security.too_many()
    if is_preview() and not demo_address(email):
        # A preview is demo-only, as /org/login there (org_auth.link_sent): any other
        # address gets the same answer and nothing is sent, shown or logged, so a real
        # address neither signs up there nor gets a link
        return SentPage(email)
    asked_for(sess, email)
    if is_preview() and isinstance(notify.get_emailer(), notify.FakeEmailer):
        # As /org/login on a preview (org_auth.link_sent): nothing is emailed, so the page
        # shows the link, for the demo's data only. An admin's stays in the log.
        link, shown = _send_link(email)
        return SentPage(email, link if shown else None)
    return SentPage(email), BackgroundTask(_send_link, email)


@ar("/login/verify", methods=["GET"])
def confirm(req, sess):
    """Consumes nothing, so a mail scanner that fetches the link can't use it up: the
    token moves into the session and the browser comes back here without it, as on
    /org/login/verify, to ConfirmPage, which only peeks at it."""
    token = query(req, "token")
    if token:
        sess[TOKEN_KEY] = token[:200]
        return Redirect("/login/verify")
    _, email = data.peek_login_token(sess.get(TOKEN_KEY, ""))
    if not email:
        return HttpHeader("Referrer-Policy", "same-origin"), LoginPage(error=BAD_LINK)
    owner = data.email_sign_in_owner(email)
    return ConfirmPage("/login/verify", email, sess, owner)


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
