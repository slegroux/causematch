"""The volunteer's own profile, /me/profile: their photo for now.

A photo goes up as a multipart form. static/photo.js first shrinks it in the browser and
shows it (a blob: preview); without script the form posts the original. Either way the
server decodes and re-encodes it (app/photos.py) before storing anything.
"""

from fasthtml.common import H1, A, Div, FtResponse, Img, P, Redirect, Script, Span
from fasthtml.common import Button as PlainButton
from fasthtml.common import Form as PlainForm
from monsterui.all import Alert, AlertT, Container, FormLabel, TextPresets
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app import data, photos
from app.forms import query
from app.ui import (
    BACK,
    CARD,
    TITLE,
    Avatar,
    BigButton,
    Icon,
    Router,
    TextInput,
    VolunteerNav,
)

ar = Router()

BAD_PHOTO = (
    "We couldn't read that picture. Choose a JPEG, PNG or WebP photo under 6 MB."
)
NOTICE = {"saved": "Photo saved.", "removed": "Photo removed."}
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


def ProfilePage(uid, error=None, notice=None):
    p = data.get_profile(uid)
    return Container(
        VolunteerNav("me"),
        A(
            Icon(BACK, size=18),
            "Back to me",
            href="/profile",
            cls="-ml-1 inline-flex min-h-11 items-center gap-1 text-sm font-semibold "
            "text-ink-muted hover:text-ink",
        ),
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
                        p.display_name or "Your profile",
                        cls="font-display text-lg font-extrabold text-ink",
                    ),
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
        Script(src="/static/photo.js", defer=True),
        cls="max-w-md pb-8",
    )


@ar("/me/profile", methods=["GET"])
def profile(req):
    return ProfilePage(req.scope["uid"], notice=query(req, "notice"))


async def read_photo(req):
    """The bytes of the form's one `photo` file, at most photos.MAX_UPLOAD + 1 of them;
    None when there is no file."""
    form = await req.form(max_files=1, max_fields=1)
    try:
        upload = form.get("photo")
        if not isinstance(upload, UploadFile):
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
    await run_in_threadpool(data.set_volunteer_photo, uid, processed)
    return Redirect("/me/profile?notice=saved")


@ar("/me/profile/photo/remove", methods=["POST"])
def remove_photo(req):
    data.remove_volunteer_photo(req.scope["uid"])
    return Redirect("/me/profile?notice=removed")
