"""Continue with Google or Apple: /auth/{google,apple}/start and their callbacks. Google
returns by a GET, Apple by a cross-site form POST (form_post). The flow and its checks live
in app/oauth.py. With CAUSEMATCH_FAKE_OAUTH=1 the fake provider serves the same routes
through a local consent page, /auth/fake/consent.

One flow, three intents, sealed in the flow cookie: a volunteer signing in (/login), a
coordinator or admin signing in (?for=org, from /org/login: the verified email must be the
one on file, and the provider must host that mailbox, else the emailed link is sent) and a
signed-in volunteer connecting a provider (Me's Connect, a same-origin POST). A callback
acts only on its own intent."""

import dataclasses
import json
import logging
from urllib.parse import urlencode

from fasthtml.common import A, Form, FtResponse, Hidden, Redirect, Response, Span
from monsterui.all import LabelCheckboxX
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse

from app import data, notify, oauth, security
from app.auth import sign_in, sign_in_staff
from app.config import is_preview, is_production, public_base_url
from app.forms import EMAIL, bad_request, query, str_form
from app.routes.auth import SUSPENDED, PhoneForm
from app.routes.org_auth import EmailForm, link_sent
from app.ui import BigButton, Field, Refusal, Router, SignInPage, TextInput

ar = Router()
log = logging.getLogger("causematch")

FAILED = "That {} sign-in didn't work or took too long. Try again."
DOWN = "We couldn't sign you in with {} just now. Try again in a minute."
CANCELLED = "{} sign-in was cancelled."
UNVERIFIED = (
    "Your {} account's email isn't verified yet, so it can't sign in here. "
    "Verify it with {}, or {}."
)
NO_ORG = "No nonprofit account uses this email. Claim your nonprofit: "
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


def _start(name, intent="volunteer", uid=None, sv=None):
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
        oauth.seal(name, state, nonce, verifier, intent, uid, sv),
        max_age=oauth.COOKIE_MAX_AGE,
        **_cookie_options(name),
    )
    return r


def _sign_in_intent(req):
    return "staff" if query(req, "for") == "org" else "volunteer"


@ar("/auth/google/start", methods=["GET"])
def google_start(req):
    return _start("google", _sign_in_intent(req))


@ar("/auth/apple/start", methods=["GET"])
def apple_start(req):
    return _start("apple", _sign_in_intent(req))


# --- Me: connect and disconnect (signed-in volunteers; the beforeware checks) -----------


def _connect(req, name):
    """Start a link flow for the signed-in volunteer: their uid and session version go
    into the sealed flow cookie, and the callback connects to nobody else."""
    profile = data.get_profile(req.scope["uid"])
    return _start(name, "link", profile.id, profile.session_version or 1)


def _disconnect(req, name):
    """Only ways in that work now count as another: the phone while codes can be texted,
    a provider while it is configured."""
    providers = [n for n, _ in oauth.enabled_providers()]
    phone_works = notify.phone_sign_in_enabled()
    if not data.unlink_identity(req.scope["uid"], name, phone_works, providers):
        return Redirect("/me/profile?error=last")
    return Redirect("/me/profile?notice=disconnected")


@ar("/me/identity/google/connect", methods=["POST"])
def google_connect(req):
    return _connect(req, "google")


@ar("/me/identity/apple/connect", methods=["POST"])
def apple_connect(req):
    return _connect(req, "apple")


@ar("/me/identity/google/disconnect", methods=["POST"])
def google_disconnect(req):
    return _disconnect(req, "google")


@ar("/me/identity/apple/disconnect", methods=["POST"])
def apple_disconnect(req):
    return _disconnect(req, "apple")


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
    # whatever happened: a cooperating browser's replay finds no cookie, and the
    # provider's code works once anyway (app/oauth.py unseal)
    resp.delete_cookie(oauth.oauth_cookie(), **_cookie_options(name))
    return resp


def _login(error, status, intent="volunteer", name=""):
    """The page a refused or failed callback answers with: its intent's sign-in page, or,
    for a link, a 400 (a failed check) or Me with what went wrong."""
    if intent == "staff":
        return FtResponse(EmailForm(error=error), status_code=status)
    if intent == "link":
        return (
            Refusal(400)
            if status == 400
            else Redirect(f"/me/profile?error=failed-{name}")
        )
    return FtResponse(PhoneForm(error=error), status_code=status)


