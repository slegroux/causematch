"""Continue with Google, for volunteers: /auth/google/start and its callback. The flow and
its checks live in app/oauth.py. With CAUSEMATCH_FAKE_OAUTH=1 the fake provider serves the
same two routes through a local consent page, /auth/fake/consent."""

import logging
from urllib.parse import urlencode

from fasthtml.common import Form, FtResponse, Hidden, Redirect, Response
from monsterui.all import LabelCheckboxX
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse

from app import data, oauth
from app.auth import sign_in
from app.config import is_production, public_base_url
from app.forms import bad_request, query, str_form
from app.routes.auth import SUSPENDED, PhoneForm
from app.ui import BigButton, Field, Router, SignInPage, TextInput

ar = Router()
log = logging.getLogger("causematch")

FAILED = "That Google sign-in didn't work or took too long. Try again."
DOWN = "We couldn't sign you in with Google just now. Try again in a minute."
CANCELLED = "Google sign-in was cancelled."
UNVERIFIED = (
    "Your Google account's email isn't verified yet, so it can't sign in here. "
    "Verify it with Google, or use your phone number."
)
MAX_PICTURE_URL = 2000  # characters kept in the session for onboarding's photo choice


def _not_found():
    return Response("Not found", status_code=404)


def _cookie_options():
    return {
        "path": "/",
        "secure": is_production(),
        "httponly": True,
        "samesite": "lax",
    }


def _redirect_uri():
    return public_base_url() + "/auth/google/callback"


@ar("/auth/google/start", methods=["GET"])
def google_start():
    provider = oauth.get_google()
    if provider is None:
        return _not_found()
    state, nonce, verifier = oauth.new_flow()
    r = RedirectResponse(
        provider.authorize_url(
            _redirect_uri(), state, nonce, oauth.challenge(verifier)
        ),
        status_code=303,
    )
    r.set_cookie(
        oauth.oauth_cookie(),
        oauth.seal("google", state, nonce, verifier),
        max_age=oauth.COOKIE_MAX_AGE,
        **_cookie_options(),
    )
    return r


@ar("/auth/google/callback", methods=["GET"])
async def google_callback(req, sess):
    provider = oauth.get_google()
    if provider is None:
        return _not_found()
    resp = await run_in_threadpool(_sync_callback, req, sess, provider)
    if hasattr(resp, "__response__"):
        resp = resp.__response__(req)
    # single use, whatever happened: a replay of this URL finds no cookie
    resp.delete_cookie(oauth.oauth_cookie(), **_cookie_options())
    return resp


def _login(error, status):
    return FtResponse(PhoneForm(error=error), status_code=status)


def _sync_callback(req, sess, provider):
    try:
        nonce, verifier = oauth.unseal(
            req.cookies.get(oauth.oauth_cookie()), "google", query(req, "state")
        )
    except oauth.AuthError as e:
        log.warning("Google sign-in refused: %s", e)
        return _login(FAILED, 400)
    code = query(req, "code")
    if query(req, "error") or not code:
        return _login(CANCELLED, 200)
    try:
        claims = provider.finish(code, verifier, _redirect_uri(), nonce)
    except oauth.Unavailable as e:
        log.warning("Google sign-in failed: %s", e)  # the exception type, never a body
        return _login(DOWN, 200)
    except oauth.AuthError as e:
        log.warning("Google sign-in refused: %s", e)
        return _login(FAILED, 400)
    if not claims.subject:
        return _login(FAILED, 400)
    if not (claims.email_verified and claims.email):
        return _login(UNVERIFIED, 400)
    profile = data.get_profile(data.volunteer_for_identity(claims))
    if profile is None:  # suspended by an admin
        sess.clear()
        return _login(SUSPENDED, 200)
    loc = sign_in(sess, profile).loc
    if (
        claims.picture_url
        and not profile.onboarded_at
        and len(claims.picture_url) <= MAX_PICTURE_URL
    ):
        sess["oauth_picture"] = claims.picture_url  # onboarding offers it, unticked
    return RedirectResponse(loc, status_code=303)


# --- the fake provider's consent page (CAUSEMATCH_FAKE_OAUTH=1 only) ----------------------

FLOW = ("state", "nonce", "code_challenge")


def _fake():
    provider = oauth.get_google()
    return provider if isinstance(provider, oauth.Fake) else None


@ar("/auth/fake/consent", methods=["GET"])
def fake_consent(req):
    if _fake() is None:
        return _not_found()
    return SignInPage(
        "Fake Google sign-in",
        Form(
            *[Hidden(name=k, value=query(req, k)) for k in FLOW],
            Field("Subject", TextInput(name="sub", value="fake-1", required=True)),
            Field("Email", TextInput(name="email", type="email", value="")),
            LabelCheckboxX(
                "Email verified", name="email_verified", value="1", checked=True
            ),
            Field("Given name", TextInput(name="given_name", value="")),
            Field("Family name", TextInput(name="family_name", value="")),
            Field("Picture URL", TextInput(name="picture_url", value="")),
            BigButton("Continue"),
            action="/auth/fake/consent",
            method="post",
            cls="space-y-4",
        ),
        intro="Development only: nothing goes to Google.",
    )


@ar("/auth/fake/consent", methods=["POST"])
async def fake_consent_post(req):
    fake = _fake()
    if fake is None:
        return _not_found()
    form = await str_form(req)
    if form is None:
        return bad_request()
    f = {k: form.get(k, "").strip() for k in (*FLOW, "sub", "email", "given_name",
                                              "family_name", "picture_url")}  # fmt: skip
    if not all(f[k] for k in (*FLOW, "sub")):
        return bad_request()
    claims = oauth.Claims(
        provider="google",
        subject=f["sub"],
        email=f["email"],
        email_verified=form.get("email_verified") == "1",
        given_name=f["given_name"],
        family_name=f["family_name"],
        picture_url=f["picture_url"],
    )
    code = fake.mint(claims, f["nonce"], f["code_challenge"])
    return Redirect(
        "/auth/google/callback?" + urlencode({"code": code, "state": f["state"]})
    )
