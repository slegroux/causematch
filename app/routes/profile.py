"""The volunteer's own profile, /me/profile: their photo, name, pronouns, bio and
interests.

A photo goes up as a multipart form. static/photo.js first shrinks it in the browser and
shows it (a blob: preview); without script the form posts the original. Either way the
server decodes and re-encodes it (app/photos.py) before storing anything.
"""

import json

from fasthtml.common import (
    H1,
    H2,
    Datalist,
    Div,
    FtResponse,
    Img,
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
    LabelCheckboxX,
    TextPresets,
)
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app import data, photos
from app.choices import INTERESTS, MAX_BIO, MAX_INTERESTS, MAX_PRONOUNS, PRONOUNS
from app.forms import HIDDEN_CHARS_MESSAGE, bad_request, hidden_chars, query, str_form
from app.ui import (
    CARD,
    TITLE,
    Avatar,
    BackLink,
    BigButton,
    Field,
    Router,
    TextInput,
    VolunteerShell,
)

ar = Router()

BAD_PHOTO = (
    "We couldn't read that picture. Choose a JPEG, PNG or WebP photo under 6 MB."
)
BUSY = "Many photos are being uploaded right now. Try again in a minute."
NOTICE = {
    "saved": "Photo saved.",
    "removed": "Photo removed.",
    "details": "Profile saved.",
}
MAX_NAME = 80
MAX_NEIGHBORHOOD = 80  # as onboarding (LIMITS)
# The types a phone's picker offers; the server decides what it accepts (photos.FORMATS)
ACCEPT = "image/jpeg,image/png,image/webp,image/heic,image/heif"


def PhotoForm(action, label, button, ident="photo", shrink=True):
    """A picture upload: static/photo.js previews it and, with shrink, sends a 1600 px
    JPEG instead of the original."""
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
        cls="space-y-3",
    )


def DetailsForm(p, f=None):
    """Name, pronouns, bio, interests and the neighborhood nonprofits see; f: the values
    just typed, after an error."""
    f = f or {
        "first_name": p.display_name or "",
        "last_name": p.last_name or "",
        "pronouns": p.pronouns or "",
        "bio": p.bio or "",
        "neighborhood": p.neighborhood or "",
        "interests": json.loads(p.interests or "[]"),
    }
    return PlainForm(
        H2("About you", cls="font-display text-lg font-extrabold text-ink"),
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
            Div(
                *[
                    LabelCheckboxX(
                        label, name="interests", value=v, checked=v in f["interests"]
                    )
                    for v, label in INTERESTS
                ],
                cls="grid grid-cols-1 gap-y-1 sm:grid-cols-2 sm:gap-x-4",
            ),
        ),
        BigButton("Save profile"),
        action="/me/profile/details",
        method="post",
        cls="space-y-4 p-5 " + CARD,
    )


def ProfilePage(uid, error=None, notice=None, f=None):
    p = data.get_profile(uid)
    return VolunteerShell(
        "me",
        BackLink("/profile", "Back to me"),
        H1("Your profile", cls=TITLE),
        Alert(error, cls=AlertT.error + " mb-4") if error else None,
        Alert(NOTICE[notice], cls=AlertT.info + " mb-4") if notice in NOTICE else None,
        Div(
            Div(
                Avatar(p.display_name, p.photo_key, "lg", data_photo_current="1"),
                # what the chosen photo will look like, filled in by static/photo.js
                Img(
                    alt="Your new photo",
                    hidden=True,
                    data_photo_preview="1",
                    cls="h-28 w-28 rounded-full object-cover ring-2 ring-white "
                    "shadow-warm",
                ),
                Div(
                    Span(
                        " ".join(n for n in (p.display_name, p.last_name) if n)
                        or "Your profile",
                        cls="font-display text-lg font-extrabold text-ink",
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
            ),
            PlainForm(
                PlainButton(
                    "Remove photo",
                    type="submit",
                    cls="inline-flex min-h-11 items-center text-sm font-semibold "
                    "text-ink-muted underline underline-offset-4 hover:text-ink",
                ),
                action="/me/profile/photo/remove",
                method="post",
            )
            if p.photo_key
            else None,
            data_photo_card="1",
            cls="space-y-5 p-5 " + CARD,
        ),
        Div(DetailsForm(p, f), cls="mt-4"),
        Script(src="/static/photo.js", defer=True),
    )


@ar("/me/profile", methods=["GET"])
def profile(req):
    return ProfilePage(req.scope["uid"], notice=query(req, "notice"))


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
        for k in ("first_name", "last_name", "pronouns", "bio", "neighborhood")
    }
    f["interests"] = list(dict.fromkeys(form.getlist("interests")))
    return await run_in_threadpool(_sync_save_details, req.scope["uid"], f)


def _sync_save_details(uid, f):
    if error := details_error(f):
        return FtResponse(ProfilePage(uid, error, f=f), status_code=400)
    data.save_profile_details(uid, **f)
    return Redirect("/me/profile?notice=details")
