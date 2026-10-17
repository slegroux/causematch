"""CAUSEMATCH_ENV=preview: a production deploy behind a password, with fake sign-in, the
demo's data and a banner. Production stays exactly as strict."""

import base64
import hashlib
import hmac
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from conftest import ROOT, coordinator_for, jpeg, uid_of
from PIL import Image
from starlette.testclient import TestClient
from test_chat import Chat, bubble, one_loop, refused, say  # noqa: F401  one_loop: fixture
from test_photos import key_of, upload

from app import data, demo_seed, notify, security
from app.auth import session_cookie, session_secret
from app.config import (
    DEMO_COORDINATOR,
    DEMO_PHONE,
    contact_email,
    demo_banner,
    demo_matches,
    is_dev,
    is_preview,
    is_production,
    phone_hash_key,
    public_base_url,
    require_env,
    tasks_token,
)
from app.db import (
    Match,
    Profile,
    matches,
    messages,
    openings,
    orgs,
    places,
    profiles,
    swipes,
    urgent_calls,
)
from app.db import (
    now as db_now,
)
from app.ui import DEMO_NOTICE, PREVIEW_NOTICE
from main import app

PASSWORD = "p" * 20  # low entropy on purpose: the secret scan reads tests too
SECRET = "s" * 32
PREVIEW = {"CAUSEMATCH_ENV": "preview", "PREVIEW_PASSWORD": PASSWORD}
# what a running preview has on top: production's secrets
SECRETS = {"PHONE_HASH_KEY": SECRET, "TASKS_TOKEN": SECRET, "SESSION_SECRET": SECRET}
CLOUD_RUN = {"K_SERVICE": "causematch-preview"}
PRODUCTION = (
    {"CAUSEMATCH_ENV": "production"},
    CLOUD_RUN,
    {**CLOUD_RUN, "CAUSEMATCH_ENV": "development"},
)


def basic(password=PASSWORD, user="preview"):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def access_cookie(password=PASSWORD, issued=None, secret=SECRET):
    """The cookie's value worked out here, not by the code under test."""
    issued = int(time.time()) if issued is None else issued
    digest = hashlib.sha256(password.encode()).hexdigest()
    msg = f"preview-access-v2|{digest}|{issued}".encode()
    return f"{issued}.{hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()}"


COOKIE = "__Host-cm_preview"
PRIVATE = "This is a private preview. Ask the Causematch team for the link."
DAYS_30 = 30 * 24 * 3600


def counted():
    """A wrong guess was counted against the test client's address."""
    return ("preview", "testclient") in security._buckets


@pytest.fixture
def preview(monkeypatch):
    """This process in a preview: the password, and the secrets production requires."""
    for k, v in {
        **PREVIEW,
        **SECRETS,
        "CONTACT_EMAIL": "hello@causematch.test",
    }.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def guest(preview):
    """A browser in the preview that has typed the password."""
    return TestClient(app, headers={"Authorization": basic()})


# --- configuration -----------------------------------------------------------------


@pytest.mark.parametrize("env", PRODUCTION)
def test_production_is_unchanged(env):
    assert is_production(env) and not is_preview(env) and not is_dev(env)
    with pytest.raises(RuntimeError):
        demo_banner({**env, "CAUSEMATCH_DEMO_BANNER": "1"})
    for make in (notify.make_verifier, notify.make_sms):
        with pytest.raises(RuntimeError):
            make({**env, "CAUSEMATCH_FAKE_SMS": "1"})
    with pytest.raises(RuntimeError):
        notify.make_emailer({**env, "CAUSEMATCH_FAKE_EMAIL": "1"})


@pytest.mark.parametrize("on_cloud_run", [{}, CLOUD_RUN])
@pytest.mark.parametrize("password", [None, "", "x" * 15])
def test_a_preview_without_a_strong_password_refuses_to_start(on_cloud_run, password):
    env = {"CAUSEMATCH_ENV": "preview", **on_cloud_run}
    if password is not None:
        env["PREVIEW_PASSWORD"] = password
    with pytest.raises(RuntimeError, match="PREVIEW_PASSWORD"):
        require_env(env)
    assert not is_preview(env) and is_production(env)  # strict, and no fakes
    with pytest.raises(RuntimeError):
        notify.make_verifier({**env, "CAUSEMATCH_FAKE_SMS": "1"})


@pytest.mark.parametrize("on_cloud_run", [{}, CLOUD_RUN])
def test_a_preview_may_fake_sign_in_and_show_the_banner(on_cloud_run):
    env = {**PREVIEW, **on_cloud_run}
    require_env(env)
    assert is_preview(env) and is_production(env) and not is_dev(env)
    assert demo_banner(env)  # always on, without CAUSEMATCH_DEMO_BANNER
    fake = {**env, "CAUSEMATCH_FAKE_SMS": "1", "CAUSEMATCH_FAKE_EMAIL": "1"}
    assert isinstance(notify.make_verifier(fake), notify.FakeVerifier)
    assert isinstance(notify.make_sms(fake), notify.FakeSMS)
    assert isinstance(notify.make_emailer(fake), notify.FakeEmailer)


def test_a_preview_keeps_productions_secrets_cookie_and_https():
    with pytest.raises(RuntimeError):
        session_secret(PREVIEW)
    with pytest.raises(RuntimeError):
        session_secret({**PREVIEW, "SESSION_SECRET": "x" * 31})
    assert session_secret({**PREVIEW, "SESSION_SECRET": SECRET}) == SECRET
    for check, name in (
        (phone_hash_key, "PHONE_HASH_KEY"),
        (tasks_token, "TASKS_TOKEN"),
    ):
        with pytest.raises(RuntimeError):
            check(PREVIEW)
        with pytest.raises(RuntimeError):
            check({**PREVIEW, name: "x" * 31})
    with pytest.raises(RuntimeError):
        contact_email(PREVIEW)
    with pytest.raises(RuntimeError):
        public_base_url(PREVIEW)
    with pytest.raises(RuntimeError):
        public_base_url({**PREVIEW, "PUBLIC_BASE_URL": "http://x.run.app"})
    assert session_cookie(PREVIEW) == "__Host-cm_session"


