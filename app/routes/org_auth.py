import logging
import re

import httpx
from fasthtml.common import APIRouter, Form, Input, P, Redirect
from monsterui.all import Button, ButtonT, TextPresets

from app import data, notify
from app.ui import Page

ar = APIRouter()
log = logging.getLogger("causematch")

SENT = "If that address belongs to a coordinator, a sign-in link is on its way."
BAD_EMAIL = "Enter your email address."
BAD_LINK = "That sign-in link has expired or was already used. Ask for a new one."


def EmailForm(email="", error=None, notice=None):
    return Page(
        "Nonprofit sign in",
        Form(
            Input(
                name="email",
                value=email,
                type="email",
                inputmode="email",
                autocomplete="email",
                placeholder="you@nonprofit.org",
                aria_label="Email",
                required=True,
            ),
            Button("Email me a link", cls=ButtonT.primary + " w-full"),
            P("We email you a sign-in link. No password.", cls=TextPresets.muted_sm),
            action="/org/login/send",
            method="post",
        ),
        error=error,
        intro=notice or "Sign in to post volunteer openings.",
    )


@ar("/org/login", methods=["GET"])
def org_login_page():
    return EmailForm()


@ar("/org/login/send", methods=["POST"])
def org_login_send(email: str = ""):
    email = email.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return EmailForm(email[:254], BAD_EMAIL)
    uid = data.coordinator_by_email(email)
    if uid and data.coordinator_org_ids(uid):
        try:
            notify.send_login_link(email, data.create_login_token(uid))
        except httpx.HTTPError as e:
            log.warning("sending a sign-in link failed: %s", type(e).__name__)
    return EmailForm(notice=SENT)


@ar("/org/login/verify", methods=["GET"])
def org_login_verify(sess, token: str = ""):
    uid = data.consume_login_token(token) if 0 < len(token) <= 200 else None
    profile = data.get_profile(uid) if uid else None
    if (
        not profile
        or profile.role != "coordinator"
        or not data.coordinator_org_ids(uid)
    ):
        return EmailForm(error=BAD_LINK)
    sess.clear()
    sess["uid"] = uid
    sess["sv"] = profile.session_version or 1
    return Redirect("/org")
