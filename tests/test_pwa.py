import json
import re
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_manifest_is_public_and_installable(anon):
    r = anon.get("/static/manifest.webmanifest", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] in (
        "application/manifest+json",
        "application/json",
    )
    m = json.loads(r.text)
    assert m["name"] == "Causematch" and m["short_name"]
    assert m["start_url"] == "/deck"
    assert m["display"] == "standalone"
    assert m["theme_color"] and m["background_color"]
    sizes = {i["sizes"]: i for i in m["icons"]}
    assert {"192x192", "512x512"} <= set(sizes)
    for size, icon in sizes.items():
        icon_r = anon.get(icon["src"], follow_redirects=False)
        assert (
            icon_r.status_code == 200 and icon_r.headers["content-type"] == "image/png"
        )
        assert "x".join(map(str, png_size(icon_r.content))) == size


def test_apple_touch_icon_and_favicon_are_public(anon):
    r = anon.get("/static/icons/apple-touch-icon.png", follow_redirects=False)
    assert r.status_code == 200 and png_size(r.content) == (180, 180)
    r = anon.get("/favicon.ico", follow_redirects=False)
    assert r.status_code == 200 and r.content[:4] == b"\x00\x00\x01\x00"


def test_every_page_links_the_manifest_and_icons(anon, user, coord):
    for client, path in (
        (anon, "/login"),
        (anon, "/org/login"),
        (user, "/deck"),
        (coord, "/org"),
    ):
        html = client.get(path).text
        assert 'rel="manifest" href="/static/manifest.webmanifest"' in html, path
        assert 'rel="apple-touch-icon"' in html, path
        assert 'name="theme-color"' in html, path
        assert 'name="apple-mobile-web-app-capable"' in html, path
        assert 'src="/static/a2hs.js"' in html, path


def test_home_screen_hint_is_on_the_deck_only(anon, user, coord):
    """Elsewhere the toast would cover the chat composer or the end of a list."""
    tag = re.search(r'<div[^>]*id="a2hs"[^>]*>', user.get("/deck").text)
    assert tag and "hidden" in tag.group(0)
    for client, path in (
        (user, "/matches"),
        (user, "/history"),
        (user, "/profile"),
        (anon, "/login"),
        (coord, "/org"),
        (coord, "/org/inbox"),
    ):
        assert 'id="a2hs"' not in client.get(path).text, path


def test_home_screen_script_wraps_every_storage_call_in_try():
    js = (ROOT / "static" / "a2hs.js").read_text()
    blocks = re.findall(r"try \{(.*?)\} catch", js, re.DOTALL)
    assert js.count("localStorage") == sum(b.count("localStorage") for b in blocks) > 0


def test_manifest_has_a_maskable_icon_and_an_id():
    m = json.loads((ROOT / "static" / "manifest.webmanifest").read_text())
    assert m["id"] == "/deck"
    (mask,) = [i for i in m["icons"] if i.get("purpose") == "maskable"]
    data = (ROOT / mask["src"].removeprefix("/")).read_bytes()
    assert png_size(data) == (512, 512)


def test_in_app_browsers_are_excluded_from_the_hint():
    js = (ROOT / "static" / "a2hs.js").read_text()
    for ua in ("Instagram", "FBAN", "FBAV", "GSA/", "Ddg/"):
        assert ua.replace("/", "\\/") in js, ua
