"""What volunteers see of an org besides its openings: on /org/organization its logo, a
photo of its work, its about text and website; per opening, a photo and a link to the
event as the org advertises it. Uploads go through app/photos.py like a volunteer's."""

from functools import partial

from fasthtml.common import (
    H1,
    H2,
    A,
    Div,
    FtResponse,
    Img,
    P,
    Redirect,
    Script,
    Textarea,
)
from fasthtml.common import Form as PlainForm
from monsterui.all import (
    Alert,
    AlertT,
    Button,
    ButtonT,
    TextPresets,
)
from starlette.concurrency import run_in_threadpool

from app import data, photos
from app.forms import (
    HIDDEN_CHARS_MESSAGE,
    bad_request,
    hidden_chars,
    https_url,
    query,
    str_form,
)
from app.routes.profile import BAD_PHOTO, BUSY, PhotoForm, read_photo
from app.ui import (
    CARD,
    ORG_TITLE,
    SMALL_LINK,
    EventLink,
    Field,
    LogoBadge,
    OrgShell,
    Refusal,
    Router,
    TextInput,
)

ar = Router()

MAX_ABOUT = 1000
BAD_LINK = "Enter a full https:// address, like https://example.org/volunteer."
NOTICE = {
    "saved": "Saved. Volunteers see it on your cards now.",
}


def Preview(key, label, logo=False):
    """The current picture, and where static/photo.js shows the newly chosen one."""
    shape = "h-24 w-24 rounded-full" if logo else "aspect-[3/2] w-full rounded-2xl"
    return Div(
        Div(
            (
                LogoBadge(key, "h-24 w-24")
                if logo
                else Img(
                    src=f"/photos/{key}.jpg", alt=label, cls=shape + " object-cover"
                )
            )
            if key
            else P("None yet.", cls=TextPresets.muted_sm),
            data_photo_current="1",
        ),
        Img(
            alt=f"Your new {label.lower()}",
            hidden=True,
            data_photo_preview="1",
            cls=shape + " object-cover",
        ),
        cls="max-w-sm",
    )


def PictureCard(title, intro, key, action, ident, logo=False):
    return Div(
        H2(title, cls="font-display text-lg font-extrabold text-ink"),
        P(intro, cls=TextPresets.muted_sm),
        Preview(key, title, logo),
        PhotoForm(
            action,
            f"Choose a new {title.lower()}" if key else f"Add a {title.lower()}",
            f"Save {title.lower()}",
            ident=ident,
            shrink=not logo,  # a logo keeps its transparency: no JPEG in the browser
        ),
        data_photo_card="1",
        cls="space-y-3 p-5 " + CARD,
    )


def OrgSection(org, f=None, error=None):
    f = f or {"about": org.about or "", "website": org.website or ""}
    base = f"/org/organization/{org.id}"
    return Div(
        H2(org.name, cls="font-display text-2xl font-extrabold text-ink"),
        Alert(error, cls=AlertT.error) if error else None,
        Div(
            PictureCard(
                "Logo",
                "Shown as a round badge on your cards. A square or simple logo reads best.",
                org.logo_key,
                f"{base}/logo",
                f"logo-{org.id}",
                logo=True,
            ),
            PictureCard(
                "Photo",
                "A picture of your volunteers at work: the card shows it when an "
                "opening has no photo of its own.",
                org.photo_key,
                f"{base}/photo",
                f"photo-{org.id}",
            ),
            cls="grid gap-4 md:grid-cols-2",
        ),
        PlainForm(
            Field(
                f"About {org.name} (up to {MAX_ABOUT} characters)",
                Textarea(f["about"], name="about", rows=4, cls="uk-textarea"),
            ),
            Field(
                "Website (optional)",
                TextInput(
                    name="website",
                    type="url",
                    inputmode="url",
                    value=f["website"],
                    placeholder="https://",
                ),
            ),
            Button("Save details", cls=ButtonT.primary),
            action=base,
            method="post",
            cls="space-y-3 p-5 " + CARD,
        ),
        cls="space-y-4",
    )


def OrganizationPage(uid, notice="", errors=None):
    errors = errors or {}
    return OrgShell(
        "organization",
        data.org_waiting(uid),
        H1("Organization", cls=ORG_TITLE + " pb-4"),
        Alert(NOTICE[notice], cls=AlertT.info + " mb-4") if notice in NOTICE else None,
        Div(
            *[
                OrgSection(org, *errors.get(org.id, (None, None)))
                for org, _ in data.coordinator_orgs(uid)
            ],
            cls="max-w-3xl space-y-10",
        ),
        Script(src="/static/photo.js", defer=True),
    )


@ar("/org/organization", methods=["GET"])
def organization(req):
    return OrganizationPage(req.scope["uid"], query(req, "notice"))


@ar("/org/organization/{org_id}", methods=["POST"])
async def save_details(req, org_id: int):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_save_details, req.scope["uid"], org_id, form)


def _sync_save_details(uid, org_id, form):
    f = {
        "about": form.get("about", "").strip(),
        "website": form.get("website", "").strip(),
    }
    website = https_url(f["website"]) if f["website"] else None
    error = None
    if len(f["about"]) > MAX_ABOUT:
        error = f"Keep the about text to {MAX_ABOUT} characters."
    elif hidden_chars(f["about"], multiline=True):
        error = HIDDEN_CHARS_MESSAGE
    elif f["website"] and not website:
        error = BAD_LINK
    try:
        if error:
            if org_id not in data.coordinator_org_ids(uid):
                raise PermissionError("not a member")
            return FtResponse(
                OrganizationPage(uid, errors={org_id: (f, error)}), status_code=400
            )
        data.set_org_details(uid, org_id, f["about"], website)
    except PermissionError:
        return Refusal(403)
    return Redirect("/org/organization?notice=saved")


