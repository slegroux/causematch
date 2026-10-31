"""The volunteer's own profile, /me/profile: their photo, name, pronouns, bio and
interests, and how we reach them (a mobile number for texts, else email).

A photo goes up as a multipart form. static/photo.js first shrinks it in the browser and
shows it (a blob: preview); without script the form posts the original. Either way the
server decodes and re-encodes it (app/photos.py) before storing anything.
"""

import json
import logging
import re

import httpx
from fasthtml.common import (
    H1,
    H2,
    Datalist,
    Div,
    FtResponse,
    Img,
    Label,
    Option,
    P,
    Redirect,
    Script,
    Span,
    Textarea,
)
from fasthtml.common import Button as PlainButton
from fasthtml.common import Form as PlainForm
from monsterui.all import (
    Alert,
    AlertT,
    FormLabel,
    TextPresets,
)
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app import data, notify, oauth, photos, security
from app.auth import BAD_PHONE, normalize_phone, pending_phone, set_pending
from app.choices import INTERESTS, MAX_BIO, MAX_INTERESTS, MAX_PRONOUNS, PRONOUNS
from app.forms import (
    EMAIL,
    HIDDEN_CHARS_MESSAGE,
    bad_request,
    hidden_chars,
    query,
    str_form,
)
from app.routes.auth import phone_off
from app.routes.prefs import Checks as ChipChecks
from app.routes.safety import HubRow
from app.ui import (
    CARD,
    ORG_SECTION,
    SMALL_LINK,
    TITLE,
    TRASH,
    Avatar,
    BackLink,
    BigButton,
    Field,
    Router,
    TextInput,
    VolunteerShell,
)

ar = Router()
log = logging.getLogger("causematch")

BAD_PHOTO = (
    "We couldn't read that picture. Choose a JPEG, PNG or WebP photo under 6 MB."
)
BUSY = "Many photos are being uploaded right now. Try again in a minute."
NOTICE = {
    "saved": "Photo saved.",
    "removed": "Photo removed.",
    "details": "Profile saved.",
    "phone": "Number saved. Match news and shift reminders now come by text.",
    "connected": "Connected. You can sign in with it now.",
    "disconnected": "Disconnected.",
}
# Why Me's connect or disconnect didn't happen: ?error=<key>, from app/routes/oauth.py
ERRORS = {"last": "Keep at least one way to sign in."}
for _name, _label in oauth.LABELS.items():
    ERRORS |= {
        f"taken-{_name}": f"That {_label} account is already connected to another "
        "Causematch account.",
        f"other-{_name}": f"You already connected a {_label} account. Disconnect it "
        "first.",
        f"refused-{_name}": f"That {_label} account can't be connected. Contact us if "
        "that is a mistake.",
        f"failed-{_name}": f"Connecting {_label} didn't work. Try again.",
    }
# Me's add-a-number keeps its pending number under its own session keys (auth.set_pending)
ME_PENDING = "me_pending"
MAX_NAME = 80
MAX_NEIGHBORHOOD = 80
MAX_EMAIL = 254
MAX_OTHER_CAUSES = 300
# The types a phone's picker offers; the server decides what it accepts (photos.FORMATS)
ACCEPT = "image/jpeg,image/png,image/webp,image/heic,image/heif"


def PhotoForm(action, label, button, ident="photo", shrink=True, auto=False):
    """A picture upload: static/photo.js previews it and, with shrink, sends a 1600 px
    JPEG instead of the original. With auto, choosing the picture sends it."""
    return PlainForm(
        Div(
            FormLabel(label, fr=ident),
            TextInput(
                type="file",
                id=ident,
                name="photo",
                accept=ACCEPT,
                required=True,
                cls="h-auto py-2 file:mr-3 file:rounded-full file:border-0 "
                "file:bg-brand-soft file:px-4 file:py-2 file:font-bold "
                "file:text-brand-ink",
            ),
            cls="space-y-2",
        ),
        P(
            role="alert",
            data_photo_status="1",
            hidden=True,
            cls="text-sm font-semibold text-red-700",
        ),
        BigButton(button),
        action=action,
        method="post",
        enctype="multipart/form-data",
        data_photo_form="1",
        data_shrink="1600" if shrink else None,
        data_auto="1" if auto else None,
        cls="space-y-3",
    )


