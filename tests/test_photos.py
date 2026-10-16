"""Photos: upload, re-encoding (no metadata survives), the size and bomb limits, and who
may see a volunteer's photo. Every picture here is generated; none is of a real person."""

import ast
import io
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import warnings
import zlib

import pytest
from conftest import FORM, ROOT, TestClient, admin_client, jpeg, sign_in, uid_of
from PIL import ExifTags, Image

from app import data, photos, security
from app.db import matches, openings, profiles
from main import app


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
    # asked for again on every view, so a block hides it at once
    assert r.headers["cache-control"] == "private, no-cache"
    assert r.headers["cross-origin-resource-policy"] == "same-origin"
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


# --- lifecycle and moderation --------------------------------------------------------------


def test_deleting_the_account_deletes_the_photos(user):
    from app.db import photos as photo_rows

    upload(user, jpeg())
    uid, key = uid_of(user), key_of(user)
    assert user.post("/profile/delete", follow_redirects=False).status_code == 303
    assert profiles[uid].photo_key is None
    assert not photo_rows(
        where="owner_kind = 'volunteer' AND owner_id = ?", where_args=[uid]
    )
    assert admin_client().get(f"/photos/{key}.jpg").status_code == 404


def test_a_suspended_volunteers_photo_is_hidden_but_not_from_admins(user, coord):
    upload(user, jpeg())
    key = key_of(user)
    data.decide(_coord_uid(), _liked(user, coord.org_id).id, True)
    assert coord.get(f"/photos/{key}.jpg").status_code == 200
    profiles.update(id=uid_of(user), suspended_at="2026-10-15T00:00:00")
    assert coord.get(f"/photos/{key}.jpg").status_code == 404
    assert admin_client().get(f"/photos/{key}.jpg").status_code == 200  # to review it


def _photo_report(user, coord):
    upload(user, jpeg())
    m = _liked(user, coord.org_id)
    r = coord.post(
        f"/org/matches/{m.id}/report",
        data={"reason": "inappropriate_profile", "details": "The photo."},
        follow_redirects=False,
    )
    assert r.status_code == 303
    from app.db import reports

    (rep,) = reports()
    return rep


