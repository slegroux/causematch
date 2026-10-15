"""An org's logo, photo, about and website; an opening's photo and event link; the swipe
card's picture (opening photo, org photo, logo, cause art); the demo's media. Every picture
here is generated."""

import io
import json
import re

import pytest
from fasthtml.common import to_xml
from PIL import Image, ImageDraw
from test_photos import jpeg, uid_of
from test_ui import _demo

from app import data, photos
from app.db import openings, orgs
from app.forms import domain, https_url
from app.routes.deck import OpeningCard


def png(size=(400, 120), fill=(20, 60, 140, 255), text_white=False):
    """A transparent logo: a coloured bar, or a white wordmark-ish bar."""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    color = (255, 255, 255, 255) if text_white else fill
    ImageDraw.Draw(im).rectangle((40, 40, size[0] - 40, size[1] - 40), fill=color)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def opened(raw):
    return Image.open(io.BytesIO(raw))


def upload(client, path, raw, kind="image/jpeg"):
    return client.post(path, files={"photo": ("p", raw, kind)}, follow_redirects=False)


def first_open(org_id):
    return next(o for o in openings() if o.org_id == org_id and o.status == "open")


# --- photos.process kinds ------------------------------------------------------------------


def test_a_card_photo_is_cut_to_3_by_2():
    out = photos.process(jpeg((3000, 1000)), "card")
    assert opened(out.full).size == photos.CARD
    small = photos.process(jpeg((600, 600)), "card")
    assert opened(small.full).size == (600, 400)  # cut, never enlarged


def test_a_logo_is_a_square_on_white():
    out = opened(photos.process(png(), "logo").full)
    assert out.size == (photos.LOGO, photos.LOGO)
    assert out.getpixel((2, 2)) >= (250, 250, 250)


def test_a_white_wordmark_gets_a_dark_background():
    out = opened(photos.process(png(text_white=True), "logo").full)
    assert sum(out.getpixel((2, 2))) < 150  # ink, so the white logo still shows


def test_an_opaque_logo_keeps_its_own_background():
    out = opened(photos.process(jpeg((300, 100), color=(0, 90, 60)), "logo").full)
    r, g, b = out.getpixel((2, 2))
    assert abs(r - 0) < 12 and abs(g - 90) < 12 and abs(b - 60) < 12


def test_svg_is_not_a_picture():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="9" height="9"/></svg>'
    with pytest.raises(photos.BadPhoto):
        photos.process(svg, "logo")


# --- links -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "http://example.org/x",
        "javascript:alert(1)",
        "data:text/html,hi",
        "https://user:pw@example.org/",
        "https://localhost/",
        "https://example.org/a b",
        "https://example.org:99999/",
        "//example.org/x",
        "https://" + "a" * 600 + ".org",
        "https://evil.com\\good.org",  # a browser reads the backslash as a slash
        "https://good.org\u202e/moc.live",  # right-to-left override in the host
        "https://example.org/a\u2066b",  # and anywhere else
        "https://good.org%2eevil.com/",  # a browser decodes the host: good.org.evil.com
        "https://%65vil.com/",
        "https://-bad.org/",  # every label, after IDNA, is letters, digits and hyphens
        "https://bad-.org/",
        "https://a_b.org/",
        "https://bücher.de/" + "x" * 480,  # 498 typed, over 500 once in punycode
    ],
)
def test_only_plain_https_links_are_accepted(raw):
    assert https_url(raw) is None


def test_a_link_shows_its_domain():
    assert https_url(" https://www.example.org/volunteer ") == (
        "https://www.example.org/volunteer"
    )
    assert domain("https://www.Example.org/volunteer") == "example.org"


def test_an_international_domain_shows_as_punycode():
    """A Cyrillic "а" looks like a Latin "a": the card says xn--pple-43d.com instead."""
    url = https_url("https://\u0430pple.com/volunteer")
    assert url == "https://xn--pple-43d.com/volunteer"
    assert domain(url) == "xn--pple-43d.com"


def test_org_photos_may_be_cached_a_day(coord, user):
    upload(coord, f"/org/organization/{coord.org_id}/logo", png(), "image/png")
    r = user.get(f"/photos/{orgs[coord.org_id].logo_key}.jpg")
    assert r.headers["cache-control"] == "private, max-age=86400"


# --- the Organization page -------------------------------------------------------------


