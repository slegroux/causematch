"""CAUSEMATCH_ENV=preview: a production deploy behind a password, with fake sign-in, the
demo's data and a banner. Production stays exactly as strict."""

import base64
import io
import json
import os
import re
import subprocess
import sys

import pytest
from conftest import ROOT, coordinator_for
from PIL import Image
from starlette.testclient import TestClient
from test_chat import Chat, bubble, one_loop, refused, say  # noqa: F401  one_loop: fixture

from app import demo_seed, notify, security
from app.auth import session_cookie, session_secret
from app.config import (
    contact_email,
    demo_banner,
    is_dev,
    is_preview,
    is_production,
    phone_hash_key,
    public_base_url,
    require_env,
    tasks_token,
)
from app.db import matches, openings, orgs, profiles, urgent_calls
from app.ui import DEMO_NOTICE, PREVIEW_NOTICE
from main import app

PASSWORD = "p" * 20  # low entropy on purpose: the secret scan reads tests too
SECRET = "s" * 32
PREVIEW = {"CAUSEMATCH_ENV": "preview", "PREVIEW_PASSWORD": PASSWORD}
CLOUD_RUN = {"K_SERVICE": "causematch-preview"}
PRODUCTION = (
    {"CAUSEMATCH_ENV": "production"},
    CLOUD_RUN,
    {**CLOUD_RUN, "CAUSEMATCH_ENV": "development"},
)


def basic(password=PASSWORD, user="preview"):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@pytest.fixture
def preview(monkeypatch):
    """This process in a preview: the password, and the secrets production requires."""
    for k, v in {
        **PREVIEW,
        "PHONE_HASH_KEY": SECRET,
        "TASKS_TOKEN": SECRET,
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
    for headers in ({}, {"Authorization": basic("w" * 20)}):
        c = TestClient(app, headers=headers)
        for path in EVERYWHERE:
            security.reset_buckets()  # wrong ones count: test_guessing_...
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 401, path
            assert r.headers["www-authenticate"] == (
                'Basic realm="Causematch preview", charset="UTF-8"'
            )
            assert r.headers["x-robots-tag"] == "noindex, nofollow"
            assert "Causematch" not in r.text  # nothing of the app
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
    for k, v in {**PREVIEW, "PHONE_HASH_KEY": SECRET, "TASKS_TOKEN": SECRET}.items():
        monkeypatch.setenv(k, v)
    assert refused(chat.vol, chat.vol_ws)
    chat.vol.headers["Authorization"] = basic("w" * 20)
    assert refused(chat.vol, chat.vol_ws)
    chat.vol.headers["Authorization"] = basic()
    with chat.vol.websocket_connect(chat.vol_ws) as ws:
        say(ws, "hello from the preview")
        assert "hello from the preview" in bubble(ws)


def test_outside_a_preview_there_is_no_password(anon):
    r = anon.get("/login")
    assert r.status_code == 200 and "x-robots-tag" not in r.headers
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
    demo volunteer's "I'm free today" deck isn't just the urgent call."""
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