def _sync_callback(req, sess, provider, params):
    name, label = provider.name, oauth.LABELS[provider.name]
    try:
        flow = oauth.unseal(
            req.cookies.get(oauth.oauth_cookie()), name, params.get("state", "")
        )
    except oauth.AuthError as e:
        log.warning("%s sign-in refused: %s", label, e)
        return _login(FAILED.format(label), 400)
    # Each callback past here can call the provider with our client secret, so it is
    # counted. Only after the flow cookie and state check out: a page on another site can
    # fire junk callbacks at a visitor (an img tag, Apple's cross-site post), and those
    # must not use up the budget of everyone behind the same address. A valid cookie only
    # comes from /start, so the budget a caller can spend is their own address's.
    if not security.allow((security.OAUTH_PER_IP, security.ip_subject(req))):
        return security.too_many()
    intent = flow.intent
    code = params.get("code", "")
    if params.get("error") or not code:
        return _login(CANCELLED.format(label), 200, intent, name)
    try:
        claims = provider.finish(code, flow.verifier, _redirect_uri(name), flow.nonce)
    except oauth.Unavailable as e:
        log.warning(
            "%s sign-in failed: %s", label, e
        )  # the exception type, never a body
        return _login(DOWN.format(label), 200, intent, name)
    except oauth.AuthError as e:
        log.warning("%s sign-in refused: %s", label, e)
        return _login(FAILED.format(label), 400, intent, name)
    if not claims.subject:
        return _login(FAILED.format(label), 400, intent, name)
    if not (claims.email_verified and claims.email):
        other = "get an emailed link" if intent == "staff" else "use your phone number"
        return _login(UNVERIFIED.format(label, label, other), 400, intent, name)
    if intent == "staff":
        return _staff_sign_in(req, sess, claims)
    if intent == "link":
        return _link(sess, name, flow, claims)
    if params.get("user"):  # Apple's names, sent on first consent only
        given, family = oauth.apple_names(params["user"])
        claims = dataclasses.replace(claims, given_name=given, family_name=family)
    profile = data.get_profile(data.volunteer_for_identity(claims))
    if profile is None or profile.role != "volunteer":  # suspended by an admin
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


def _staff_sign_in(req, sess, claims):
    """A coordinator or admin by the verified provider email, found as the emailed link
    finds them (data.staff_by_email), when the provider hosts that mailbox
    (oauth.authoritative). Otherwise the verified email may be stale, so the answer is the
    emailed link's, for known and unknown addresses alike. Creates nothing: no profile,
    no identity row."""
    raw = claims.email.strip()
    # judged as the provider sent it: lowercasing first turns the Kelvin sign into a "k"
    if not (raw.isascii() and len(raw) <= 254 and EMAIL.fullmatch(raw)):
        no_org = Span(NO_ORG, A("start a claim", href="/org/claim", cls="font-bold"))
        return FtResponse(EmailForm(error=no_org), status_code=200)
    email = raw.lower()
    if not oauth.authoritative(claims):
        sent = link_sent(req, email)
        return sent if isinstance(sent, Response) else FtResponse(sent)
    profile, is_coordinator = data.staff_by_email(email)
    # A preview's fake consent page takes any typed email as verified, so there it never
    # signs in an admin: as on /org/login, where a preview shows a coordinator's link on
    # the page but an admin's stays in the log (org_auth.link_sent)
    if profile and not is_coordinator and is_preview() and oauth.fake_enabled():
        profile = None
    if profile is None:
        no_org = Span(NO_ORG, A("start a claim", href="/org/claim", cls="font-bold"))
        return FtResponse(EmailForm(error=no_org), status_code=200)
    return RedirectResponse(sign_in_staff(sess, profile, is_coordinator).loc, 303)


def _link(sess, name, flow, claims):
    """Connect this identity to the volunteer who pressed Connect: the flow's uid, still on
    the session version it was started with, and the session's own uid when the session
    cookie came along. Apple's cross-site form_post never carries it (SameSite=Lax), so
    there the sealed session version stands in: signing out or being suspended since
    changes it. The session is never written here, so Apple's post leaves it as it was."""
    profile = data.get_profile(flow.uid)
    current = bool(profile) and (profile.session_version or 1) == flow.sv
    same = sess.get("uid") == flow.uid and sess.get("sv") == flow.sv
    cross_site = name == "apple" and "uid" not in sess
    if not (current and profile.role == "volunteer" and (same or cross_site)):
        log.warning("%s connect refused: not the session that started it", name)
        return Refusal(400)
    if why := data.link_identity(flow.uid, claims):
        return Redirect(f"/me/profile?error={why}-{name}")
    return Redirect("/me/profile?notice=connected")


# --- the fake provider's consent page (CAUSEMATCH_FAKE_OAUTH=1 only) ----------------------

FIELDS = ("sub", "email", "given_name", "family_name", "picture_url", "hd")


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
            # Google's hd, a Workspace account's domain: staff sign-in needs it to match
            Field("Hosted domain", TextInput(name="hd", value=""))
            if fake.name == "google"
            else None,
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
        hosted_domain="" if apple else f["hd"],  # Apple has no hd
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