def _start(tmp_path, **env):
    """python -c 'import main' in a fresh process: the app's startup, nothing served."""
    base = {
        "PATH": os.environ["PATH"],
        "CAUSEMATCH_DB": str(tmp_path / "preview.db"),
        "SESSION_SECRET": SECRET,
        "PHONE_HASH_KEY": SECRET,
        "TASKS_TOKEN": SECRET,
        "PUBLIC_BASE_URL": "https://causematch-preview.run.app",
        "CONTACT_EMAIL": "hello@causematch.test",
        "TRUSTED_PROXY_HOPS": "1",
        "CAUSEMATCH_FAKE_SMS": "1",
        "CAUSEMATCH_FAKE_EMAIL": "1",
        **CLOUD_RUN,
    }
    return subprocess.run(
        [sys.executable, "-c", "import main"],
        cwd=ROOT,
        env={**base, **env},
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cloud_run_starts_a_preview_only_with_its_password(tmp_path):
    r = _start(tmp_path, CAUSEMATCH_ENV="preview")
    assert r.returncode != 0 and "PREVIEW_PASSWORD must be" in r.stderr
    r = _start(tmp_path, CAUSEMATCH_ENV="production")  # fakes: production refuses
    assert r.returncode != 0 and "CAUSEMATCH_FAKE_SMS=1 needs" in r.stderr


def test_a_preview_seeds_itself_at_startup_and_logs_the_sign_in_links(tmp_path):
    r = _start(tmp_path, CAUSEMATCH_ENV="preview", PREVIEW_PASSWORD=PASSWORD)
    assert r.returncode == 0, r.stderr
    assert "preview seeded" in r.stderr
    for email in (demo_seed.COORDINATOR[0], demo_seed.ADMIN[0]):
        assert re.search(
            rf"{re.escape(email)} .*https://causematch-preview\.run\.app"
            r"/org/login/verify\?token=\S+",
            r.stderr,
        )


# --- the password ------------------------------------------------------------------

EVERYWHERE = (
    "/",
    "/login",
    "/org/login",
    "/static/theme.css",
    "/static/manifest.webmanifest",
    "/favicon.ico",
    "/photos/x.jpg",
    "/photos/thumb/x.jpg",
    "/privacy",
    "/no-such-page",
)


def test_every_path_needs_the_password(preview):
    for headers in (
        {},
        {"Authorization": basic("w" * 20)},
        {"Cookie": f"{COOKIE}={access_cookie('w' * 20)}"},
    ):
        c = TestClient(app, headers=headers)
        for path in EVERYWHERE:
            security.reset_buckets()  # wrong ones count: test_guessing_...
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 401, path
            assert "www-authenticate" not in r.headers  # no browser dialog
            assert r.headers["x-robots-tag"] == "noindex, nofollow"
            assert r.headers["content-type"].startswith("text/html")
            assert PRIVATE in r.text and "<link" not in r.text  # nothing of the app
        r = c.post("/tasks/reminders", headers={"Authorization": f"Bearer {SECRET}"})
        assert r.status_code == 401
        r = c.post("/login/send", data={"phone": "3105550123"})
        assert r.status_code == 401 and not notify.get_verifier().sent


def test_the_password_lets_every_path_through(guest):
    for path in EVERYWHERE:
        r = guest.get(path, follow_redirects=False)
        assert r.status_code != 401, path
        assert r.headers["x-robots-tag"] == "noindex, nofollow", path
    assert guest.get("/login").status_code == 200
    assert guest.get("/static/theme.css").status_code == 200
    # any user name
    other = TestClient(app, headers={"Authorization": basic(user="")})
    assert other.get("/login").status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        "Basic",
        "Basic !!!",
        "Bearer " + PASSWORD,
        "basic "
        + base64.b64encode(PASSWORD.encode()).decode(),  # no colon: no password
        basic(PASSWORD[:-1]),
        basic(PASSWORD + "x"),
    ],
)
def test_malformed_or_wrong_credentials_are_refused(preview, header):
    r = TestClient(app, headers={"Authorization": header}).get("/login")
    assert r.status_code == 401


def test_checking_a_bucket_leaves_nothing_behind():
    assert security.take_token("preview", "203.0.113.9", take=False)
    assert ("preview", "203.0.113.9") not in security._buckets
    assert security.take_token("preview", "203.0.113.9")
    assert ("preview", "203.0.113.9") in security._buckets


def test_guessing_the_password_is_slowed(preview):
    wrong = TestClient(app, headers={"Authorization": basic("g" * 20)})
    first_visit = TestClient(app)
    for _ in range(20):  # a first visit sends no credentials: not a guess
        assert first_visit.get("/login").status_code == 401
    for _ in range(security.BUCKETS["preview"][0]):
        assert wrong.get("/login").status_code == 401
    assert wrong.get("/login").status_code == 429
    # past the limit even the right password waits: a guess can't be confirmed
    right = TestClient(app, headers={"Authorization": basic()})
    assert right.get("/login").status_code == 429


