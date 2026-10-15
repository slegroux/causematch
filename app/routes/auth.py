import logging
import re

import httpx
from fasthtml.common import A, APIRouter, Form, Input, P, Redirect
from monsterui.all import AT, Button, ButtonT, TextPresets

from app import data, notify
from app.auth import BAD_PHONE, normalize_phone
from app.forms import bad_request, str_form
from app.ui import Page

ar = APIRouter()
log = logging.getLogger("causematch")

INTRO = "Causematch connects you with Los Angeles nonprofits that need a hand."


def PhoneForm(phone="", error=None):
    return Page(
        "Sign in",
        Form(
            Input(
                name="phone",
                value=phone,
                type="tel",
                inputmode="tel",
                autocomplete="tel",
                placeholder="(310) 555-0123",
                aria_label="Mobile number",
                required=True,
            ),
            Button("Text me a code", cls=ButtonT.primary + " w-full"),
            P("We text you a 6-digit code. No password.", cls=TextPresets.muted_sm),
            action="/login/send",
            method="post",
        ),
        error=error,
        intro=INTRO,
    )


def CodeForm(phone, error=None):
    return Page(
        "Enter your code",
        Form(
            Input(
                name="code",
                inputmode="numeric",
                autocomplete="one-time-code",
                maxlength="6",
                placeholder="6-digit code",
                aria_label="Code",
                required=True,
            ),
            Button("Sign in", cls=ButtonT.primary + " w-full"),
            action="/login/verify",
            method="post",
        ),
        P("Wrong number? ", A("Start over", href="/login", cls=AT.muted)),
        error=error,
        intro=f"We texted a code to {phone}.",
    )


@ar("/login", methods=["GET"])
def login_page():
    return PhoneForm()


@ar("/login/send", methods=["POST"])
async def send(req, sess):
    form = await str_form(req)
    if form is None:
        return bad_request()
    phone = form.get("phone", "")
    e164 = normalize_phone(phone)
    if not e164:
        return PhoneForm(phone, BAD_PHONE)
    try:
        notify.send_code(e164)
    except httpx.HTTPError as e:
        log.warning("sending a sign-in code failed: %s", type(e).__name__)
        return PhoneForm(
            phone, "We couldn't send a text just now. Try again in a minute."
        )
    sess.clear()
    sess["pending_phone"] = e164
    return CodeForm(e164)


@ar("/login/verify", methods=["POST"])
async def verify(req, sess):
    form = await str_form(req)
    if form is None:
        return bad_request()
    e164 = sess.get("pending_phone")  # only the number a code was sent to
    if not e164:
        return PhoneForm(error="Enter your number to get a code.")
    code = form.get("code", "").strip()
    if not re.fullmatch(r"[0-9]{6}", code):
        return CodeForm(e164, "Enter the 6-digit code from the text.")
    try:
        ok = notify.check_code(e164, code)
    except httpx.HTTPError as e:
        log.warning("checking a sign-in code failed: %s", type(e).__name__)
        return CodeForm(e164, "We couldn't check that code just now. Try again.")
    if not ok:
        return CodeForm(e164, "That code didn't work. Check the text and try again.")
    uid = data.get_or_create_volunteer(e164)
    profile = data.get_profile(uid)
    sess.clear()
    sess["uid"] = uid
    sess["sv"] = profile.session_version or 1
    return Redirect("/deck" if profile.onboarded_at else "/onboarding")


@ar("/logout", methods=["POST"])
def logout(sess):
    profile = data.get_profile(sess["uid"]) if sess.get("uid") else None
    if profile:
        data.sign_out_everywhere(profile.id)  # open chat sockets and other tabs end too
    sess.clear()
    return Redirect(
        "/org/login" if profile and profile.role == "coordinator" else "/login"
    )
