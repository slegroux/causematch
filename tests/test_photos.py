"""Photos: upload, re-encoding (no metadata survives), the size and bomb limits, and who
may see a volunteer's photo. Every picture here is generated; none is of a real person."""

import io
import struct
import zlib

import pytest
from conftest import TestClient, sign_in
from PIL import ExifTags, Image
from test_claim import admin_client

from app import data, photos, security
from app.db import matches, openings, profiles
from main import app


def jpeg(size=(1200, 900), exif=None, color=(200, 90, 60)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", exif=exif or Image.Exif())
    return buf.getvalue()


def with_gps():
    """A JPEG that says where and with what it was taken."""
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "SecretCam"
    exif[ExifTags.Base.Model] = "Model 7"
    exif.get_ifd(ExifTags.IFD.GPSInfo).update(
        {
            ExifTags.GPS.GPSLatitudeRef: "N",
            ExifTags.GPS.GPSLatitude: (34.0, 3.0, 30.0),
            ExifTags.GPS.GPSLongitudeRef: "W",
            ExifTags.GPS.GPSLongitude: (118.0, 14.0, 0.0),
        }
    )
    raw = jpeg(exif=exif)
    assert Image.open(io.BytesIO(raw)).getexif().get_ifd(ExifTags.IFD.GPSInfo)
    return raw


def bomb_png(side=20000):
    """A valid 1-bit PNG, side x side: about 50 KB that decodes to 400 million pixels."""

    def chunk(kind, body):
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body))
        )

    z = zlib.compressobj(9)
    row = b"\0" * (1 + side // 8)  # filter byte, then the row's bits
    idat = b"".join(z.compress(row) for _ in range(side)) + z.flush()
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 1, 0, 0, 0, 0))
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def upload(client, raw, name="photo.jpg", kind="image/jpeg"):
    return client.post(
        "/me/profile/photo",
        files={"photo": (name, raw, kind)},
        follow_redirects=False,
    )


def uid_of(client):
    return data.get_or_create_volunteer(client.phone)


def key_of(client):
    return profiles[uid_of(client)].photo_key


@pytest.fixture
def other():
    """A second onboarded volunteer."""
    from conftest import FORM

    c = TestClient(app)
    sign_in(c)
    assert c.post("/onboarding", data=FORM, follow_redirects=False).status_code == 303
    return c


# --- upload and re-encoding ----------------------------------------------------------------


