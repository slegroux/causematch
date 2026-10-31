import logging
import re

import httpx
from fasthtml.common import H2, A, Div, Form, P, Redirect
from monsterui.all import AT, TextPresets
from starlette.concurrency import run_in_threadpool

from app import data, notify, oauth, security
from app.auth import BAD_PHONE, normalize_phone, pending_phone, set_pending, sign_in
from app.config import DEMO_PHONE, demo_matches
from app.forms import bad_request, str_form
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
# config.demo_matches on, the demo number signs in as a fresh copy of the demo volunteer
demo_volunteer = None

INTRO = "Causematch connects you with Los Angeles nonprofits that need a hand."
SUSPENDED = "This account can't sign in. Contact us if that is a mistake."


def ProviderSignIn(for_org=False):
    """Continue with Apple, then Google: each when a provider serves it (app/oauth.py
    enabled_providers). for_org: the nonprofit sign-in's, for coordinators and admins."""
    providers = oauth.enabled_providers()
    if not providers:
        return None
    if for_org:
        then = H2("Or get an emailed link", cls="pt-6 font-bold text-ink")
    elif notify.phone_sign_in_enabled():  # goes with PhoneForm's phone form
        then = H2("Or use your phone number", cls="pt-6 font-bold text-ink")
    else:
        then = None
    return Div(
        *[
            A(
                f"Continue with {label}",
                href=f"/auth/{name}/start" + ("?for=org" if for_org else ""),
                cls="uk-btn uk-btn-primary w-full h-12 text-lg shadow-warm",
            )
            for name, label in providers
        ],
        then,
        cls="flex flex-col gap-3",  # one button per line, apart: they never run together
    )


def PhoneForm(phone="", error=None):
    return SignInPage(
        "Sign in",
        DemoSignIn(),
        ProviderSignIn(),
        Form(
            Field(
                "Mobile number",
                TextInput(
                    name="phone",
                    value=phone,
                    type="tel",
                    inputmode="tel",
                    autocomplete="tel",
                    placeholder="(310) 555-0123",
                    aria_label="Mobile number",
                    required=True,
                ),
            ),
            BigButton("Text me a code"),
            P("We text you a 6-digit code. No password.", cls=TextPresets.muted_sm),
            action="/login/send",
            method="post",
            cls="space-y-4",
        )
        if notify.phone_sign_in_enabled()  # off while Twilio is unset
        else None,
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


def CodeForm(phone, error=None):
    return SignInPage(
        "Enter your code",
        Form(
            Field(
                "Code",
                TextInput(
                    name="code",
                    inputmode="numeric",
                    autocomplete="one-time-code",
                    maxlength="6",
                    aria_label="Code",
                    required=True,
                    cls="h-14 text-center text-2xl font-bold tracking-[0.4em]",
                ),
            ),
            BigButton("Sign in"),
            action="/login/verify",
            method="post",
            cls="space-y-4",
        ),
        P("Wrong number? ", A("Start over", href="/login", cls=AT.muted)),
        error=error,
        intro=f"We texted a code to {phone}.",
    )


def phone_off():
    """The phone-code routes' answer while Twilio is unset (notify.phone_sign_in_enabled)."""
    return security.page(
        404, "Not found", "That page isn't here. Go back and try again."
    )


@ar("/login", methods=["GET"])
def login_page(sess):
    pending_phone(sess)  # an expired number leaves the cookie
    return PhoneForm()


@ar("/login/send", methods=["POST"])
async def send(req, sess):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_send, req, form, sess)


def _sync_send(req, form, sess):
    if not notify.phone_sign_in_enabled():
        return phone_off()
    phone = form.get("phone", "")
    e164 = normalize_phone(phone)
    if not e164:
        return PhoneForm(phone, BAD_PHONE)
    # before the suspended check, so a refusal looks the same for every number
    if not security.allow(*security.sms_send_limits(req, e164)):
        return security.too_many()
    try:
        if not data.phone_suspended(e164):  # a suspended number costs no text
            notify.send_code(e164)
    except httpx.HTTPError as e:
        log.warning("sending a sign-in code failed: %s", type(e).__name__)
        return PhoneForm(
            phone, "We couldn't send a text just now. Try again in a minute."
        )
    sess.clear()
    set_pending(sess, e164)
    return CodeForm(e164)


@ar("/login/verify", methods=["POST"])
async def verify(req, sess):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_verify, req, form, sess)


def _sync_verify(req, form, sess):
    if not notify.phone_sign_in_enabled():
        return phone_off()
    e164 = pending_phone(sess)  # only the number a code was sent to, for 15 minutes
    if not e164:
        return PhoneForm(error="Enter your number to get a code.")
    code = form.get("code", "").strip()
    if not re.fullmatch(r"[0-9]{6}", code):
        return CodeForm(e164, "Enter the 6-digit code from the text.")
    # only wrong codes count, so the right one always gets through below the limit
    if not security.allow(*security.verify_limits(e164), record=False):
        return security.too_many()
    try:
        ok = notify.check_code(e164, code)
    except httpx.HTTPError as e:
        log.warning("checking a sign-in code failed: %s", type(e).__name__)
        return CodeForm(e164, "We couldn't check that code just now. Try again.")
    if not ok:
        security.allow(*security.verify_limits(e164))
        return CodeForm(e164, "That code didn't work. Check the text and try again.")
    uid = None
    if demo_matches() and demo_volunteer and e164 == DEMO_PHONE:
        if not security.allow((security.DEMO_PER_IP, security.ip_subject(req))):
            return security.too_many()  # a fresh copy each time: 10 an hour per address
        uid = demo_volunteer(e164)
    uid = uid or data.get_or_create_volunteer(e164)
    profile = data.get_profile(uid)
    sess.clear()  # the code is used: the number leaves the session either way
    if profile is None:  # suspended by an admin
        return PhoneForm(
            error="This number can't sign in. Contact us if that is a mistake."
        )
    return sign_in(sess, profile)


@ar("/login/demo", methods=["POST"])
def demo_sign_in(req, sess):
    """ "Try it as a volunteer" (config.demo_matches only; production answers 403): a fresh
    copy of the demo volunteer, as signing in with the demo number gives. Counted against
    the address's sign-in limits, so it can't make copies in a loop."""
    if not (demo_matches() and demo_volunteer):
        return Refusal(403)
    if not security.allow(
        (security.DEMO_PER_IP, security.ip_subject(req)),
        (security.SMS_PER_IP, security.ip_subject(req)),
        (security.SMS_PER_NET, security.ip_subject(req, 48)),
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