def DetailsForm(p, f=None):
    """What nonprofits see about the volunteer, then what only we see; f: the values just
    typed, after an error."""
    f = f or {
        "first_name": p.display_name or "",
        "last_name": p.last_name or "",
        "pronouns": p.pronouns or "",
        "bio": p.bio or "",
        "neighborhood": p.neighborhood or "",
        "email": p.email or "",
        "other_causes": p.other_causes or "",
        "interests": json.loads(p.interests or "[]"),
    }
    return PlainForm(
        H2("About you", cls=ORG_SECTION),
        P(
            "Nonprofits see these after you like one of their openings: your first "
            "name and last initial (your full last name once they say yes), pronouns, "
            "bio, interests and neighborhood.",
            cls=TextPresets.muted_sm,
        ),
        Field(
            "First name",
            TextInput(
                name="first_name",
                value=f["first_name"],
                maxlength=str(MAX_NAME),
                autocomplete="given-name",
                required=True,
            ),
        ),
        Field(
            "Last name (optional)",
            TextInput(
                name="last_name",
                value=f["last_name"],
                maxlength=str(MAX_NAME),
                autocomplete="family-name",
            ),
        ),
        Field(
            "Pronouns (optional)",
            TextInput(
                name="pronouns",
                value=f["pronouns"],
                maxlength=str(MAX_PRONOUNS),
                list="pronoun-suggestions",
                placeholder="e.g. they/them",
            ),
            Datalist(*[Option(value=v) for v in PRONOUNS], id="pronoun-suggestions"),
        ),
        Field(
            f"A short bio (optional, up to {MAX_BIO} characters)",
            Textarea(
                f["bio"],
                name="bio",
                rows=4,
                maxlength=str(MAX_BIO),
                placeholder="What brings you to volunteering?",
                cls="uk-textarea",
            ),
        ),
        Field(
            "Neighborhood nonprofits see (optional, e.g. Silver Lake)",
            TextInput(
                name="neighborhood",
                value=f["neighborhood"],
                maxlength=str(MAX_NEIGHBORHOOD),
            ),
        ),
        Field(
            f"Interests (up to {MAX_INTERESTS})",
            ChipChecks("interests", INTERESTS, f["interests"], "flex flex-wrap gap-2"),
        ),
        H2("Only we see", cls=ORG_SECTION + " pt-2"),
        P("Never shown to nonprofits.", cls=TextPresets.muted_sm),
        Field(
            "Email (optional, for last-minute calls when you have no texts)",
            TextInput(
                name="email",
                type="email",
                value=f["email"],
                maxlength=str(MAX_EMAIL),
                autocomplete="email",
            ),
        ),
        Field(
            "Any other cause you'd like to see here? (optional)",
            TextInput(
                name="other_causes",
                value=f["other_causes"],
                maxlength=str(MAX_OTHER_CAUSES),
            ),
        ),
        BigButton("Save profile"),
        action="/me/profile/details",
        method="post",
        cls="space-y-4 p-5 " + CARD,
    )


def CodeInput():
    return TextInput(
        name="code",
        inputmode="numeric",
        autocomplete="one-time-code",
        maxlength="6",
        aria_label="Code",
        required=True,
    )


