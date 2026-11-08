"""Continue with Google or Apple: /auth/{google,apple}/start and their callbacks. Google
returns by a GET, Apple by a cross-site form POST (form_post). The flow and its checks live
in app/oauth.py. With CAUSEMATCH_FAKE_OAUTH the fake provider serves the same routes
through a local consent page, /auth/fake/consent: for Google only with "1", for each
provider it names otherwise (oauth.fake_names). On a preview that page sets no subject:
the browser's own, random and sealed in a cookie at /start, is the account.

One flow, three intents, sealed in the flow cookie: a volunteer signing in (/login), a
coordinator or admin signing in (?for=org, from /org/login: the verified email must be the
one on file, and the provider must host that mailbox, else the emailed link is sent) and a
signed-in volunteer connecting a provider (Me's Connect, a same-origin POST). A callback
acts only on its own intent."""

import dataclasses
import hmac
import json
import logging
from urllib.parse import urlencode

from fasthtml.common import (
    A,
    Button,
    Form,
    FtResponse,
    Hidden,
    Redirect,
    Response,
    Span,
)
from monsterui.all import LabelCheckboxX
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse

from app import data, oauth, security
from app.auth import sign_in, sign_in_staff
from app.config import demo_address, is_preview, is_production, public_base_url
from app.forms import EMAIL, bad_request, query, str_form
from app.routes.auth import SUSPENDED, LoginPage, SentPage
from app.routes.auth import link_sent as volunteer_link_sent
from app.routes.org_auth import EmailForm, link_sent
from app.ui import BigButton, Field, Refusal, Router, SignInPage, TextInput

ar = Router()
log = logging.getLogger("causematch")

