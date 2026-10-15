import httpx
from fasthtml.common import A, APIRouter, Form, Input, P, Redirect
from monsterui.all import AT, Button, ButtonT, TextPresets

from app import data, notify
from app.auth import BAD_PHONE, normalize_phone
from app.ui import Page

ar = APIRouter()

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
            Input(name="phone", value=phone, type="hidden"),
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
def send(phone: str = ""):
    e164 = normalize_phone(phone)
    if not e164:
        return PhoneForm(phone, BAD_PHONE)
    try:
        notify.send_code(e164)
    except httpx.HTTPError:
        return PhoneForm(
            phone, "We couldn't send a text just now. Try again in a minute."
        )
    return CodeForm(e164)


@ar("/login/verify", methods=["POST"])
def verify(sess, phone: str = "", code: str = ""):
    e164 = normalize_phone(phone)
    if not e164:
        return PhoneForm(phone, BAD_PHONE)
    try:
        ok = notify.check_code(e164, code.strip())
    except httpx.HTTPError:
        return CodeForm(e164, "We couldn't check that code just now. Try again.")
    if not ok:
        return CodeForm(e164, "That code didn't work. Check the text and try again.")
    uid = data.get_or_create_volunteer(e164)
    sess.clear()
    sess["uid"] = uid
    onboarded = data.get_profile(uid).onboarded_at
    return Redirect("/deck" if onboarded else "/onboarding")


@ar("/logout", methods=["POST"])
def logout(sess):
    sess.clear()
    return Redirect("/login")