def test_a_photo_comes_back_as_a_small_jpeg_with_no_exif(user):
    r = upload(user, with_gps())
    assert r.status_code == 303 and r.headers["location"] == "/me/profile?notice=saved"
    key = key_of(user)
    assert photos.is_key(key)
    r = user.get(f"/photos/{key}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert b"Exif" not in r.content and b"SecretCam" not in r.content
    im = Image.open(io.BytesIO(r.content))
    assert im.format == "JPEG" and max(im.size) == photos.FULL
    assert not im.getexif() and "exif" not in im.info
    thumb = Image.open(io.BytesIO(user.get(f"/photos/thumb/{key}.jpg").content))
    assert thumb.size == (photos.THUMB, photos.THUMB) and not thumb.getexif()


def test_a_sideways_phone_photo_is_turned_upright():
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6  # the camera was turned: rotate 90 degrees
    out = photos.process(jpeg((1200, 600), exif=exif))
    assert Image.open(io.BytesIO(out.full)).size == (400, 800)


def test_transparent_png_and_webp_are_accepted(user):
    for fmt, kind in (("PNG", "image/png"), ("WEBP", "image/webp")):
        buf = io.BytesIO()
        Image.new("RGBA", (300, 300), (0, 120, 0, 0)).save(buf, fmt)
        assert upload(user, buf.getvalue(), "p", kind).status_code == 303, fmt


def test_a_photo_is_served_privately_with_an_etag(user):
    upload(user, jpeg())
    url = f"/photos/{key_of(user)}.jpg"
    r = user.get(url)
    assert r.headers["cache-control"] == "private, max-age=86400"
    etag = r.headers["etag"]
    again = user.get(url, headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""


def test_a_new_photo_replaces_the_old_one(user):
    upload(user, jpeg())
    old = key_of(user)
    upload(user, jpeg(color=(10, 20, 200)))
    assert key_of(user) != old
    assert user.get(f"/photos/{old}.jpg").status_code == 404


def test_removing_the_photo_deletes_it(user):
    upload(user, jpeg())
    key = key_of(user)
    r = user.post("/me/profile/photo/remove", follow_redirects=False)
    assert r.headers["location"] == "/me/profile?notice=removed"
    assert key_of(user) is None and user.get(f"/photos/{key}.jpg").status_code == 404


def test_the_profile_page_shows_the_photo_and_the_form(user):
    page = user.get("/me/profile").text
    assert 'enctype="multipart/form-data"' in page and "Add a photo" in page
    assert '<script src="/static/photo.js"' in page
    upload(user, jpeg())
    key = key_of(user)
    assert f"/photos/thumb/{key}.jpg" in user.get("/me/profile").text
    assert f"/photos/thumb/{key}.jpg" in user.get("/profile").text  # the Me hub


# --- refused uploads -----------------------------------------------------------------------


def test_a_decompression_bomb_is_refused(user):
    raw = bomb_png()
    assert len(raw) < 200_000
    r = upload(user, raw, "bomb.png", "image/png")
    assert r.status_code == 400 and "couldn't read that picture" in r.text
    assert key_of(user) is None


def test_a_text_file_is_refused(user):
    r = upload(user, b"just some words, not a picture\n", "notes.txt", "text/plain")
    assert r.status_code == 400 and key_of(user) is None


def test_a_form_with_no_file_is_refused(user):
    r = user.post("/me/profile/photo", data={"photo": "x"}, follow_redirects=False)
    assert r.status_code == 400 and key_of(user) is None


def test_over_6_mb_is_413(user):
    r = upload(user, b"\xff" * (7 * 1024 * 1024))
    assert r.status_code == 413 and "Photo too large" in r.text
    assert key_of(user) is None


def test_a_chunked_upload_over_6_mb_is_413(user):
    head = (
        b'--b\r\nContent-Disposition: form-data; name="photo"; filename="a.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\n"
    )
    body = iter([head] + [b"\xff" * (1024 * 1024)] * 7 + [b"\r\n--b--\r\n"])
    r = user.post(
        "/me/profile/photo",
        content=body,
        headers={"Content-Type": "multipart/form-data; boundary=b"},
    )
    assert r.status_code == 413 and key_of(user) is None


def test_only_upload_routes_take_more_than_64_kb(user):
    assert security.max_body("/me/profile/photo") == photos.MAX_UPLOAD
    for path in ("/me/profile/photo/remove", "/onboarding", "/me/profile/photox"):
        assert security.max_body(path) == security.MAX_BODY, path
    r = user.post("/me/profile/photo/remove", content=b"x" * 70_000)
    assert r.status_code == 413


def test_the_csp_lets_only_blob_previews_in():
    """photo.js previews a picture as a blob: URL; that is the CSP's one addition."""
    img = next(d for d in security.csp().split("; ") if d.startswith("img-src"))
    assert img == "img-src 'self' data: blob:"
    assert security.csp().count("blob:") == 1


# --- who may see a volunteer's photo -------------------------------------------------------


def test_another_volunteer_gets_404(user, other):
    upload(user, jpeg())
    key = key_of(user)
    assert other.get(f"/photos/{key}.jpg").status_code == 404
    assert other.get(f"/photos/thumb/{key}.jpg").status_code == 404


def test_signed_out_is_sent_to_sign_in(user, anon):
    upload(user, jpeg())
    r = anon.get(f"/photos/{key_of(user)}.jpg", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.parametrize("key", ["nope", "a" * 22, "../../etc/passwd"])
def test_an_unknown_key_is_404(user, key):
    assert user.get(f"/photos/{key}.jpg").status_code == 404


def _liked(user, org_id):
    opening = next(o for o in openings() if o.org_id == org_id and o.status == "open")
    return data.swipe(uid_of(user), opening.id, True)


def test_a_coordinator_sees_it_after_a_like_on_their_opening(user, coord):
    upload(user, jpeg())
    url = f"/photos/{key_of(user)}.jpg"
    assert coord.get(url).status_code == 404  # no like yet
    m = _liked(user, coord.org_id)
    assert coord.get(url).status_code == 200
    matches.update(id=m.id, org_blind=True)  # liked after the org blocked them
    assert coord.get(url).status_code == 404


def test_a_coordinator_of_another_org_gets_404(user, coord):
    from conftest import coordinator_for, org_sign_in

    upload(user, jpeg())
    _liked(user, coord.org_id)
    _, email = coordinator_for(1)
    elsewhere = TestClient(app)
    org_sign_in(elsewhere, email)
    assert elsewhere.get(f"/photos/{key_of(user)}.jpg").status_code == 404


def test_a_block_hides_it_from_the_org(user, coord):
    upload(user, jpeg())
    m = _liked(user, coord.org_id)
    data.block_org(uid_of(user), m.id)
    assert coord.get(f"/photos/{key_of(user)}.jpg").status_code == 404


def test_an_admin_sees_it(user):
    upload(user, jpeg())
    assert admin_client().get(f"/photos/{key_of(user)}.jpg").status_code == 200


# --- the matrix: which of an org's matches show it the photo -------------------------------


def _coord_uid():
    from conftest import coordinator_for

    return data.coordinator_by_email(coordinator_for(0)[1])


def _no_like(user, m):
    matches.delete(m.id)


def _pending(user, m):
    pass


def _accepted(user, m):
    data.decide(_coord_uid(), m.id, True)


def _declined(user, m):
    data.decide(_coord_uid(), m.id, False)


def _withdrawn_after_accept(user, m):
    data.decide(_coord_uid(), m.id, True)
    data.withdraw(uid_of(user), m.id)


def _withdrawn_while_pending(user, m):
    data.withdraw(uid_of(user), m.id)


def _org_blind(user, m):
    matches.update(id=m.id, org_blind=True)


def _volunteer_blocked_org(user, m):
    data.block_org(uid_of(user), m.id)


def _org_blocked_volunteer(user, m):
    data.decide(_coord_uid(), m.id, True)
    data.block_volunteer(_coord_uid(), m.id)


def _suspended(user, m):
    profiles.update(id=uid_of(user), suspended_at="2026-10-15T00:00:00")


@pytest.mark.parametrize(
    ("state", "seen"),
    [
        (_no_like, False),
        (_pending, True),
        (_accepted, True),
        (_declined, True),
        (_withdrawn_after_accept, True),  # still in the org's past matches
        (_withdrawn_while_pending, False),  # never reached the org's inbox
        (_org_blind, False),
        (_volunteer_blocked_org, False),
        (_org_blocked_volunteer, False),
        (_suspended, False),
    ],
    ids=lambda v: v.__name__.strip("_") if callable(v) else str(v),
)
def test_the_org_sees_the_photo_only_for_a_match_it_can_see(user, coord, state, seen):
    upload(user, jpeg())
    key = key_of(user)
    m = _liked(user, coord.org_id)
    state(user, m)
    for url in (f"/photos/{key}.jpg", f"/photos/thumb/{key}.jpg"):
        assert coord.get(url).status_code == (200 if seen else 404), url
    rows = [r for box in data.org_inbox(_coord_uid()).values() for r in box]
    # the inbox never links a photo the route would refuse
    assert all(r["photo_key"] in (None, key) for r in rows)
    if not seen:
        assert all(r["photo_key"] is None for r in rows)


def test_the_inbox_chat_and_shifts_show_the_photo(user, coord):
    upload(user, jpeg())
    key = key_of(user)
    m = _liked(user, coord.org_id)
    thumb = f"/photos/thumb/{key}.jpg"
    assert thumb in coord.get("/org/inbox").text
    data.decide(_coord_uid(), m.id, True)
    assert thumb in coord.get(f"/org/chat/{m.id}").text
    assert thumb in coord.get("/org/shifts").text
    assert thumb not in user.get(f"/chat/{m.id}").text  # the volunteer sees the org