def test_websockets_need_the_password(one_loop, monkeypatch):  # noqa: F811
    chat = Chat()  # signed in before the preview starts
    for k, v in {**PREVIEW, **SECRETS}.items():
        monkeypatch.setenv(k, v)
    assert refused(chat.vol, chat.vol_ws)
    chat.vol.headers["Authorization"] = basic("w" * 20)
    assert refused(chat.vol, chat.vol_ws)
    chat.vol.headers["Authorization"] = basic()
    with chat.vol.websocket_connect(chat.vol_ws) as ws:
        say(ws, "hello from the preview")
        assert "hello from the preview" in bubble(ws)
    del chat.vol.headers["Authorization"]
    security.reset_buckets()
    # The test client's sockets are ws://, so a Secure cookie stays home: set it plain.
    chat.vol.cookies.set(COOKIE, access_cookie("w" * 20))
    assert refused(chat.vol, chat.vol_ws) and counted()
    chat.vol.cookies.set(COOKIE, access_cookie())
    with chat.vol.websocket_connect(chat.vol_ws) as ws:
        say(ws, "hello again")
        assert "hello again" in bubble(ws)


# --- the access link ---------------------------------------------------------------


def test_the_link_sets_the_cookie_and_drops_the_key(preview):
    c = TestClient(app, base_url="https://testserver")  # the cookie is Secure
    r = c.get(f"/login?next=%2Forg&access={PASSWORD}&x=1", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Forg&x=1"
    assert r.headers["cache-control"] == "no-store"
    flags = [f.strip() for f in r.headers["set-cookie"].split(";")]
    name, _, value = flags[0].partition("=")
    issued = int(value.partition(".")[0])
    assert name == COOKIE and value == access_cookie(issued=issued)
    assert 0 <= time.time() - issued < 60
    assert {f.lower() for f in flags[1:]} == {
        "httponly",
        "max-age=2592000",
        "path=/",
        "samesite=lax",
        "secure",
    }
    assert PASSWORD not in r.headers["location"] + r.headers["set-cookie"]
    # just the key: back to the bare path; curl -I sends HEAD
    r = c.head(f"/?access={PASSWORD}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert not counted()


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/",
        "///evil.example/",
        "/%2F%2Fevil.example/",
        "/%5Cevil.example/",
        "/%2F%5Cevil.example/",
        "/%09/evil.example/",
        "/https:%2F%2Fevil.example/",
    ],
)
def test_the_link_never_leaves_the_site(preview, path):
    c = TestClient(app, base_url="https://testserver")
    r = c.get(f"https://testserver{path}?access={PASSWORD}", follow_redirects=False)
    location = r.headers["location"]
    assert r.status_code == 303 and location.startswith("/"), location
    assert not location.startswith("//") and "\\" not in location, location
    assert not urlsplit(location).netloc and not urlsplit(location).scheme