def test_the_organization_page_saves_logo_photo_about_and_website(coord):
    page = coord.get("/org/organization")
    assert page.status_code == 200 and "Add a logo" in page.text
    base = f"/org/organization/{coord.org_id}"
    assert upload(coord, f"{base}/logo", png(), "image/png").status_code == 303
    assert upload(coord, f"{base}/photo", jpeg((1600, 1200))).status_code == 303
    r = coord.post(
        base,
        data={"about": "We sort food.", "website": "https://example.org"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/org/organization?notice=saved"
    org = orgs[coord.org_id]
    assert org.about == "We sort food." and org.website == "https://example.org"
    assert opened(coord.get(f"/photos/{org.logo_key}.jpg").content).size == (320, 320)
    assert opened(coord.get(f"/photos/{org.photo_key}.jpg").content).size == (960, 640)
    old = org.logo_key
    upload(coord, f"{base}/logo", png(fill=(200, 0, 0, 255)), "image/png")
    assert orgs[coord.org_id].logo_key != old
    assert coord.get(f"/photos/{old}.jpg").status_code == 404  # replaced, deleted


@pytest.mark.parametrize(
    "form",
    [
        {"about": "x", "website": "http://example.org"},
        {"about": "x", "website": "javascript:alert(1)"},
        {"about": "x" * 1001, "website": ""},
    ],
)
def test_bad_details_are_refused_and_change_nothing(coord, form):
    before = orgs[coord.org_id]
    r = coord.post(f"/org/organization/{coord.org_id}", data=form)
    assert r.status_code == 400
    after = orgs[coord.org_id]
    assert (after.about, after.website) == (before.about, before.website)


def test_a_bad_logo_is_400(coord):
    r = upload(
        coord, f"/org/organization/{coord.org_id}/logo", b"<svg/>", "image/svg+xml"
    )
    assert r.status_code == 400 and "couldn't read that picture" in r.text
    assert orgs[coord.org_id].logo_key is None


def test_org_uploads_take_6_mb_and_details_64_kb(coord):
    big = b"\xff" * (7 * 1024 * 1024)
    for path in (
        f"/org/organization/{coord.org_id}/logo",
        f"/org/organization/{coord.org_id}/photo",
        f"/org/openings/{first_open(coord.org_id).id}/photo",
    ):
        assert upload(coord, path, big).status_code == 413, path
    r = coord.post(f"/org/organization/{coord.org_id}", content=b"x" * 70_000)
    assert r.status_code == 413


def test_org_pictures_are_seen_by_any_signed_in_user(coord, user):
    upload(coord, f"/org/organization/{coord.org_id}/logo", png(), "image/png")
    key = orgs[coord.org_id].logo_key
    assert user.get(f"/photos/{key}.jpg").status_code == 200
    assert user.get(f"/photos/thumb/{key}.jpg").status_code == 200


def test_the_dashboard_links_to_the_new_pages(coord):
    page = coord.get("/org").text
    assert 'href="/org/organization"' in page
    assert f'href="/org/openings/{first_open(coord.org_id).id}/media"' in page


# --- an opening's photo and link ---------------------------------------------------------


def test_an_opening_gets_a_photo_and_an_https_link(coord):
    o = first_open(coord.org_id)
    base = f"/org/openings/{o.id}"
    assert coord.get(f"{base}/media").status_code == 200
    assert upload(coord, f"{base}/photo", jpeg()).status_code == 303
    r = coord.post(
        f"{base}/link",
        data={"event_url": "https://www.example.org/events/1"},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"{base}/media?notice=saved"
    o = openings[o.id]
    assert o.photo_key and o.event_url == "https://www.example.org/events/1"
    page = coord.get(f"{base}/media").text
    assert "See it on example.org" in page
    for bad in ("http://example.org", "javascript:alert(1)"):
        r = coord.post(f"{base}/link", data={"event_url": bad})
        assert r.status_code == 400
        assert openings[o.id].event_url == "https://www.example.org/events/1"
    coord.post(f"{base}/link", data={"event_url": ""})
    assert openings[o.id].event_url is None


# --- the swipe card ------------------------------------------------------------------------


def card(user, opening_id):
    return next(c for c in data.deck(uid_of(user), 50) if c["id"] == opening_id)


def card_html(user, opening_id):
    return to_xml(OpeningCard(card(user, opening_id)))


def test_the_card_picture_falls_back_opening_org_logo_cause(user):
    o = first_open(orgs()[0].id)
    html = card_html(user, o.id)
    assert "/static/img/causes/" in html and "/photos/" not in html
    logo = data.store_photo("org_logo", o.org_id, photos.process(png(), "logo"))
    orgs.update(id=o.org_id, logo_key=logo)
    html = card_html(user, o.id)
    assert f"/photos/{logo}.jpg" in html and "/static/img/causes/" not in html
    org_photo = data.store_photo("org_photo", o.org_id, photos.process(jpeg(), "card"))
    orgs.update(id=o.org_id, photo_key=org_photo)
    html = card_html(user, o.id)
    assert f"/photos/{org_photo}.jpg" in html and f"/photos/{logo}.jpg" in html
    own = data.store_photo("opening", o.id, photos.process(jpeg(), "card"))
    openings.update(id=o.id, photo_key=own)
    html = card_html(user, o.id)
    assert f"/photos/{own}.jpg" in html and f"/photos/{org_photo}.jpg" not in html
    assert f"/photos/{logo}.jpg" in html  # the badge stays


def test_the_card_and_the_match_link_to_the_event_in_a_new_tab(user):
    o = first_open(orgs()[0].id)
    openings.update(id=o.id, event_url="https://www.example.org/volunteer")
    html = card_html(user, o.id)
    link = re.search(r"<a [^>]*href=\"https://www.example.org/volunteer\"[^>]*>", html)
    assert link and 'target="_blank"' in link[0]
    assert 'rel="noopener noreferrer"' in link[0]
    assert "See it on example.org" in html
    data.swipe(uid_of(user), o.id, True)
    assert "See it on example.org" in user.get("/matches").text


def test_the_deck_page_shows_the_top_cards_photo(user):
    for org in orgs():
        key = data.store_photo("org_photo", org.id, photos.process(jpeg(), "card"))
        orgs.update(id=org.id, photo_key=key)
    top = data.deck(uid_of(user), 1)[0]
    page = user.get("/deck").text
    assert f'src="/photos/{top["photo"]}.jpg"' in page


# --- scripts/demo.py ---------------------------------------------------------------------


def test_the_demo_loads_logos_photos_and_links(tmp_path):
    (tmp_path / "logos-raw").mkdir()
    (tmp_path / "photos-raw").mkdir()
    (tmp_path / "logos-raw" / "a.png").write_bytes(png())
    (tmp_path / "logos-raw" / "b.svg").write_bytes(b"<svg/>")
    (tmp_path / "photos-raw" / "a.jpg").write_bytes(jpeg((1200, 800)))
    (tmp_path / "photos-raw" / "a-2.jpg").write_bytes(jpeg((900, 900)))
    org = {
        "cause": "environment-parks",
        "lat": 34.1,
        "lon": -118.25,
        "volunteer_url": "https://invented.example.org/volunteer",
    }
    before = {o.id for o in orgs()}
    _demo().load_orgs(
        [
            {
                **org,
                "name": "Invented Org A",
                "website": "https://invented.example.org",
                "logo_file": "a.png",
                "photo_file": "a.jpg",
                "openings": [
                    {"title": "One", "source_url": "https://invented.example.org/one"},
                    {
                        "title": "Two",
                        "photo_file": "a-2.jpg",
                        "source_url": "http://x.org",
                    },
                ],
            },
            {
                **org,
                "name": "Invented Org B",
                "logo_file": "../logos-raw/b.svg",  # SVG: skipped, and only a basename
                "photo_file": "missing.jpg",
                "openings": [{"title": "Three"}],
            },
        ],
        tmp_path,
    )
    a, b = sorted((o for o in orgs() if o.id not in before), key=lambda o: o.name)
    assert a.logo_key and a.photo_key and a.website == "https://invented.example.org"
    assert b.logo_key is None and b.photo_key is None
    one, two = openings(where="org_id = ?", where_args=[a.id], order_by="id")
    assert one.event_url == "https://invented.example.org/one" and not one.photo_key
    assert two.event_url == org["volunteer_url"] and two.photo_key  # http: fallback
    (three,) = openings(where="org_id = ?", where_args=[b.id])
    assert three.event_url == org["volunteer_url"]
    assert json.loads(three.causes) == ["environment-parks"]