FAILED = "That {} sign-in didn't work or took too long. Try again."
DOWN = "We couldn't sign you in with {} just now. Try again in a minute."
CANCELLED = "{} sign-in was cancelled."
UNVERIFIED = (
    "Your {} account's email isn't verified yet, so it can't sign in here. "
    "Verify it with {}, or get an emailed link."
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


def _start(req, name, intent="volunteer", uid=None, sv=None):
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
    if (
        is_preview()
        and isinstance(provider, oauth.Fake)
        and not oauth.browser_subject(req.cookies.get(oauth.fake_subject_cookie()))
    ):
        _set_subject(r, oauth.new_browser_subject())
    return r


def _set_subject(r, value):
    # read by the fake page's same-site POST, which a Lax cookie rides along on
    r.set_cookie(
        oauth.fake_subject_cookie(),
        value,
        max_age=oauth.FAKE_SUBJECT_MAX_AGE,
        **_cookie_options("google"),
    )


def _sign_in_intent(req):
    return "staff" if query(req, "for") == "org" else "volunteer"


@ar("/auth/google/start", methods=["GET"])
def google_start(req):
    return _start(req, "google", _sign_in_intent(req))


@ar("/auth/apple/start", methods=["GET"])
def apple_start(req):
    return _start(req, "apple", _sign_in_intent(req))


# --- Me: connect and disconnect (signed-in volunteers; the beforeware checks) -----------


def _connect(req, name):
    """Start a link flow for the signed-in volunteer: their uid and session version go
    into the sealed flow cookie, and the callback connects to nobody else."""
    profile = data.get_profile(req.scope["uid"])
    return _start(req, name, "link", profile.id, profile.session_version or 1)


def _disconnect(req, sess, name):
    """Only ways in that work now count as another: a provider while it is configured,
    and the emailed link (email is always configured). A disconnect ends every other
    session and open chat, because a reason to disconnect is often that the provider
    account is no longer only theirs; this browser stays signed in."""
    uid = req.scope["uid"]
    working = [n for n, _ in oauth.enabled_providers()] + [data.EMAIL_SIGN_IN]
    had = name in data.identities_of(uid)
    if not data.unlink_identity(uid, name, working):
        return Redirect("/me/profile?error=last")
    if had:
        data.sign_out_everywhere(uid)
        sess["sv"] = data.get_profile(uid).session_version or 1
    return Redirect("/me/profile?notice=disconnected")


@ar("/me/identity/google/connect", methods=["POST"])
def google_connect(req):
    return _connect(req, "google")


@ar("/me/identity/apple/connect", methods=["POST"])
def apple_connect(req):
    return _connect(req, "apple")


@ar("/me/identity/google/disconnect", methods=["POST"])
def google_disconnect(req, sess):
    return _disconnect(req, sess, "google")


@ar("/me/identity/apple/disconnect", methods=["POST"])
def apple_disconnect(req, sess):
    return _disconnect(req, sess, "apple")


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
    return FtResponse(LoginPage(error=error), status_code=status)


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
        return _login(UNVERIFIED.format(label, label), 400, intent, name)
    if intent == "staff":
        return _staff_sign_in(req, sess, claims)
    if intent == "link":
        return _link(sess, name, flow, claims)
    trusted = _trusted(provider)
    if _refuses_staff(claims, trusted):
        return _staff_address(req, sess, claims)
    if params.get("user"):  # Apple's names, sent on first consent only
        given, family = oauth.apple_names(params["user"])
        claims = dataclasses.replace(claims, given_name=given, family_name=family)
    profile = data.get_profile(data.volunteer_for_identity(claims, trusted))
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


def _trusted(provider):
    """Whether a provider's verified email may join accounts (data.volunteer_for_identity):
    not the fake's on a preview, which takes any typed email as verified."""
    return not (is_preview() and isinstance(provider, oauth.Fake))


def _refuses_staff(claims, trusted):
    """One address, one role: a volunteer sign-up, or a join, whose verified email is a
    coordinator's or admin's mailbox (data.staff_mailbox) is refused. A volunteer who
    signed in with this identity before keeps signing in: a claim's unverified address,
    approved as a coordinator's, mustn't lock them out. The fake on a preview proves no
    address, and its answer mustn't tell a staff address from another, so there only a
    demo address is checked; any other signs this browser's own account in or up."""
    email = claims.email.strip()
    if not (trusted or demo_address(email)):
        return False
    known = data.identity_owner(claims.provider, claims.subject)
    return data.staff_mailbox(email) and not known


def _staff_address(req, sess, claims):
    """A volunteer sign-in refused for a staff mailbox (_refuses_staff): no volunteer is
    signed in or created.
    The answer is the emailed link's for that address (/login/send): "check your email",
    and the staff link goes to an exact live staff address, nothing to a +tag of one."""
    sess.clear()
    raw = claims.email.strip()
    if not (raw.isascii() and len(raw) <= 254 and EMAIL.fullmatch(raw)):
        return FtResponse(SentPage(raw[:254].lower()))  # nothing sent: no valid address
    sent = volunteer_link_sent(req, raw.lower(), sess)
    return sent if isinstance(sent, Response) else FtResponse(sent)


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
        sent = link_sent(req, email, sess)
        return sent if isinstance(sent, Response) else FtResponse(sent)
    profile, is_coordinator = data.staff_by_email(email)
    # A preview's fake consent page takes any typed email as verified, so there it signs
    # in a demo coordinator only: never an admin (as on /org/login, where an admin's link
    # stays in the log) and never an address off the demo's domains (org_auth.link_sent)
    if (
        profile
        and is_preview()
        and claims.provider in oauth.fake_names()
        and not (is_coordinator and demo_address(email))
    ):
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
    trusted = _trusted(oauth.get_provider(name))
    if why := data.link_identity(flow.uid, claims, trusted):
        return Redirect(f"/me/profile?error={why}-{name}")
    return Redirect("/me/profile?notice=connected")


# --- the fake provider's consent page (CAUSEMATCH_FAKE_OAUTH only) ----------------------

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
    preview = is_preview()
    return SignInPage(
        f"Fake {label} sign-in",
        Form(
            Hidden(name="provider", value=fake.name),
            *[Hidden(name=k, value=query(req, k)) for k in _flow_fields(fake)],
            # blank, not a shared default: the subject is the account, so two people who
            # both keep a default would sign in to one account. A preview's is the
            # browser's own (fake_consent_post), so there is nothing to type.
            None
            if preview
            else Field(
                "Subject",
                TextInput(
                    name="sub",
                    value="",
                    placeholder="Any word only you will use",
                    required=True,
                ),
            ),
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
            # a shared device: the next person gets an account of their own
            Button(
                "Use a different account",
                type="submit",
                name="fresh",
                value="1",
                cls="uk-btn uk-btn-default h-12 w-full",
            )
            if preview
            else None,
            action="/auth/fake/consent",
            method="post",
            cls="space-y-4",
        ),
        intro=f"Preview only: nothing goes to {label}. This browser keeps its own "
        "account, which no other browser can sign in to."
        if preview
        else f"Development only: nothing goes to {label}.",
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
    fresh = None
    if is_preview():  # never a typed subject: this browser's own, sealed at /start
        if not _own_flow(req, fake, f):
            return bad_request()
        if form.get("fresh") == "1":  # "Use a different account"
            fresh = oauth.new_browser_subject()
        cookie = fresh or req.cookies.get(oauth.fake_subject_cookie())
        f["sub"] = oauth.browser_subject(cookie)
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
        callback = "/auth/google/callback?"
        resp = Redirect(callback + urlencode({"code": code, "state": f["state"]}))
    else:
        resp = FtResponse(_fake_apple_post(code, f))
    if fresh:
        resp = resp.__response__(req)
        _set_subject(resp, fresh)
    return resp


def _own_flow(req, fake, f):
    """The posted flow is this browser's own, the one in its flow cookie from /start:
    state, nonce and PKCE challenge. Otherwise a link carrying someone else's flow would
    mint a code for this browser's subject, and the sender's callback would redeem it,
    signing them in to this browser's account. Apple's flow cookie is SameSite=None, so it
    rides along on this same-site POST too."""
    try:
        flow = oauth.unseal(
            req.cookies.get(oauth.oauth_cookie()), fake.name, f["state"]
        )
    except oauth.AuthError:
        return False
    # bytes: compare_digest refuses a str with non-ASCII characters
    if not hmac.compare_digest(flow.nonce.encode(), f["nonce"].encode()):
        return False
    posted = f.get("code_challenge", "").encode()
    return not fake.pkce or hmac.compare_digest(
        oauth.challenge(flow.verifier).encode(), posted
    )


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