def ReachCard(p, pending=None):
    """How we reach this volunteer: texts to their number, or email; without a number
    (and while texts can be sent) a form to add and verify one."""
    to = " to the address below." if p.email else ": add your email below."
    by_email = f"match news and shift reminders come by email{to}"
    if notify.channel(p.id) == "sms":
        content = (
            H2("Texts", cls=ORG_SECTION),
            P(
                "Match news and shift reminders come by text to "
                f"{notify.mask(p.phone)}. A volunteer without a number gets them "
                "by email.",
                cls=TextPresets.muted_sm,
            ),
        )
    elif p.phone or not notify.phone_sign_in_enabled():
        content = (
            H2("Email", cls=ORG_SECTION),
            P(by_email.capitalize(), cls=TextPresets.muted_sm),
        )
    else:
        content = (
            H2("Add a mobile number for text reminders", cls=ORG_SECTION),
            P(f"Without one, {by_email}", cls=TextPresets.muted_sm),
            PlainForm(
                P(f"We texted a code to {pending}.", cls="text-ink"),
                Field("Code", CodeInput()),
                BigButton("Save number"),
                action="/me/phone/verify",
                method="post",
                cls="space-y-3",
            )
            if pending
            else None,
            PlainForm(
                Field(
                    "Another number" if pending else "Mobile number",
                    TextInput(
                        name="phone",
                        type="tel",
                        inputmode="tel",
                        autocomplete="tel",
                        placeholder="(310) 555-0123",
                        aria_label="Mobile number",
                        required=True,
                    ),
                ),
                BigButton("Text me a code"),
                action="/me/phone/send",
                method="post",
                cls="space-y-3",
            ),
        )
    return Div(*content, cls="space-y-3 p-5 " + CARD)


def SignInMethods(p):
    """Me's sign-in methods: the number, and each provider either connected (with its
    email and a Disconnect button) or offered (Connect), when it is configured."""
    connected = data.identities_of(p.id)

    def button(label, action):
        return PlainForm(
            PlainButton(label, type="submit", cls=SMALL_LINK + " text-sm"),
            action=action,
            method="post",
        )

    rows = [
        Div(
            P(f"Connected as {connected[name]}", cls="text-ink"),
            button(f"Disconnect {label}", f"/me/identity/{name}/disconnect"),
            cls="space-y-1",
        )
        if name in connected
        else button(f"Connect {label}", f"/me/identity/{name}/connect")
        for name, label in oauth.LABELS.items()
        if name in connected or oauth.get_provider(name)
    ]
    return Div(
        H2("Sign-in methods", cls=ORG_SECTION),
        P(
            "Phone: " + (notify.mask(p.phone) if p.phone else "none"),
            cls="text-ink",
        ),
        *rows,
        cls="space-y-3 p-5 " + CARD,
    )


def ProfilePage(uid, error=None, notice=None, f=None, pending=None):
    p = data.get_profile(uid)
    return VolunteerShell(
        "me",
        BackLink("/profile", "Back to me"),
        H1("Your profile", cls=TITLE),
        Alert(error, cls=AlertT.error + " mb-4") if error else None,
        Alert(NOTICE[notice], cls=AlertT.info + " mb-4") if notice in NOTICE else None,
        Div(
            Div(
                # the picture is the tap target that opens the file picker
                Label(
                    Avatar(p.display_name, p.photo_key, "lg", data_photo_current="1"),
                    # what the chosen photo will look like, filled in by static/photo.js
                    Img(
                        alt="Your new photo",
                        hidden=True,
                        data_photo_preview="1",
                        cls="h-28 w-28 rounded-full object-cover ring-2 ring-white "
                        "shadow-warm",
                    ),
                    fr="photo",
                    cls="cursor-pointer",
                ),
                Div(
                    Span(
                        " ".join(n for n in (p.display_name, p.last_name) if n)
                        or "Your profile",
                        cls=ORG_SECTION,
                    ),
                    P(p.pronouns, cls="text-sm text-ink-muted") if p.pronouns else None,
                    P(
                        "Nonprofits see your photo only after you like one of their "
                        "openings.",
                        cls=TextPresets.muted_sm,
                    ),
                ),
                cls="flex items-center gap-4",
            ),
            PhotoForm(
                "/me/profile/photo",
                "Change your photo" if p.photo_key else "Add a photo",
                "Save photo",
                auto=True,
            ),
            PlainForm(
                PlainButton(
                    "Remove photo",
                    type="submit",
                    cls=SMALL_LINK + " text-sm",
                ),
                action="/me/profile/photo/remove",
                method="post",
            )
            if p.photo_key
            else None,
            data_photo_card="1",
            cls="space-y-5 p-5 " + CARD,
        ),
        Div(ReachCard(p, pending), cls="mt-4"),
        Div(SignInMethods(p), cls="mt-4"),
        Div(DetailsForm(p, f), cls="mt-4"),
        Div(
            HubRow(TRASH, "Delete my account", "/profile/delete", danger=True),
            cls="mt-4 overflow-hidden " + CARD,
        ),
        P(
            "Deleting your account removes your name, phone, email and location and "
            "withdraws your matches.",
            cls=TextPresets.muted_sm + " px-2 pt-2",
        ),
        Script(src="/static/photo.js", defer=True),
        uid=uid,
    )