def test_an_admin_can_remove_a_reported_photo(user, coord):
    rep = _photo_report(user, coord)
    key = key_of(user)
    admin = admin_client()
    page = admin.get("/admin/reports").text
    assert "Inappropriate photo or profile" in page
    assert f"/photos/thumb/{key}.jpg" in page and "Remove photo" in page
    r = admin.post(f"/admin/reports/{rep.id}/remove-photo", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/reports"
    from app.db import reports

    assert reports[rep.id].status == "actioned" and key_of(user) is None
    assert admin.get(f"/photos/{key}.jpg").status_code == 404
    assert "Remove photo" not in admin.get("/admin/reports").text
    assert upload(user, jpeg()).status_code == 303  # they may add another


def test_remove_photo_needs_a_report_on_a_volunteer(user, coord):
    from app.db import reports

    upload(user, jpeg())
    m = _liked(user, coord.org_id)
    user.post(f"/matches/{m.id}/report", data={"reason": "spam"})
    (rep,) = reports()  # names the org
    r = admin_client().post(f"/admin/reports/{rep.id}/remove-photo")
    assert r.status_code == 400 and reports[rep.id].status == "open"
    assert key_of(user)


def test_only_an_admin_can_remove_a_photo(user, coord):
    rep = _photo_report(user, coord)
    for who in (user, coord):
        r = who.post(f"/admin/reports/{rep.id}/remove-photo", follow_redirects=False)
        assert (
            r.status_code in (303, 403)
            and r.headers.get("location") != "/admin/reports"
        )
    assert key_of(user)


# --- the deck's "Add a photo" nudge --------------------------------------------------------


def test_the_deck_nudges_for_a_photo_until_one_is_added(user):
    assert "Add a photo so nonprofits know" in user.get("/deck").text
    upload(user, jpeg())
    assert "Add a photo so nonprofits know" not in user.get("/deck").text


def test_the_nudge_can_be_dismissed(user):
    r = user.post("/deck/photo-nudge/dismiss", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/deck"
    assert "Add a photo so nonprofits know" not in user.get("/deck").text


# --- decode memory, decode slots and upload throttling ------------------------------------


GENERATE = r"""
import sys
from PIL import Image, ImageDraw
out = sys.argv[1]
Image.new("RGB", (7000, 7000), (200, 90, 60)).save(f"{out}/7000.webp", "WEBP", lossless=True)
Image.new("RGBA", (7000, 7000), (200, 90, 60, 255)).save(f"{out}/7000-rgba.png")
Image.new("1", (7000, 7000)).save(f"{out}/7000-1bit.gif")
Image.new("L", (7000, 7000), 128).save(f"{out}/7000-grey.png")
Image.new("RGB", (7000, 7000), (200, 90, 60)).save(f"{out}/7000.jpg", quality=80)
Image.new("RGB", (3460, 2310), (20, 90, 160)).save(f"{out}/8mp.webp", "WEBP", lossless=True)
im = Image.new("RGBA", (4000, 3000), (0, 0, 0, 0))
for x in range(0, 4000, 200):
    ImageDraw.Draw(im).rectangle((x, 0, x + 100, 3000), fill=(x % 255, 90, 160, 255))
im.save(f"{out}/12mp-rgba.png")
# progressive JPEGs: libjpeg keeps every coefficient of a multi-scan file
stripes = Image.new("RGB", (7071, 7071), (40, 120, 200))
for x in range(0, 7071, 400):
    ImageDraw.Draw(stripes).rectangle((x, 0, x + 150, 7071), fill=(200, 60, 30))
stripes.save(f"{out}/prog-444.jpg", progressive=True, subsampling=0, quality=80)
stripes.convert("CMYK").save(f"{out}/prog-cmyk.jpg", progressive=True, quality=80)
stripes.resize((4240, 2830)).save(
    f"{out}/prog-12mp.jpg", progressive=True, subsampling=0, quality=80
)
stripes.convert("L").save(f"{out}/grey-prog.jpg", progressive=True, quality=80)
# 6000 x 6000 x 1 component = the coefficient budget exactly: accepted, and its decode
# holds a 72 MB coefficient buffer whatever draft() asks for
stripes.convert("L").resize((6000, 6000)).save(
    f"{out}/prog-grey-36mp.jpg", progressive=True, quality=80
)
"""
MEASURE = r"""
import resource, sys
from pathlib import Path
from app import photos
raw = Path(sys.argv[1]).read_bytes()
def peak():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024  # bytes on macOS, KB on Linux
before = peak()
try:
    photos.process_here(raw, sys.argv[2])  # in this process: its memory is measured
    result = "ok"
except photos.BadPhoto:
    result = "refused"
print(result, (peak() - before) // 2**20)
"""


@pytest.fixture(scope="module")
def crafted(tmp_path_factory):
    """The reviewer's files: tiny uploads that claim huge bitmaps. Made in another
    process, so making them doesn't raise the peak the measurements read."""
    out = tmp_path_factory.mktemp("crafted")
    subprocess.run([sys.executable, "-c", GENERATE, str(out)], check=True)
    (out / "many-scans.jpg").write_bytes(repeat_last_scan(out / "grey-prog.jpg", 50))
    (out / "one-dc-scan.jpg").write_bytes(
        first_scan_only((out / "prog-444.jpg").read_bytes())
    )
    (out / "one-component-scan.jpg").write_bytes(
        one_component_scan((out / "7000.jpg").read_bytes())
    )
    return out


def first_scan_only(raw):
    """A progressive JPEG cut after its first scan (a DC scan over every component):
    one scan, but libjpeg still keeps the whole-image coefficient buffer."""
    _, end = scan_bounds(raw)[0]
    return raw[:end] + b"\xff\xd9"


def one_component_scan(raw):
    """A baseline JPEG whose only scan names 1 of its 3 components (non-interleaved):
    libjpeg buffers the whole image's coefficients for it too."""
    start, _ = scan_bounds(raw)[0]
    length = int.from_bytes(raw[start + 2 : start + 4], "big")
    component, tables = raw[start + 5], raw[start + 6]
    header = b"\xff\xda\x00\x08\x01" + bytes([component, tables, 0, 63, 0])
    return raw[:start] + header + raw[start + 2 + length :]


def scan_bounds(raw):
    """[(start, end)] of each SOS segment and its entropy-coded data in a JPEG."""
    i, out = 2, []
    while raw[i + 1] != 0xD9:
        start, marker = i, raw[i + 1]
        i += 2 + int.from_bytes(raw[i + 2 : i + 4], "big")
        if marker == 0xDA:
            while raw[(j := raw.find(b"\xff", i)) + 1] in (0, *range(0xD0, 0xD8)):
                i = j + 2
            i = j
            out.append((start, i))
    return out


def repeat_last_scan(path, times):
    """The JPEG with its last scan repeated: more passes, nearly no more bytes each."""
    raw = path.read_bytes()
    start, end = scan_bounds(raw)[-1]
    return raw[:end] + raw[start:end] * times + raw[end:]


def peak_mb(path, kind):
    """(result, peak RSS growth in MB) of photos.process in a fresh process."""
    r = subprocess.run(
        [sys.executable, "-c", MEASURE, str(path), kind],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=True,
    )
    result, mb = r.stdout.split()
    return result, int(mb)


@pytest.mark.parametrize(
    ("name", "kind", "expected"),
    [
        ("7000.webp", "volunteer", "refused"),  # 2 KB, was +833 MB
        ("7000.webp", "logo", "refused"),  # was +1174 MB
        ("7000-rgba.png", "logo", "refused"),  # was +806 MB
        ("7000-1bit.gif", "volunteer", "refused"),  # was +439 MB
        ("7000-grey.png", "volunteer", "refused"),  # was +439 MB
        ("7000.jpg", "volunteer", "ok"),  # a JPEG decodes at a smaller scale
        ("8mp.webp", "card", "ok"),  # just under WebP's cap
        ("12mp-rgba.png", "logo", "ok"),  # just under PNG's cap
        ("prog-444.jpg", "volunteer", "refused"),  # 50 MP, was +384 MB
        ("prog-cmyk.jpg", "volunteer", "refused"),  # was +527 MB
        ("many-scans.jpg", "volunteer", "refused"),  # 56 scans, before any decoding
        ("prog-12mp.jpg", "card", "ok"),  # 12 MP x 3 components: at the budget
        ("one-dc-scan.jpg", "volunteer", "refused"),  # progressive with one scan
        ("one-component-scan.jpg", "volunteer", "refused"),  # SOF0, 1 of 3 in its scan
    ],
)
def test_a_huge_bitmap_never_costs_more_than_200_mb(crafted, name, kind, expected):
    result, mb = peak_mb(crafted / name, kind)
    assert result == expected
    assert mb < (50 if expected == "refused" else 200), mb


def test_a_third_decode_waits_for_one_of_two_to_finish():
    held = [photos._decoding.acquire(timeout=1) for _ in range(2)]
    assert all(held)
    done = threading.Event()
    t = threading.Thread(target=lambda: (photos.process(jpeg()), done.set()))
    t.start()
    try:
        assert not done.wait(0.5)  # both slots are taken
    finally:
        photos._decoding.release()
        photos._decoding.release()
    assert done.wait(10)
    t.join()


def test_the_bomb_check_leaves_warning_filters_alone():
    """Process-wide warning filters are not thread-safe: the size check is explicit."""
    before = list(warnings.filters)
    threads = [
        threading.Thread(target=photos.process, args=(jpeg((600, 400)),))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with pytest.raises(photos.BadPhoto):
        photos.process(bomb_png())
    assert list(warnings.filters) == before


def test_uploads_are_throttled(user):
    for _ in range(10):
        assert upload(user, jpeg((200, 200))).status_code == 303
    r = upload(user, jpeg((200, 200)))
    assert r.status_code == 429 and "retry-after" in r.headers


def test_an_upload_is_exactly_one_photo_file(user):
    two = [
        ("photo", ("a.jpg", jpeg(), "image/jpeg")),
        ("photo", ("b.jpg", jpeg(), "image/jpeg")),
    ]
    assert user.post("/me/profile/photo", files=two).status_code == 400
    r = user.post(
        "/me/profile/photo",
        data={"extra": "x"},
        files={"photo": ("a.jpg", jpeg(), "image/jpeg")},
    )
    assert r.status_code == 400
    other = {"picture": ("a.jpg", jpeg(), "image/jpeg")}
    assert user.post("/me/profile/photo", files=other).status_code == 400
    assert key_of(user) is None


def test_another_orgs_upload_is_refused_before_decoding(coord, monkeypatch):
    from conftest import coordinator_for, org_sign_in

    from app.db import openings, orgs

    def never(*a, **kw):
        raise AssertionError("decoded a picture for a coordinator of another org")

    _, email = coordinator_for(1)
    elsewhere = TestClient(app)
    org_sign_in(elsewhere, email)
    monkeypatch.setattr(photos, "process", never)
    opening = next(o for o in openings() if o.org_id == coord.org_id)
    for path in (
        f"/org/organization/{coord.org_id}/logo",
        f"/org/organization/{coord.org_id}/photo",
        f"/org/openings/{opening.id}/photo",
    ):
        r = elsewhere.post(path, files={"photo": ("a.jpg", jpeg(), "image/jpeg")})
        assert r.status_code == 403, path
    assert orgs[coord.org_id].logo_key is None


# --- JPEG scans and decode slots ---------------------------------------------------------


def test_scans_are_counted_by_the_marker_stream():
    progressive = io.BytesIO()
    Image.new("RGB", (64, 64), (9, 9, 9)).save(progressive, "JPEG", progressive=True)
    raw = progressive.getvalue()
    n = len(scan_bounds(raw))
    assert n > 1 and photos.jpeg_frame(raw) == (0xC2, n, 3)  # first: DC of all three
    assert photos.jpeg_frame(jpeg((64, 64))) == (0xC0, 1, 3)
    # bytes where a marker should be are refused outright (libjpeg would skip them)
    start, _ = scan_bounds(raw)[1]
    with pytest.raises(photos.BadPhoto, match="marker should be"):
        photos.jpeg_frame(raw[:start] + b"\x00\x00" + raw[start:])
    # FF FF 00 inside entropy data is a stuffed FF, as in libjpeg: it hides no scan
    _, first_end = scan_bounds(raw)[0]
    padded = raw[:first_end] + b"\xff\xff\x00\x00\x10" + raw[first_end:]
    assert photos.jpeg_frame(padded).scans == n
    s, e = scan_bounds(raw)[-1]
    many = raw[:e] + raw[s:e] * 60 + raw[e:]
    assert photos.jpeg_frame(many).scans == photos.MAX_SCANS + 1  # stops counting there
    with pytest.raises(photos.BadPhoto, match="scans"):
        photos.process(many)


def test_an_upload_waits_at_most_a_few_seconds_for_a_decode_slot(user, monkeypatch):
    monkeypatch.setattr(photos, "WAIT", 0.2)
    held = [photos._decoding.acquire(timeout=1) for _ in range(2)]
    assert all(held)
    try:
        with pytest.raises(photos.Busy):
            photos.process(jpeg())
        r = upload(user, jpeg())
        assert r.status_code == 503 and "Try again in a minute" in r.text
        assert r.headers["retry-after"] == "30"
    finally:
        photos._decoding.release()
        photos._decoding.release()
    assert upload(user, jpeg()).status_code == 303


@pytest.mark.parametrize("blocker", ["volunteer", "org"])
def test_after_a_block_the_org_sees_no_profile_details(user, coord, blocker):
    uid = uid_of(user)
    profiles.update(
        id=uid, last_name="Lee", pronouns="they/them", bio="Early riser.",
        interests='["cooking"]',
    )  # fmt: skip
    upload(user, jpeg())
    m = _liked(user, coord.org_id)
    data.decide(_coord_uid(), m.id, True)
    assert "Early riser." in coord.get("/org/inbox").text
    if blocker == "volunteer":
        data.block_org(uid, m.id)
    else:
        data.block_volunteer(_coord_uid(), m.id)
    profiles.update(id=uid, bio="Edited after the block.")
    # answering onboarding again can't push new answers to the org either
    r = user.post(
        "/onboarding",
        data={**FORM, "first_name": "Renamed", "neighborhood": "Echo Park NEW"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/deck"
    assert profiles[uid].neighborhood == "Chinatown"
    # nor can a first name changed after the block: the org sees "Volunteer"
    r = user.post(
        "/me/profile/details",
        data={"first_name": "Renamed", "last_name": "", "pronouns": "", "bio": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303 and profiles[uid].display_name == "Renamed"
    rows = [r for box in data.org_inbox(_coord_uid()).values() for r in box]
    assert rows, "the past match stays listed"
    for page in ("/org/inbox", f"/org/chat/{m.id}", f"/org/matches/{m.id}/report"):
        html = coord.get(page).text
        for hidden in (
            "Early riser.",
            "Edited after",
            "they/them",
            "Cooking",
            "Lee",
            "Chinatown",  # the answers too: the volunteer can still change them
            "Food insecurity",
            "Weekend mornings",
            "Renamed",
        ):
            assert hidden not in html, (page, hidden)
        assert "/photos/" not in html, page
    (row,) = rows
    assert row["name"] == "Volunteer" and row["bio"] is None and row["interests"] == []
    assert (row["neighborhood"], row["frequency"]) == (None, None)
    assert row["causes"] == [] and row["availability"] == []


def test_only_baseline_extended_and_progressive_jpegs_are_read():
    raw = jpeg((64, 64))
    sof = raw.index(b"\xff\xc0")
    for marker in (0xC3, 0xC5, 0xC9, 0xCB):  # lossless, hierarchical, arithmetic
        crafted = raw[: sof + 1] + bytes([marker]) + raw[sof + 2 :]
        assert photos.jpeg_frame(crafted).sof == marker
        with pytest.raises(photos.BadPhoto, match="unsupported"):
            photos.process(crafted)
    assert photos.jpeg_frame(one_component_scan(raw)) == (0xC0, 1, 1)


# --- a header walk that fails closed, and the decode in a child ---------------------------


def _decoy(segment):
    """FF 00, then a length a careless parser would jump by, landing on `segment` hidden
    in a COM that libjpeg skips: libjpeg discards the FF 00 and the two bytes after it."""
    com = b"\xff\xfe" + (2 + len(segment)).to_bytes(2, "big") + segment
    return b"\xff\x00\x00\x06" + com


def test_ff_00_cant_hide_a_non_interleaved_scan():
    raw = one_component_scan(jpeg((64, 64)))
    start, _ = scan_bounds(raw)[0]
    decoy = b"\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
    probe = raw[:start] + _decoy(decoy) + raw[start:]
    Image.open(io.BytesIO(probe))  # Pillow reads its header: a real divergence
    with pytest.raises(photos.BadPhoto, match="out of place"):
        photos.jpeg_frame(probe)
    with pytest.raises(photos.BadPhoto):
        photos.process(probe)


def test_ff_00_cant_hide_an_unsupported_frame():
    raw = jpeg((64, 64))
    sof = raw.index(b"\xff\xc0")
    decoy = raw[sof : sof + 2 + int.from_bytes(raw[sof + 2 : sof + 4], "big")]
    lossless = raw[: sof + 1] + b"\xc3" + raw[sof + 2 :]
    probe = lossless[:sof] + _decoy(decoy) + lossless[sof:]
    with pytest.raises(photos.BadPhoto):
        photos.jpeg_frame(probe)
    with pytest.raises(photos.BadPhoto):
        photos.process(probe)


def _cut_short(raw, start, end):
    return raw[: end - 10]


def _soi_again(raw, start, end):
    return raw[:end] + b"\xff\xd8\xff\xd9"


def _stray_byte(raw, start, end):
    return raw[:start] + b"\x00" + raw[start:]


def _scan_first(raw, start, end):
    sof = raw.index(b"\xff\xc0")
    return raw[:sof] + raw[start:end] + raw[sof:]


@pytest.mark.parametrize("damage", [_cut_short, _soi_again, _stray_byte, _scan_first])
def test_a_malformed_marker_stream_is_refused(damage):
    raw = jpeg((64, 64))
    start, end = scan_bounds(raw)[0]
    with pytest.raises(photos.BadPhoto):
        photos.jpeg_frame(damage(raw, start, end))


def test_the_decode_runs_in_a_child_and_returns_two_jpegs():
    out = photos.process(jpeg((1600, 1200)))
    assert out.full[:2] == b"\xff\xd8" and Image.open(io.BytesIO(out.full)).size == (
        800,
        600,
    )
    assert Image.open(io.BytesIO(out.thumb)).size == (160, 160)


def test_a_decode_that_runs_too_long_is_killed(monkeypatch):
    """The test stands a sleeping child in for the decoder: the timeout, not a file."""
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    monkeypatch.setattr(photos, "worker_command", lambda kind: sleeper)
    monkeypatch.setattr(photos, "WORKER_TIMEOUT", 1)
    started = time.monotonic()
    with pytest.raises(photos.BadPhoto, match="too long"):
        photos.process(jpeg())
    assert time.monotonic() - started < 10
    assert photos._decoding.acquire(timeout=0) and photos._decoding.acquire(timeout=0)
    photos._decoding.release()
    photos._decoding.release()


@pytest.mark.parametrize(
    "script",
    [
        "import sys; sys.exit(1)",  # crashed, or was killed by a limit
        "import sys; sys.stdout.buffer.write(b'<html>' * 4)",  # answered something else
        "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
    ],
    ids=["exit-1", "not-jpeg", "killed"],
)
def test_a_failed_child_is_a_bad_photo(monkeypatch, script):
    monkeypatch.setattr(
        photos, "worker_command", lambda kind: [sys.executable, "-c", script]
    )
    with pytest.raises(photos.BadPhoto):
        photos.process(jpeg())


def test_a_picture_at_the_caps_fits_the_childs_limits(crafted):
    """On Linux this runs under RLIMIT_AS and RLIMIT_CPU: the limits leave room for a
    real decode at the caps."""
    out = photos.process((crafted / "prog-12mp.jpg").read_bytes(), "card")
    assert Image.open(io.BytesIO(out.full)).size == photos.CARD
    assert photos.process((crafted / "12mp-rgba.png").read_bytes(), "logo").full


def worker_report(err):
    """The worker's stderr lines as {first word: {key: value}} (app/photo_worker.py)."""
    out = {}
    for line in err.decode(errors="replace").splitlines():
        word, *pairs = line.split()
        out[word] = dict(p.split("=", 1) for p in pairs if "=" in p)
    return out


def run_capped(raw, kind, memory, monkeypatch):
    monkeypatch.setattr(photos, "WORKER_MEMORY", memory)
    code, out, err = photos._run_worker(kind, raw)
    return code, out, worker_report(err), err


# The worst files photos.process accepts, by kind of decode (test_a_huge_bitmap_...)
WORST_ACCEPTED = [
    ("prog-grey-36mp.jpg", "volunteer"),  # a 72 MB coefficient buffer
    ("prog-12mp.jpg", "card"),
    ("7000.jpg", "volunteer"),  # 49 MP baseline, decoded at reduced scale
    ("8mp.webp", "card"),
    ("12mp-rgba.png", "logo"),
]


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="macOS doesn't enforce RLIMIT_AS; production (Cloud Run) is Linux, where "
    "this runs",
)
def test_the_childs_memory_is_capped(monkeypatch, crafted):
    """RLIMIT_AS is set in the child and stops a decode that needs more: under a cap of
    the address space after imports plus 48 MB, the 72 MB coefficient buffer of an
    accepted progressive JPEG can't be had; under the production cap it decodes."""
    MB, production = 2**20, photos.WORKER_MEMORY
    raw = (crafted / "prog-grey-36mp.jpg").read_bytes()

    code, out, report, err = run_capped(raw, "volunteer", production, monkeypatch)
    assert code == 0 and out, err
    assert int(report["ready"]["as_limit"]) == production  # read back
    imported = int(report["ready"]["vmsize_kb"]) * 1024
    peak = int(report["done"]["vmpeak_kb"]) * 1024
    assert peak - imported > 64 * MB  # this decode really needs the room

    cap = imported + 48 * MB
    code, out, report, err = run_capped(raw, "volunteer", cap, monkeypatch)
    assert int(report["ready"]["as_limit"]) == cap  # got past the imports, capped
    assert code != 0 and out == b"" and "done" not in report, err
    # libjpeg's failed allocation reaches Pillow as a decoder error ("broken data
    # stream"), so the worker refuses the picture (exit 2), or Python raises
    # MemoryError (exit 1): either way the same bytes decoded above, uncapped
    assert b"\nrefused: " in err or b"MemoryError" in err, err
    with pytest.raises(photos.BadPhoto):
        photos.process(raw, "volunteer")
    print(
        f"\nRLIMIT_AS: after imports {imported / MB:.0f} MB; stopped under "
        f"{cap / MB:.0f} MB; decodes under {production / MB:.0f} MB with a "
        f"peak of {peak / MB:.0f} MB"
    )


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="macOS doesn't enforce RLIMIT_AS; production (Cloud Run) is Linux, where "
    "this runs",
)
def test_the_production_cap_leaves_room_for_the_worst_accepted_files(
    monkeypatch, crafted
):
    MB = 2**20
    rows = []
    for name, kind in WORST_ACCEPTED:
        raw = (crafted / name).read_bytes()
        code, out, report, err = run_capped(
            raw, kind, photos.WORKER_MEMORY, monkeypatch
        )
        assert code == 0 and out, (name, err)
        peak = int(report["done"]["vmpeak_kb"]) * 1024
        assert peak < photos.WORKER_MEMORY, name
        rows.append(f"{name} ({kind}): peak {peak / MB:.0f} MB")
    print("\nunder the production cap of", photos.WORKER_MEMORY // MB, "MB:")
    print("\n".join(rows))


# --- what the child sees, how much it may say, and the parent's logs ----------------------


def _child(monkeypatch, script):
    monkeypatch.setattr(
        photos, "worker_command", lambda kind: [sys.executable, "-I", "-c", script]
    )


def test_the_child_gets_no_secrets(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "s3cret-value")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t0ken")
    _child(monkeypatch, "import os, sys; sys.stdout.write(repr(dict(os.environ)))")
    _, out, _ = photos._run_worker("volunteer", b"")
    seen = ast.literal_eval(out.decode())  # the child's repr of a dict of strings
    assert b"s3cret" not in out and b"t0ken" not in out
    assert {"PATH", "LANG"} <= set(seen) and set(seen) <= {
        "PATH",
        "LANG",
        "__CF_USER_TEXT_ENCODING",  # macOS adds this one to every process
        "LC_CTYPE",
    }


def test_the_real_worker_says_ready_and_answers_two_jpegs():
    code, out, err = photos._run_worker("volunteer", jpeg((640, 480)))
    assert code == 0 and err.startswith(b"ready")
    size = int.from_bytes(out[:8], "big")
    assert out[8:10] == b"\xff\xd8" and out[8 + size : 8 + size + 2] == b"\xff\xd8"


def test_a_child_that_says_too_much_is_killed(monkeypatch):
    _child(
        monkeypatch,
        "import sys\nwhile True: sys.stdout.buffer.write(b'x' * 65536)",
    )
    started = time.monotonic()
    code, out, _ = photos._run_worker("volunteer", b"")
    assert code == -signal.SIGKILL and len(out) <= photos.WORKER_OUT + 65536
    assert time.monotonic() - started < photos.WORKER_TIMEOUT
    with pytest.raises(photos.BadPhoto):
        photos.process(jpeg())


def test_a_child_whose_length_runs_past_its_answer_is_refused(monkeypatch):
    _child(
        monkeypatch,
        "import sys; sys.stdout.buffer.write((10**6).to_bytes(8, 'big') + b'\\xff\\xd8')",
    )
    with pytest.raises(photos.BadPhoto, match="something else"):
        photos.process(jpeg())


def test_a_child_that_cant_start_means_busy(monkeypatch, user):
    monkeypatch.setattr(photos, "worker_command", lambda kind: ["/nonexistent/python"])
    with pytest.raises(photos.Busy):
        photos.process(jpeg())
    r = upload(user, jpeg())
    assert r.status_code == 503 and r.headers["retry-after"] == "30"


def test_a_failed_child_is_logged_without_the_picture(monkeypatch, caplog):
    _child(
        monkeypatch,
        "import os, signal, sys; sys.stderr.write('boom\\n'); sys.stderr.flush(); "
        "os.kill(os.getpid(), signal.SIGKILL)",
    )
    raw = jpeg((300, 200))
    with (
        caplog.at_level("WARNING", logger="causematch"),
        pytest.raises(photos.BadPhoto),
    ):
        photos.process(raw)
    (line,) = [
        r.getMessage() for r in caplog.records if "photo worker" in r.getMessage()
    ]
    assert "rc=-9" in line and "SIGKILL" in line and "boom" in line
    assert raw[:64].hex() not in line and len(line) < 500


# --- WebP sizes come from the header, in Python: the app never runs libwebp ------------------


def webp(size, **kw):
    buf = io.BytesIO()
    mode = "RGBA" if kw.pop("alpha", False) else "RGB"
    Image.new(mode, size).save(buf, "WEBP", **kw)
    return buf.getvalue()


@pytest.mark.parametrize(
    ("kw", "chunk"),
    [
        ({"quality": 80}, b"VP8 "),
        ({"lossless": True}, b"VP8L"),
        ({"quality": 80, "alpha": True}, b"VP8X"),
    ],
)
def test_webp_sizes_are_read_from_the_header(kw, chunk):
    raw = webp((1234, 567), **kw)
    assert raw[12:16] == chunk and photos.webp_size(raw) == (1234, 567)


def test_the_app_process_never_opens_a_webp_with_pillow(monkeypatch):
    raw = webp((800, 600), quality=80)

    def refuse(*a, **kw):
        raise AssertionError("Pillow opened a WebP in the app process")

    monkeypatch.setattr(photos.Image, "open", refuse)
    assert photos._check(raw) == (800, 600, False)


def test_an_oversized_or_strange_webp_header_is_refused():
    huge = bytearray(webp((100, 100), lossless=True))
    bits = (6999) | (6999 << 14)  # 7000 x 7000, minus one each
    huge[21:25] = (
        bits | (int.from_bytes(huge[21:25], "little") & ~0xFFFFFFF)
    ).to_bytes(4, "little")
    assert photos.webp_size(bytes(huge)) == (7000, 7000)
    with pytest.raises(photos.BadPhoto, match="too many pixels"):
        photos._check(bytes(huge))
    animated = bytearray(webp((64, 64), quality=80, alpha=True))
    animated[20] |= 0x02
    for raw in (bytes(animated), b"RIFF\x00\x00\x00\x00WEBPVP8Z" + bytes(20), b"RIFF"):
        with pytest.raises(photos.BadPhoto):
            photos.webp_size(raw)