async def _upload(req, authorize, save, page, done):
    """A picture from the form, processed then saved by save(processed); page(error) on a
    bad picture (400) or when no decode slot frees up (503). authorize() runs before the
    picture is read or decoded, so a coordinator of another org gets 403 without either.
    (FastHTML has already parsed the multipart body, at most 6 MB spooled to disk, before
    any of this runs, even for a request that is then refused.)"""
    try:
        await run_in_threadpool(authorize)
    except PermissionError:
        return Refusal(403)
    raw = await read_photo(req)
    kind = "logo" if req.url.path.endswith("/logo") else "card"
    try:
        if raw is None:
            raise photos.BadPhoto("no file")
        processed = await run_in_threadpool(photos.process, raw, kind)
    except (photos.BadPhoto, photos.Busy) as e:
        busy = isinstance(e, photos.Busy)
        try:
            return FtResponse(
                await run_in_threadpool(page, BUSY if busy else BAD_PHOTO),
                status_code=503 if busy else 400,
                headers={"Retry-After": "30"} if busy else None,
            )
        except PermissionError:
            return Refusal(403)
    try:
        await run_in_threadpool(save, processed)
    except PermissionError:
        return Refusal(403)
    return Redirect(done)


def _org_page_with(uid, org_id, error):
    if org_id not in data.coordinator_org_ids(uid):
        raise PermissionError("not a member")
    return OrganizationPage(uid, errors={org_id: (None, error)})


@ar("/org/organization/{org_id}/logo", methods=["POST"])
async def save_logo(req, org_id: int):
    uid = req.scope["uid"]
    return await _upload(
        req,
        partial(data.coordinator_org, uid, org_id),
        partial(data.set_org_picture, uid, org_id, "logo"),
        partial(_org_page_with, uid, org_id),
        "/org/organization?notice=saved",
    )


@ar("/org/organization/{org_id}/photo", methods=["POST"])
async def save_org_photo(req, org_id: int):
    uid = req.scope["uid"]
    return await _upload(
        req,
        partial(data.coordinator_org, uid, org_id),
        partial(data.set_org_picture, uid, org_id, "photo"),
        partial(_org_page_with, uid, org_id),
        "/org/organization?notice=saved",
    )


# --- an opening's photo and event link ---------------------------------------------------


def OpeningMediaPage(uid, opening_id, error=None, link=None, notice=""):
    o = data.coordinator_opening(uid, opening_id)
    base = f"/org/openings/{o.id}"
    return OrgShell(
        "openings",
        data.org_waiting(uid),
        A("Back to your openings", href="/org", cls=SMALL_LINK + " mb-2 text-sm"),
        H1(o.title, cls=ORG_TITLE + " pb-4"),
        Alert(error, cls=AlertT.error + " mb-4") if error else None,
        Alert(NOTICE[notice], cls=AlertT.info + " mb-4") if notice in NOTICE else None,
        Div(
            PictureCard(
                "Photo",
                "The top of this opening's card. Without one, the card shows your "
                "organization's photo.",
                o.photo_key,
                f"{base}/photo",
                "opening-photo",
            ),
            PlainForm(
                H2("Event link", cls="font-display text-lg font-extrabold text-ink"),
                P(
                    "Your own page for this event or volunteer role. The card shows "
                    '"See it on" and its domain, and opens it in a new tab.',
                    cls=TextPresets.muted_sm,
                ),
                Field(
                    "Link (https://, optional)",
                    TextInput(
                        name="event_url",
                        type="url",
                        inputmode="url",
                        value=(o.event_url or "") if link is None else link,
                        placeholder="https://",
                    ),
                ),
                Div(EventLink(o.event_url)) if o.event_url else None,
                Button("Save link", cls=ButtonT.primary),
                action=f"{base}/link",
                method="post",
                cls="space-y-3 p-5 " + CARD,
            ),
            cls="grid gap-4 md:grid-cols-2",
        ),
        Script(src="/static/photo.js", defer=True),
    )


@ar("/org/openings/{opening_id}/media", methods=["GET"])
def opening_media(req, opening_id: int):
    try:
        return OpeningMediaPage(
            req.scope["uid"], opening_id, notice=query(req, "notice")
        )
    except PermissionError:
        return Refusal(403)


@ar("/org/openings/{opening_id}/photo", methods=["POST"])
async def save_opening_photo(req, opening_id: int):
    uid = req.scope["uid"]
    return await _upload(
        req,
        partial(data.coordinator_opening, uid, opening_id),
        partial(data.set_opening_photo, uid, opening_id),
        partial(OpeningMediaPage, uid, opening_id),
        f"/org/openings/{opening_id}/media?notice=saved",
    )


@ar("/org/openings/{opening_id}/link", methods=["POST"])
async def save_opening_link(req, opening_id: int):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(
        _sync_save_link, req.scope["uid"], opening_id, form.get("event_url", "").strip()
    )


def _sync_save_link(uid, opening_id, raw):
    url = https_url(raw) if raw else None
    try:
        if raw and not url:
            return FtResponse(
                OpeningMediaPage(uid, opening_id, BAD_LINK, link=raw), status_code=400
            )
        data.set_opening_link(uid, opening_id, url)
    except PermissionError:
        return Refusal(403)
    return Redirect(f"/org/openings/{opening_id}/media?notice=saved")