@ar("/me/profile", methods=["GET"])
def profile(req, sess):
    return ProfilePage(
        req.scope["uid"],
        error=ERRORS.get(query(req, "error")),
        notice=query(req, "notice"),
        pending=pending_phone(sess, ME_PENDING),
    )


async def read_photo(req):
    """The bytes of the form's one `photo` file, at most photos.MAX_UPLOAD + 1 of them;
    None unless the form is exactly that one file. (FastHTML has already parsed the form
    with Starlette's defaults, so req.form's own max_files would not apply.)"""
    form = await req.form()
    try:
        items = form.multi_items()
        if len(items) != 1 or items[0][0] != "photo":
            return None
        if not isinstance(upload := items[0][1], UploadFile):
            return None
        return await upload.read(photos.MAX_UPLOAD + 1)
    finally:
        await form.close()


@ar("/me/profile/photo", methods=["POST"])
async def save_photo(req):
    uid = req.scope["uid"]
    raw = await read_photo(req)
    try:
        if raw is None:
            raise photos.BadPhoto("no file")
        processed = await run_in_threadpool(photos.process, raw)
    except photos.BadPhoto:
        page = await run_in_threadpool(ProfilePage, uid, BAD_PHOTO)
        return FtResponse(page, status_code=400)
    except photos.Busy:
        page = await run_in_threadpool(ProfilePage, uid, BUSY)
        return FtResponse(page, status_code=503, headers={"Retry-After": "30"})
    await run_in_threadpool(data.set_volunteer_photo, uid, processed)
    return Redirect("/me/profile?notice=saved")


@ar("/me/profile/photo/remove", methods=["POST"])
def remove_photo(req):
    data.remove_volunteer_photo(req.scope["uid"])
    return Redirect("/me/profile?notice=removed")


def details_error(f):
    """Why these profile answers can't be saved, or None."""
    if any(
        hidden_chars(v, multiline=k == "bio") for k, v in f.items() if k != "interests"
    ):
        return HIDDEN_CHARS_MESSAGE
    if not f["first_name"] or len(f["first_name"]) > MAX_NAME:
        return f"Enter your first name ({MAX_NAME} characters at most)."
    if len(f["last_name"]) > MAX_NAME:
        return f"Keep your last name to {MAX_NAME} characters."
    if len(f["pronouns"]) > MAX_PRONOUNS:
        return f"Keep your pronouns to {MAX_PRONOUNS} characters."
    if len(f["bio"]) > MAX_BIO:
        return f"Keep your bio to {MAX_BIO} characters."
    if len(f["neighborhood"]) > MAX_NEIGHBORHOOD:
        return f"Keep your neighborhood to {MAX_NEIGHBORHOOD} characters."
    if len(f["email"]) > MAX_EMAIL or (f["email"] and not EMAIL.fullmatch(f["email"])):
        return "That email doesn't look right. Leave it blank to skip."
    if len(f["other_causes"]) > MAX_OTHER_CAUSES:
        return f"Keep other causes to {MAX_OTHER_CAUSES} characters."
    picked = set(f["interests"])
    if not picked <= {v for v, _ in INTERESTS} or len(picked) > MAX_INTERESTS:
        return f"Pick up to {MAX_INTERESTS} interests from the list."
    return None