def test_only_a_navigation_weighs_the_key(preview):
    """An <img> or fetch that a foreign page or an email fires at the preview can't use up
    a visitor's guesses: the key isn't compared, nor counted."""
    for dest, mode in (("image", "no-cors"), ("empty", "cors"), ("iframe", "navigate")):
        h = {"Sec-Fetch-Dest": dest, "Sec-Fetch-Mode": mode}
        c = TestClient(app, base_url="https://testserver", headers=h)
        for _ in range(security.BUCKETS["preview"][0] + 1):
            assert c.get("/login?access=wrong").status_code == 401
        r = c.get(f"/login?access={PASSWORD}", follow_redirects=False)
        assert r.status_code == 401 and "set-cookie" not in r.headers
        assert not counted()
    tapped = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"}
    c = TestClient(app, base_url="https://testserver", headers=tapped)
    assert c.get("/login?access=wrong").status_code == 401 and counted()
    r = c.get(f"/login?access={PASSWORD}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["set-cookie"].startswith(COOKIE)


def test_a_post_with_the_key_is_neither_let_in_nor_counted(preview):
    c = TestClient(app, base_url="https://testserver")
    r = c.post(f"/login/send?access={PASSWORD}", data={"phone": "3105550123"})
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert not notify.get_verifier().sent and not counted()


def test_a_valid_cookie_gets_in_past_the_limit(preview):
    """Someone else at the same address (an office) used up its guesses: a visitor who
    already has the cookie isn't locked out, one without it is."""
    wrong = TestClient(app, headers={"Authorization": basic("g" * 20)})
    for _ in range(security.BUCKETS["preview"][0]):
        assert wrong.get("/login").status_code == 401
    assert wrong.get("/login").status_code == 429
    inside = TestClient(app, headers={"Cookie": f"{COOKIE}={access_cookie()}"})
    assert inside.get("/login").status_code == 200
    r = inside.get("/login?access=wrong", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    stale = TestClient(app, headers={"Cookie": f"{COOKIE}={access_cookie('o' * 20)}"})
    assert stale.get("/login").status_code == 429
    assert TestClient(app).get(f"/login?access={PASSWORD}").status_code == 429


@pytest.mark.parametrize("key", [PASSWORD, "wrong"])
def test_with_the_cookie_the_key_is_only_taken_out(preview, key):
    c = TestClient(app, headers={"Cookie": f"{COOKIE}={access_cookie()}"})
    r = c.get(f"/login?access={key}&x=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?x=1"
    assert "set-cookie" not in r.headers and not counted()


def test_the_cookie_lets_pages_static_and_photos_through(user, monkeypatch):
    upload(user, jpeg())  # before the preview starts
    key = key_of(user)
    for k, v in {**PREVIEW, **SECRETS}.items():
        monkeypatch.setenv(k, v)
    user.base_url = "https://testserver"
    assert user.get("/me/profile").status_code == 401
    r = user.get(f"/me/profile?access={PASSWORD}")  # follows the 303
    assert r.status_code == 200 and str(r.url) == "https://testserver/me/profile"
    assert user.get("/static/theme.css").status_code == 200
    assert user.get(f"/photos/{key}.jpg").status_code == 200
    assert user.get(f"/photos/thumb/{key}.jpg").status_code == 200


NOW = int(time.time())
GOOD = access_cookie(issued=NOW)


@pytest.mark.parametrize(
    "value",
    [
        GOOD[:-1] + ("0" if GOOD[-1] != "0" else "1"),
        f"{NOW - 1}{GOOD[len(str(NOW)) :]}",  # another issue time, the same mac
        access_cookie("o" * 20),  # before the password was changed
        access_cookie(secret="t" * 32),  # another session secret
        access_cookie(issued=NOW - DAYS_30 - 60),  # expired
        access_cookie(issued=NOW + 3600),  # from the future
        GOOD.upper(),
        GOOD.partition(".")[2],  # no issue time
        "",
    ],
)
def test_a_tampered_or_old_cookie_is_refused_and_dropped(preview, value):
    r = TestClient(app, headers={"Cookie": f"{COOKIE}={value}"}).get("/login")
    assert r.status_code == 401 and PRIVATE in r.text and counted()
    dropped = r.headers["set-cookie"].lower()
    assert dropped.startswith(f'{COOKIE.lower()}=""') and "max-age=0" in dropped


def test_guessing_the_key_is_slowed(preview):
    c = TestClient(app, base_url="https://testserver")
    for _ in range(20):  # a first visit sends no key: not a guess
        assert c.get("/login").status_code == 401
    # two keys in one link are one wrong guess, even with the right one among them
    assert c.get(f"/login?access={PASSWORD}&access=x").status_code == 401
    for i in range(security.BUCKETS["preview"][0] - 1):
        assert c.get(f"/login?access=g{i}").status_code == 401
    assert c.get("/login?access=g").status_code == 429
    # past the limit the right key waits too: a guess can't be confirmed
    r = c.get(f"/login?access={PASSWORD}", follow_redirects=False)
    assert r.status_code == 429 and "set-cookie" not in r.headers


def test_outside_a_preview_there_is_no_password(anon):
    r = anon.get("/login")
    assert r.status_code == 200 and "x-robots-tag" not in r.headers
    r = anon.get(f"/login?access={PASSWORD}", follow_redirects=False)
    assert r.status_code == 200 and COOKIE not in r.headers.get("set-cookie", "")
    assert anon.get("/robots.txt").status_code == 404


# --- what a preview shows ----------------------------------------------------------


def test_robots_txt_disallows_everything(preview):
    r = TestClient(app).get("/robots.txt")  # no password: crawlers read it
    assert r.status_code == 200 and r.text == "User-agent: *\nDisallow: /\n"
    assert r.headers["x-robots-tag"] == "noindex, nofollow"


def test_every_page_shows_the_preview_banner(guest, anon):
    assert PREVIEW_NOTICE == (
        "Preview · sample openings, not affiliated with these organizations · "
        "don't enter real details"
    )
    for path in ("/login", "/org/login", "/privacy"):
        page = guest.get(path).text
        assert PREVIEW_NOTICE in page and DEMO_NOTICE not in page, path


def test_no_banner_outside_a_preview(anon):
    assert PREVIEW_NOTICE not in anon.get("/login").text


def test_fake_sign_in_codes_work_in_a_preview(guest):
    guest.post("/login/send", data={"phone": "(310) 555-0123"})
    r = guest.post("/login/verify", data={"code": "000000"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"


def test_a_preview_shows_the_sign_in_link_on_the_page(guest):
    _, email = coordinator_for(0)
    page = guest.post("/org/login/send", data={"email": email}).text
    link = re.search(
        r'href="https://causematch\.test(/org/login/verify\?token=[\w-]+)"', page
    )
    assert link and "Open your sign-in link" in page
    guest.get(link[1])
    r = guest.post("/org/login/verify", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/org"
    # an unknown address: the same notice, no link
    page = guest.post("/org/login/send", data={"email": "nobody@example.org"}).text
    assert "Open your sign-in link" not in page and "/org/login/verify" not in page


def test_an_admins_sign_in_link_stays_in_the_log(guest):
    from app import data

    data.add_admin(*demo_seed.ADMIN)  # its address is in this public repo
    page = guest.post("/org/login/send", data={"email": demo_seed.ADMIN[0]}).text
    assert "Open your sign-in link" not in page and "token=" not in page
    to, _, body = notify.get_emailer().sent[-1]  # sent after the response, as always
    assert to == demo_seed.ADMIN[0] and "/org/login/verify?token=" in body


def test_the_fakes_mask_addresses_and_keep_a_bounded_list(caplog):
    assert notify.mask_email("pat@demo.causematch.test") == "p***@d***.test"
    assert notify.mask_email("x@localhost") == "x***@l***"
    emailer, sms = notify.FakeEmailer(), notify.FakeSMS()
    with caplog.at_level("WARNING", logger="causematch"):
        emailer.send("pat@demo.causematch.test", "Hi", "body")
        sms.send("+13105550199", "text")
    logged = caplog.text
    assert "pat@" not in logged and "p***@d***.test" in logged
    assert "5550199" not in logged and "***0199" in logged
    verifier = notify.FakeVerifier()
    for fake, args in (
        (emailer, ("a@b.test", "s", "b")),
        (sms, ("+1", "t")),
        (verifier, ("+1",)),
    ):
        for _ in range(notify.FAKE_KEPT + 5):
            fake.send(*args)
        assert len(fake.sent) == notify.FAKE_KEPT


def test_production_never_shows_the_sign_in_link(anon):
    _, email = coordinator_for(0)
    page = anon.post("/org/login/send", data={"email": email}).text
    assert "Open your sign-in link" not in page and "token=" not in page
    assert notify.get_emailer().sent  # emailed instead (the fake emailer here)


# --- the seed ----------------------------------------------------------------------


def _picture(fmt, size):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 120, 60)).save(buf, fmt)
    return buf.getvalue()


@pytest.fixture
def seed_dir(tmp_path):
    """PREVIEW_SEED_DIR's layout with invented orgs and pictures drawn here."""
    (tmp_path / "logos-raw").mkdir()
    (tmp_path / "photos-raw").mkdir()
    (tmp_path / "logos-raw" / "a.png").write_bytes(_picture("PNG", (300, 300)))
    (tmp_path / "photos-raw" / "a.jpg").write_bytes(_picture("JPEG", (1200, 800)))
    (tmp_path / "photos-raw" / "b.webp").write_bytes(_picture("WEBP", (900, 600)))
    org = {"lat": 34.05, "lon": -118.25, "neighborhood": "Downtown", "zip": "90012"}
    (tmp_path / "orgs.json").write_text(
        json.dumps(
            [
                {
                    **org,
                    "name": "Invented Pantry",
                    "cause": "food-insecurity",
                    "logo_file": "a.png",
                    "photo_file": "a.jpg",
                    "openings": [{"title": "Sort cans", "photo_file": "b.webp"}],
                },
                {
                    **org,
                    "name": "Invented Shelter",
                    "cause": "animal-shelters",
                    "openings": [{"title": "Walk dogs"}],
                },
            ]
        )
    )
    return tmp_path


def _empty():
    for table in (openings, orgs):
        table.delete_where()


def test_seed_preview_loads_the_seed_dir_and_the_demos_people(
    preview, seed_dir, caplog
):
    _empty()
    caplog.set_level("INFO", logger="causematch")
    demo_seed.seed_preview(str(seed_dir))
    pantry, shelter = sorted(orgs(), key=lambda o: o.name)
    assert pantry.name == "Invented Pantry" and pantry.logo_key and pantry.photo_key
    assert pantry.verified_at and shelter.verified_at
    titles = {o.title for o in openings()}
    # the urgent call's opening: the coordinator's org had only one
    fridge = "Sample: Restock the community fridge"
    assert titles == {"Sample: Sort cans", "Sample: Walk dogs", fridge}
    (call,) = urgent_calls()
    assert openings[call.opening_id].title == fridge and call.status == "open"
    assert openings(where="title = ?", where_args=["Sample: Sort cans"])[0].photo_key
    sam = [p for p in profiles() if p.display_name == "Sam"]
    assert len(sam) == 1 and sam[0].photo_key
    assert any(m.status == "accepted" for m in matches())
    logged = [r.getMessage() for r in caplog.records]
    for role, email in (
        ("coordinator", demo_seed.COORDINATOR[0]),
        ("admin", demo_seed.ADMIN[0]),
    ):
        assert any(
            m.startswith(f"preview {role} {email} ") and "/org/login/verify?token=" in m
            for m in logged
        )


def test_seed_gives_the_free_today_switch_cards_in_several_causes(preview):
    """first_start() puts openings 2+ days out: the seed moves some to tomorrow, so the
    demo volunteer's "Free in the next 48h" deck isn't just the urgent call."""
    from app import data

    _empty()
    demo_seed.seed_preview(None)
    (sam,) = [p for p in profiles() if p.display_name == "Sam"]
    cards = data.deck(sam.id, 50, today=True)
    soon = [c for c in cards if not c.get("urgent")]
    assert len(soon) >= 3
    causes = {json.loads(openings[c["id"]].causes)[0] for c in soon}
    assert len(causes) == len(soon)  # one per cause
    assert len(soon) <= len(demo_seed.SOON_TIMES)  # the rest stay 2+ days out


def test_seed_preview_without_a_seed_dir_uses_the_invented_orgs(preview):
    _empty()
    demo_seed.seed_preview(None)
    assert orgs.count and all(o.verified_at for o in orgs())  # not development
    assert any(m.status == "accepted" for m in matches())


def test_seed_preview_runs_in_a_preview_only(seed_dir):
    _empty()
    with pytest.raises(RuntimeError, match="preview only"):
        demo_seed.seed_preview(str(seed_dir))
    assert not orgs.count


def test_no_route_imports_the_demo_seed():
    routes = sorted((ROOT / "app" / "routes").rglob("*.py"))
    assert routes
    assert [f.name for f in routes if "demo_seed" in f.read_text()] == []


def test_seed_preview_only_fills_an_empty_database(preview, seed_dir):
    before = [o.name for o in orgs()]
    demo_seed.seed_preview(str(seed_dir))
    assert [o.name for o in orgs()] == before


# --- the demo's matches (config.demo_matches) ------------------------------------------

ON = {"CAUSEMATCH_DEMO_MATCHES": "1"}


def test_production_refuses_the_demo_matches_switch(tmp_path):
    for env in PRODUCTION:
        with pytest.raises(RuntimeError):
            demo_matches({**env, **ON})
        assert not demo_matches(env)
    assert demo_matches({**PREVIEW, **ON}) and not demo_matches(PREVIEW)
    assert demo_matches({"CAUSEMATCH_ENV": "development", **ON})
    real = {k: "x" for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                             "TWILIO_VERIFY_SID", "TWILIO_FROM_NUMBER",
                             "EMAIL_API_KEY", "EMAIL_FROM")}  # fmt: skip
    r = _start(tmp_path, CAUSEMATCH_ENV="production", CAUSEMATCH_FAKE_SMS="",
               CAUSEMATCH_FAKE_EMAIL="", **real, **ON)  # fmt: skip
    assert r.returncode != 0 and "CAUSEMATCH_DEMO_MATCHES=1 needs" in r.stderr


@pytest.fixture
def demo(preview, monkeypatch):
    """A preview with the demo's matches on, seeded with the invented orgs."""
    monkeypatch.setenv("CAUSEMATCH_DEMO_MATCHES", "1")
    _empty()
    return demo_seed.seed_preview(None) or demo_seed


def as_demo_volunteer():
    c = TestClient(app, headers={"Authorization": basic()})
    c.post("/login/send", data={"phone": demo_seed.ME_PHONE})
    r = c.post("/login/verify", data={"code": "000000"}, follow_redirects=False)
    assert r.headers["location"] == "/deck"
    (c.uid,) = [
        p.id
        for p in profiles(order_by="id")
        if (p.phone or "").startswith(data.DEMO_PHONE_PREFIX)
    ][-1:]
    return c


def test_each_visitor_of_the_demo_number_gets_their_own_sam(demo):
    a, b = as_demo_volunteer(), as_demo_volunteer()
    assert a.uid != b.uid
    assert profiles[a.uid].display_name == profiles[b.uid].display_name == "Sam"
    first = data.deck(a.uid, 1)[0]
    assert first["urgent"]  # the urgent call is still the first card
    before = [c["id"] for c in data.deck(b.uid, 50)]
    swiped = swipes.count_where("volunteer_id = ?", [a.uid])
    a.post("/swipe", data={"opening_id": first["id"], "liked": "0"})
    second = data.deck(a.uid, 1)[0]["id"]
    a.post("/swipe", data={"opening_id": second, "liked": "1"})
    assert swipes.count_where("volunteer_id = ?", [a.uid]) == swiped + 2
    assert [c["id"] for c in data.deck(b.uid, 50)] == before
    assert data.deck(b.uid, 1)[0]["id"] == first["id"]  # b's urgent card is still up


def test_a_new_demo_sam_starts_with_no_matches_and_a_full_deck(demo):
    uid = as_demo_volunteer().uid
    assert matches.count_where("volunteer_id = ?", [uid]) == 0
    assert not messages(where="match_id IN (SELECT id FROM match WHERE volunteer_id = ?)",
                        where_args=[uid])  # fmt: skip
    deck = data.deck(uid, 200)
    # the invented seed holds 7 openings (the real seed dir holds more than 40)
    assert len(deck) >= 7 and deck[0]["urgent"]


def test_demo_copies_are_capped_and_the_oldest_is_reused(demo, monkeypatch):
    monkeypatch.setattr(data, "DEMO_COPIES", 2)
    (seed_sam,) = profiles(where="phone = ?", where_args=[DEMO_PHONE])
    a, b = (as_demo_volunteer().uid for _ in range(2))
    for uid in (a, b):
        data.swipe(uid, data.deck(uid, 2)[1]["id"], True)  # a match and a swipe each
    c = as_demo_volunteer().uid
    phones = {p.id: p.phone for p in profiles() if p.id in (a, b, c)}
    assert set(phones) == {b, c} and phones[c] == f"{data.DEMO_PHONE_PREFIX}0000"
    for table, col in ((matches, "volunteer_id"), (swipes, "volunteer_id"),
                       (places, "user_id")):  # fmt: skip
        count = lambda uid, t=table, k=col: t.count_where(f"{k} = ?", [uid])
        assert count(a) == 0, table  # a's rows went with it
        assert count(seed_sam.id), table  # the seed's Sam keeps his
        assert count(b), table  # and so does the other copy


def test_without_the_switch_the_demo_number_is_the_seeds_sam(preview):
    _empty()
    demo_seed.seed_preview(None)
    (seed_sam,) = profiles(where="phone = ?", where_args=[DEMO_PHONE])
    c = TestClient(app, headers={"Authorization": basic()})
    c.post("/login/send", data={"phone": demo_seed.ME_PHONE})
    c.post("/login/verify", data={"code": "000000"})
    assert data.get_or_create_volunteer(DEMO_PHONE) == seed_sam.id
    assert not [
        p for p in profiles() if (p.phone or "").startswith(data.DEMO_PHONE_PREFIX)
    ]
    with pytest.raises(PermissionError):
        data.new_demo_volunteer()
    assert "Sam" in c.get("/profile").text


def test_the_demo_buttons_sign_in_each_role_in_one_tap(demo):
    assert demo_seed.COORDINATOR[0] == DEMO_COORDINATOR
    browser = TestClient(app, headers={"Authorization": basic()})
    page = browser.get("/login").text
    for text in ("Demo: you", "Try it as a volunteer", "Try it as a nonprofit"):
        assert text in page.replace("&#x27;", "'")
    org_page = browser.get("/org/login").text
    assert (
        "Try it as a nonprofit" in org_page and "Try it as a volunteer" not in org_page
    )
    before = {p.id for p in profiles()}
    r = browser.post("/login/demo", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/deck"
    (copy,) = [p for p in profiles() if p.id not in before]
    assert copy.phone.startswith(data.DEMO_PHONE_PREFIX) and copy.display_name == "Sam"
    assert "Urgent" in browser.get("/deck").text  # signed in, on their own deck
    pat = TestClient(app, headers={"Authorization": basic()})
    r = pat.post("/org/login/demo", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/org"
    assert pat.get("/org/inbox").status_code == 200
    assert pat.get("/deck", follow_redirects=False).headers["location"] == "/org"
    # a POST from another site is refused, like any form here
    evil = {"Authorization": basic(), "Origin": "https://evil.example"}
    assert TestClient(app, headers=evil).post("/login/demo").status_code == 403


def test_in_a_demo_the_coordinator_sees_demo_people_only(demo):
    """Anyone can become the demo coordinator, so a tester who signed up with their own
    number never shows in its inbox; a demo copy's like does."""
    from app.auth import normalize_phone

    assert {normalize_phone(p) for p in (demo_seed.ME_PHONE, demo_seed.NO_PHOTO_PHONE)}
    assert normalize_phone(demo_seed.NO_PHOTO_PHONE) in data.DEMO_SEEDED_PHONES
    pat = data.coordinator_by_email(DEMO_COORDINATOR)
    tester = TestClient(app, headers={"Authorization": basic()})
    tester.post("/login/send", data={"phone": "(213) 555-0188"})
    tester.post("/login/verify", data={"code": "000000"})
    (real,) = profiles(where="phone = ?", where_args=["+12135550188"])
    profiles.update(id=real.id, display_name="Realtester", onboarded_at="2026-01-01",
                    heard_from="skipped")  # fmt: skip
    sam = as_demo_volunteer()
    opening = demo_seed_first_opening(pat)
    before = data.org_pending_count(opening.org_id)
    for c, uid in ((tester, real.id), (sam, sam.uid)):
        r = c.post("/swipe", data={"opening_id": opening.id, "liked": "1"},
                   follow_redirects=False)  # fmt: skip
        assert r.status_code == 303, uid
    ids = {m.volunteer_id: m.id for m in matches(where="opening_id = ?",
                                                  where_args=[opening.id])}  # fmt: skip
    pending = {r["id"] for r in data.org_inbox(pat)["pending"]}
    assert ids[sam.uid] in pending and ids[real.id] not in pending
    pat_page = TestClient(app, headers={"Authorization": basic()})
    pat_page.post("/org/login/demo")
    inbox = pat_page.get("/org/inbox").text
    assert "Realtester" not in inbox and "Sam" in inbox
    assert data.org_pending_count(opening.org_id) == before + 1  # the copy only
    (m,) = matches(where="volunteer_id = ?", where_args=[real.id])
    with pytest.raises(PermissionError):  # nor accepted by id, so no chat or shift
        data.decide(pat, m.id, True)


def demo_seed_first_opening(coordinator):
    org_id = data.coordinator_org_ids(coordinator)[0]
    return openings(where="org_id = ? AND status = 'open'", where_args=[org_id],
                    order_by="id")[0]  # fmt: skip


def test_a_demo_makes_at_most_ten_copies_an_hour_per_address(demo):
    browser = TestClient(
        app, headers={"Authorization": basic()}, client=("198.51.100.80", 1)
    )
    codes = [browser.post("/login/demo", follow_redirects=False).status_code
             for _ in range(11)]  # fmt: skip
    assert codes == [303] * 10 + [429]
    other = TestClient(
        app, headers={"Authorization": basic()}, client=("198.51.100.81", 1)
    )
    assert other.post("/login/demo", follow_redirects=False).status_code == 303


def _like_a_team_card(client, uid):
    """Likes the first card a stand-in team runs, made long enough ago to be due."""
    card = next(
        c for c in data.deck(uid, 50)
        if not c["call_id"] and data.demo_team(openings[c["id"]].org_id)
    )  # fmt: skip
    client.post("/swipe", data={"opening_id": card["id"], "liked": "1"})
    (m,) = matches(
        where="volunteer_id = ? AND opening_id = ?", where_args=[uid, card["id"]]
    )
    assert m.status == "pending"  # not at once: a few seconds later
    early = (datetime.now(UTC) - data.DEMO_ACCEPT_AFTER).isoformat(timespec="seconds")
    matches.update(id=m.id, created_at=early)
    return m


def test_in_a_preview_a_demo_team_says_yes_with_a_welcome(demo):
    sam = as_demo_volunteer()
    m = _like_a_team_card(sam, sam.uid)
    page = sam.get("/deck").text
    assert "It&#x27;s a match!" in page or "It's a match!" in page
    assert f'href="/chat/{m.id}"' in page and matches[m.id].status == "accepted"
    (hi,) = messages(where="match_id = ?", where_args=[m.id])
    assert hi.body.startswith("Hi Sam! Thanks for signing up")


def test_in_a_preview_an_ordinary_volunteers_like_waits(user, demo):
    user.headers["Authorization"] = basic()
    uid = uid_of(user)
    m = _like_a_team_card(user, uid)
    assert "s a match!" not in user.get("/deck").text
    assert matches[m.id].status == "pending" and data.demo_accept_due(uid) == []


def _demo_opening_of_a_team(slots):
    o = next(
        o for o in openings(where="status = 'open'", order_by="id")
        if data.demo_team(o.org_id)
    )  # fmt: skip
    openings.update(id=o.id, slots=slots)
    return o


def test_five_likes_on_stand_in_openings_give_five_matches(demo):
    sam = as_demo_volunteer()
    liked = []
    for _ in range(5):
        card = next(
            c for c in data.deck(sam.uid, 50)
            if not c["call_id"] and data.demo_team(openings[c["id"]].org_id)
        )  # fmt: skip
        sam.post("/swipe", data={"opening_id": card["id"], "liked": "1"})
        liked.append(card["id"])
    early = (datetime.now(UTC) - data.DEMO_ACCEPT_AFTER).isoformat(timespec="seconds")
    for m in matches(where="volunteer_id = ?", where_args=[sam.uid]):
        assert m.status == "pending"
        matches.update(id=m.id, created_at=early)
    assert "s a match!" in sam.get("/deck").text.replace("&#x27;", "'")
    mine = matches(where="volunteer_id = ?", where_args=[sam.uid])
    assert len(mine) == 5 and {m.status for m in mine} == {"accepted"}


def test_many_demo_copies_matching_one_opening_leave_it_open(demo):
    o = _demo_opening_of_a_team(slots=1)
    team = data.demo_team(o.org_id)
    for _ in range(4):
        uid = as_demo_volunteer().uid
        m = data.swipe(uid, o.id, True)
        data.decide(team, m.id, True)
    assert matches.count_where("opening_id = ? AND status = 'accepted'", [o.id]) == 4
    assert openings[o.id].status == "open"
    assert o.id in {c["id"] for c in data.deck(as_demo_volunteer().uid, 200)}


def test_outside_a_demo_every_accepted_match_fills_an_opening(user):
    o = openings(where="status = 'open'", order_by="id")[0]
    openings.update(id=o.id, slots=1)
    uid = uid_of(user)
    profiles.update(id=uid, phone=f"{data.DEMO_PHONE_PREFIX}9999")  # looks like a copy
    m = data.swipe(uid, o.id, True)
    matches.update(id=m.id, status="accepted")
    data._sync_fill(o.id)
    assert openings[o.id].status == "filled"


def test_outside_a_demo_decide_counts_a_copy_like_phone(user):
    """Outside a demo a copy-like phone's accepted match still takes the slot: the gate
    in _filling() is what lets copies through, and only in a demo."""
    o = openings(where="status = 'open'", order_by="id")[0]
    openings.update(id=o.id, slots=1)
    copy = profiles.insert(
        Profile(
            role="volunteer",
            phone=f"{data.DEMO_PHONE_PREFIX}9999",
            created_at=db_now(),
        )
    ).id
    matches.insert(
        Match(
            opening_id=o.id, volunteer_id=copy, status="accepted", created_at=db_now()
        )
    )
    pending = data.swipe(uid_of(user), o.id, True)
    team = data.add_coordinator("boss@example.org", o.org_id, name="Boss")
    with pytest.raises(ValueError, match="no slots left"):
        data.decide(team, pending.id, True)


def test_in_a_demo_copies_never_fill_pats_side_or_the_urgent_card(demo):
    pat = data.coordinator_by_email(DEMO_COORDINATOR)
    row = next(r for r in data.org_inbox(pat)["pending"])
    o = openings[matches[row["id"]].opening_id]
    accepted = data.opening_counts(pat)[o.id]["accepted"]
    openings.update(id=o.id, slots=accepted + 1)  # one real spot left
    for _ in range(3):  # copies accepted on Pat's opening
        uid = as_demo_volunteer().uid
        data.decide(pat, data.swipe(uid, o.id, True).id, True)
    assert openings[o.id].status == "open"
    assert data.opening_counts(pat)[o.id]["accepted"] == accepted
    inbox = {r["id"]: r for r in data.org_inbox(pat)["pending"]}
    assert not inbox[row["id"]]["full"]
    assert data.open_spots(pat, o.id) == 1
    starts = (datetime.now(data.LA) + timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M")
    data.post_urgent_call(pat, o.id, starts, 1, False)  # still available
    data.decide(pat, row["id"], True)  # the seeded waiting volunteer is accepted
    assert openings[o.id].status == "filled"


def test_the_urgent_card_stays_first_for_every_copy(demo):
    for _ in range(4):
        sam = as_demo_volunteer()
        card = data.deck(sam.uid, 1)[0]
        assert card["urgent"]
        data.swipe(sam.uid, card["id"], True, call_id=card["call_id"])


# --- the deploy ----------------------------------------------------------------------


def test_the_preview_seed_stays_out_of_git_but_goes_to_cloud_build():
    r = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "-q", "preview-seed/orgs.json"],
        check=False,
        timeout=30,
    )
    assert r.returncode == 0  # ignored
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "preview-seed"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert tracked.stdout == ""
    rules = (ROOT / ".gcloudignore").read_text().splitlines()
    assert rules.index("#!include:.gitignore") < rules.index("!preview-seed/")


def _chat_bodies(match_id):
    return [
        m.body
        for m in messages(where="match_id = ?", where_args=[match_id], order_by="id")
    ]


def test_a_stand_in_org_answers_a_demo_copys_first_message_once(demo):
    sam = as_demo_volunteer()
    m = _like_a_team_card(sam, sam.uid)
    sam.get("/deck")  # the demo team says yes, with its welcome
    assert len(_chat_bodies(m.id)) == 1
    sam.post(f"/chat/{m.id}/send", data={"msg": "Can I bring a friend?"})
    first = data._first_name(profiles[sam.uid].display_name)
    assert _chat_bodies(m.id)[1:] == [
        "Can I bring a friend?",
        f"Thanks, {first}! We're looking forward to it. See you there.",
    ]
    sam.post(f"/chat/{m.id}/send", data={"msg": "Thanks!"})
    assert _chat_bodies(m.id)[-1] == "Thanks!" and len(_chat_bodies(m.id)) == 4


def test_pats_org_and_real_volunteers_get_no_stand_in_reply(demo):
    sam = as_demo_volunteer()
    pat = data.coordinator_by_email(demo_seed.COORDINATOR[0])
    o = next(
        o for o in openings(where="status = 'open'", order_by="id")
        if not data.demo_team(o.org_id)
    )  # fmt: skip
    m = data.swipe(sam.uid, o.id, True)
    data.decide(pat, m.id, True)
    sam.post(f"/chat/{m.id}/send", data={"msg": "Hello"})
    assert _chat_bodies(m.id) == ["Hello"]


def test_no_stand_in_reply_outside_demo_mode(demo, monkeypatch):
    sam = as_demo_volunteer()
    m = _like_a_team_card(sam, sam.uid)
    sam.get("/deck")
    monkeypatch.delenv("CAUSEMATCH_DEMO_MATCHES")
    sam.post(f"/chat/{m.id}/send", data={"msg": "Hi"})
    assert _chat_bodies(m.id)[-1] == "Hi" and len(_chat_bodies(m.id)) == 2
