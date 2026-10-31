"""Continue with Google or Apple, for volunteers: /auth/{google,apple}/start and their
callbacks. Google returns by a GET, Apple by a cross-site form POST (form_post). The flow
and its checks live in app/oauth.py. With CAUSEMATCH_FAKE_OAUTH=1 the fake provider serves
the same routes through a local consent page, /auth/fake/consent."""

import dataclasses
import json
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

FAILED = "That {} sign-in didn't work or took too long. Try again."
DOWN = "We couldn't sign you in with {} just now. Try again in a minute."
CANCELLED = "{} sign-in was cancelled."
UNVERIFIED = (
    "Your {} account's email isn't verified yet, so it can't sign in here. "
    "Verify it with {}, or use your phone number."
)
MAX_PICTURE_URL = 2000  # characters kept in the session for onboarding's photo choice


def _not_found():
    return Response("Not found", status_code=404)


def _cookie_options(name):
    if name == "apple":
        # Apple posts back cross-site, and only a SameSite=None cookie rides along on that
        # POST. Browsers take SameSite=None only when Secure, so Apple sign-in works only
        # over https (the plan's Risks: tested live on the preview deploy).
        return {"path": "/", "secure": True, "httponly": True, "samesite": "none"}
    # Google comes back by a top-level GET, which a Lax cookie rides along on
    return {"path": "/", "secure": is_production(), "httponly": True, "samesite": "lax"}


def _redirect_uri(name):
    return public_base_url() + f"/auth/{name}/callback"


def _start(name):
    provider = oauth.get_provider(name)
    if provider is None:
        return _not_found()
    state, nonce, verifier = oauth.new_flow()
    r = RedirectResponse(
        provider.authorize_url(
            _redirect_uri(name), state, nonce, oauth.challenge(verifier)
        ),
        status_code=303,
    )
    r.set_cookie(
        oauth.oauth_cookie(),
        oauth.seal(name, state, nonce, verifier),
        max_age=oauth.COOKIE_MAX_AGE,
        **_cookie_options(name),
    )
    return r


@ar("/auth/google/start", methods=["GET"])
def google_start():
    return _start("google")


@ar("/auth/apple/start", methods=["GET"])
def apple_start():
    return _start("apple")


@ar("/auth/google/callback", methods=["GET"])
async def google_callback(req, sess):
    params = {k: query(req, k) for k in ("code", "state", "error")}
    return await _callback(req, sess, "google", params)


@ar("/auth/apple/callback", methods=["POST"])
async def apple_callback(req, sess):
    """Apple's form_post: code, state, user (names, first consent only), error. Its
    id_token field is ignored: the one verified comes from the token endpoint. Exempt from
    the Origin check (app/security.py ORIGIN_EXEMPT); the flow cookie's state guards it."""
    form = await str_form(req)
    fields = ("code", "state", "user", "error")
    params = {k: form.get(k, "") for k in fields} if form is not None else {}
    return await _callback(req, sess, "apple", params)


async def _callback(req, sess, name, params):
    provider = oauth.get_provider(name)
    if provider is None:
        return _not_found()
    resp = await run_in_threadpool(_sync_callback, req, sess, provider, params)
    if hasattr(resp, "__response__"):
        resp = resp.__response__(req)
    # single use, whatever happened: a replay of this callback finds no cookie
    resp.delete_cookie(oauth.oauth_cookie(), **_cookie_options(name))
    return resp


def _login(error, status):
    return FtResponse(PhoneForm(error=error), status_code=status)


def _sync_callback(req, sess, provider, params):
    name, label = provider.name, oauth.LABELS[provider.name]
    try:
        nonce, verifier = oauth.unseal(
            req.cookies.get(oauth.oauth_cookie()), name, params.get("state", "")
        )
    except oauth.AuthError as e:
        log.warning("%s sign-in refused: %s", label, e)
        return _login(FAILED.format(label), 400)
    code = params.get("code", "")
    if params.get("error") or not code:
        return _login(CANCELLED.format(label), 200)
    try:
        claims = provider.finish(code, verifier, _redirect_uri(name), nonce)
    except oauth.Unavailable as e:
        log.warning(
            "%s sign-in failed: %s", label, e
        )  # the exception type, never a body
        return _login(DOWN.format(label), 200)
    except oauth.AuthError as e:
        log.warning("%s sign-in refused: %s", label, e)
        return _login(FAILED.format(label), 400)
    if not claims.subject:
        return _login(FAILED.format(label), 400)
    if not (claims.email_verified and claims.email):
        return _login(UNVERIFIED.format(label, label), 400)
    if params.get("user"):  # Apple's names, sent on first consent only
        given, family = oauth.apple_names(params["user"])
        claims = dataclasses.replace(claims, given_name=given, family_name=family)
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

FIELDS = ("sub", "email", "given_name", "family_name", "picture_url")


def _fake(name):
    provider = oauth.get_provider(name or "google")
    return provider if isinstance(provider, oauth.Fake) else None


def _flow_fields(fake):
    return ("state", "nonce", "code_challenge") if fake.pkce else ("state", "nonce")


@ar("/auth/fake/consent", methods=["GET"])
def fake_consent(req):
    fake = _fake(query(req, "provider"))
    if fake is None:
        return _not_found()
    label = oauth.LABELS[fake.name]
    return SignInPage(
        f"Fake {label} sign-in",
        Form(
            Hidden(name="provider", value=fake.name),
            *[Hidden(name=k, value=query(req, k)) for k in _flow_fields(fake)],
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
        intro=f"Development only: nothing goes to {label}.",
    )


@ar("/auth/fake/consent", methods=["POST"])
async def fake_consent_post(req):
    form = await str_form(req)
    if form is None:
        return bad_request()
    fake = _fake(form.get("provider", ""))
    if fake is None:
        return _not_found()
    flow = _flow_fields(fake)
    f = {k: form.get(k, "").strip() for k in (*flow, *FIELDS)}
    if not all(f[k] for k in (*flow, "sub")):
        return bad_request()
    apple = fake.name == "apple"
    claims = oauth.Claims(
        provider=fake.name,
        subject=f["sub"],
        email=f["email"],
        email_verified=form.get("email_verified") == "1",
        # Apple's ID token carries no names: they come in the posted user field
        given_name="" if apple else f["given_name"],
        family_name="" if apple else f["family_name"],
        picture_url="" if apple else f["picture_url"],
    )
    code = fake.mint(claims, f["nonce"], f.get("code_challenge", ""))
    if not apple:
        return Redirect(
            "/auth/google/callback?" + urlencode({"code": code, "state": f["state"]})
        )
    return _fake_apple_post(code, f)


def _fake_apple_post(code, f):
    """What Apple's page does next: post code, state and user to the callback. A button
    to press, since the CSP allows no script to submit it."""
    user = {"name": {"firstName": f["given_name"], "lastName": f["family_name"]}}
    return SignInPage(
        "Fake Apple sign-in",
        Form(
            Hidden(name="code", value=code),
            Hidden(name="state", value=f["state"]),
            Hidden(name="user", value=json.dumps({**user, "email": f["email"]})),
            BigButton("Continue to Causematch"),
            action="/auth/apple/callback",
            method="post",
            cls="space-y-4",
        ),
        intro="Development only: Apple would post this form back from its own site.",
    )