@ar("/me/profile/details", methods=["POST"])
async def save_details(req):
    form = await str_form(req)
    if form is None:
        return bad_request()
    f = {
        k: form.get(k, "").strip()
        for k in (
            "first_name",
            "last_name",
            "pronouns",
            "bio",
            "neighborhood",
            "email",
            "other_causes",
        )
    }
    f["interests"] = list(dict.fromkeys(form.getlist("interests")))
    return await run_in_threadpool(_sync_save_details, req.scope["uid"], f)


def _sync_save_details(uid, f):
    if error := details_error(f):
        return FtResponse(ProfilePage(uid, error, f=f), status_code=400)
    data.save_profile_details(uid, **f)
    return Redirect("/me/profile?notice=details")


def _phone_error(uid, error, pending=None):
    return FtResponse(ProfilePage(uid, error, pending=pending), status_code=400)


@ar("/me/phone/send", methods=["POST"])
async def phone_send(req, sess):
    """Text a code to the number a volunteer without one wants to add. Counted as a
    sign-in code is (security.sms_send_limits); the number waits in the session for
    /me/phone/verify, under Me's own keys."""
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_phone_send, req, form, sess)


def _sync_phone_send(req, form, sess):
    if not notify.phone_sign_in_enabled():
        return phone_off()
    uid = req.scope["uid"]
    if data.get_profile(uid).phone:
        return Redirect("/me/profile")  # changing a number is not offered
    e164 = normalize_phone(form.get("phone", ""))
    if not e164:
        return _phone_error(uid, BAD_PHONE)
    if not security.allow(*security.sms_send_limits(req, e164)):
        return security.too_many()
    try:
        if not data.phone_suspended(
            e164
        ):  # as at sign-in: a suspended number costs no text
            notify.send_code(e164)
    except httpx.HTTPError as e:
        log.warning("sending a code to add a number failed: %s", type(e).__name__)
        return _phone_error(
            uid, "We couldn't send a text just now. Try again in a minute."
        )
    set_pending(sess, e164, ME_PENDING)
    return Redirect("/me/profile")


@ar("/me/phone/verify", methods=["POST"])
async def phone_verify(req, sess):
    """Save the pending number once its code checks out (security.verify_limits); a
    number on another live account is refused and nothing is saved."""
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_phone_verify, req, form, sess)


def _sync_phone_verify(req, form, sess):
    if not notify.phone_sign_in_enabled():
        return phone_off()
    uid = req.scope["uid"]
    e164 = pending_phone(sess, ME_PENDING)  # only the number a code was sent to
    if not e164:
        return _phone_error(uid, "Enter your number to get a code.")
    code = form.get("code", "").strip()
    if not re.fullmatch(r"[0-9]{6}", code):
        return _phone_error(uid, "Enter the 6-digit code from the text.", e164)
    # only wrong codes count, so the right one always gets through below the limit
    if not security.allow(*security.verify_limits(e164), record=False):
        return security.too_many()
    try:
        ok = notify.check_code(e164, code)
    except httpx.HTTPError as e:
        log.warning("checking a code to add a number failed: %s", type(e).__name__)
        return _phone_error(
            uid, "We couldn't check that code just now. Try again.", e164
        )
    if not ok:
        security.allow(*security.verify_limits(e164))
        return _phone_error(
            uid, "That code didn't work. Check the text and try again.", e164
        )
    for key in ("phone", "at"):  # the code is used: the number leaves the session
        sess.pop(f"{ME_PENDING}_{key}", None)
    why = data.add_volunteer_phone(uid, e164)
    if why == "taken":
        return _phone_error(uid, "That number is already on another account.")
    if why:
        return _phone_error(
            uid, "That number can't be added. Contact us if that is a mistake."
        )
    return Redirect("/me/profile?notice=phone")
