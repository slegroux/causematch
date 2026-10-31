import atexit
import os
import shutil
import tempfile

# Must be set before app.db / main are imported.
_tmp = tempfile.mkdtemp()
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
os.environ["CAUSEMATCH_DB"] = os.path.join(_tmp, "test.db")
os.environ["CAUSEMATCH_FAKE_SMS"] = "1"
os.environ["CAUSEMATCH_FAKE_EMAIL"] = "1"
os.environ["PUBLIC_BASE_URL"] = "https://causematch.test"
os.environ["CAUSEMATCH_ENV"] = "test"
os.environ.pop("K_SERVICE", None)

import io
import itertools
import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest
import starlette.testclient
import uvicorn
from PIL import Image, ImageDraw

ORIGIN = os.environ["PUBLIC_BASE_URL"]
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "propublica"
PANTRY = "Sort and pack pantry boxes"  # org 0's seeded opening, the soonest


class SameOriginClient(starlette.testclient.TestClient):
    """A TestClient that sends our Origin, as a browser does on a form POST, an htmx request
    and a websocket handshake from one of our pages. Cross-site tests set or drop it."""

    def __init__(self, app, *args, headers=None, **kwargs):
        super().__init__(
            app, *args, headers={"Origin": ORIGIN, **(headers or {})}, **kwargs
        )


# Before any test module imports it: every test client is a same-origin one.
starlette.testclient.TestClient = SameOriginClient
TestClient = SameOriginClient

from app import data, notify, security
from app.auth import normalize_phone
from app.choices import ANY_DISTANCE_KM
from app.db import (
    Opening,
    blocks,
    causes,
    certificates,
    claim_requests,
    deck_empties,
    ensure_causes,
    login_tokens,
    matches,
    messages,
    notifications,
    now,
    openings,
    org_members,
    orgs,
    photos,
    places,
    profiles,
    rate_events,
    reports,
    retained_phones,
    seed_demo,
    shifts,
    swipes,
    urgent_calls,
)
from app.match import MODE_KMH
from app.routes import chat as chat_routes
from main import app


@pytest.fixture(autouse=True)
def fresh_db():
    """Every test starts from the same seeded rows, whatever ran before it."""
    for table in (
        profiles,
        openings,
        orgs,
        causes,
        org_members,
        login_tokens,
        swipes,
        matches,
        messages,
        shifts,
        notifications,
        reports,
        blocks,
        retained_phones,
        claim_requests,
        rate_events,
        places,
        deck_empties,
        photos,
        urgent_calls,
        certificates,
    ):
        table.delete_where()
    security.reset_buckets()
    chat_routes._sockets.clear()  # no socket outlives its test
    ensure_causes()
    seed_demo()
    notify.get_verifier().sent.clear()
    notify.get_emailer().sent.clear()
    notify.get_sms().sent.clear()


@pytest.fixture
def homelessness_off():
    """An inactive cause, for the tests of what one does. Any cause can be switched off;
    homelessness services was, until 2026-10-15, and its seeded openings stay."""
    causes.update(slug="homelessness-services", active=False)


# past app/demo_seed.py's numbers, (310) 555-0100 to 0103
_phones = itertools.count(3105550200)

FORM = {
    "first_name": "Sam",
    "adult": "1",
    "place1_where": "90012",  # Home: Chinatown's ZIP, by car within 30 minutes
    "place1_mode": "car",
    "place1_minutes": "30",
    "cause_food-insecurity": "love",
    "consent": "1",
}


def sign_in(client, phone=None):
    phone = phone or str(next(_phones))
    client.phone = normalize_phone(phone)
    client.post("/login/send", data={"phone": phone})
    r = client.post("/login/verify", data={"code": "000000"}, follow_redirects=False)
    assert r.status_code == 303
    return r


@pytest.fixture
def new_user():
    """Signed in, not yet onboarded."""
    c = TestClient(app)
    sign_in(c)
    return c


def deck_titles(user):
    """Every title in this volunteer's deck, best fit first (the page shows only the top card)."""
    return [c["title"] for c in data.deck(data.get_or_create_volunteer(user.phone), 50)]


HOME_KINDS = ("home place", "sign-up point")


def pytest_generate_tests(metafunc):
    """A test module with HOME_PLACES = True runs each test that uses `user` twice: with
    the Home place onboarding saves, and as a profile from before places."""
    if (
        getattr(metafunc.module, "HOME_PLACES", False)
        and "user" in metafunc.fixturenames
    ):
        metafunc.parametrize("user", HOME_KINDS, indirect=True)


@pytest.fixture
def user(new_user, request):
    """Signed in and onboarded, then: a car and weekly help set in Me, and Home at any
    distance, so every seeded opening is in reach. Home is either the place row onboarding
    saved ("home place") or, by default, the sign-up point a profile from before places
    reads ("sign-up point"); user.home says which. Move the volunteer with locate()."""
    kind = getattr(request, "param", "sign-up point")
    r = new_user.post("/onboarding", data=FORM, follow_redirects=False)
    assert r.headers["location"] == "/deck"
    uid = data.get_or_create_volunteer(new_user.phone)
    profiles.update(
        id=uid,
        radius_km=60,
        skills='["car"]',
        frequency="weekly",
        heard_from="skipped",
        # set in Me since onboarding no longer asks
        neighborhood="Chinatown",
        availability='["sat_morning", "sun_morning"]',
    )
    if kind == "home place":
        places.update(id=places(where="user_id = ?", where_args=[uid])[0].id,
                      max_minutes=0)  # fmt: skip
    else:
        places.delete_where("user_id = ?", [uid])
    new_user.home = kind
    return new_user


def locate(uid, lat=None, lon=None, radius_km=None):
    """Move a volunteer: the profile's point and radius, and their Home place row when
    they have one (by car, reaching exactly radius_km; 60 or more is any distance)."""
    moved = {"lat": lat, "lon": lon}
    moved = {k: v for k, v in moved.items() if v is not None}
    profiles.update(
        id=uid, **moved, **({} if radius_km is None else {"radius_km": radius_km})
    )
    rows = places(where="user_id = ?", where_args=[uid], order_by="id")
    if rows:
        if radius_km is not None:
            moved["mode"] = "car"
            moved["max_minutes"] = (
                0 if radius_km >= ANY_DISTANCE_KM else radius_km * 60 / MODE_KMH["car"]
            )
        places.update(id=rows[0].id, **moved)


@pytest.fixture
def anon():
    return TestClient(app)


def coordinator_for(org_index=0, email=None, role="coordinator"):
    """Profile id of a coordinator for the nth seeded org."""
    email = email or f"coord{org_index}@example.org"
    return (
        data.add_coordinator(
            email, orgs()[org_index].id, role, name="Pat Doe, coordinator"
        ),
        email,
    )


def last_link_token():
    """The token in the sign-in link of the last email sent."""
    _, _, body = notify.get_emailer().sent[-1]
    return re.search(r"token=([\w-]+)", body).group(1)


def org_sign_in(client, email, lands="/org"):
    """Sign in through the emailed link, the way a coordinator (or, lands="/admin", an
    admin) does."""
    client.post("/org/login/send", data={"email": email})
    token = last_link_token()
    r = client.post("/org/login/verify", data={"token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == lands
    return token


def admin_client(email="boss@example.org"):
    data.add_admin(email, "Boss Person")
    c = TestClient(app)
    org_sign_in(c, email, lands="/admin")
    return c


def volunteer(name="Sam"):
    """A signed-in, onboarded volunteer; c.uid is the profile id."""
    c = TestClient(app)
    sign_in(c)
    c.uid = data.get_or_create_volunteer(c.phone)
    profiles.update(
        id=c.uid,
        display_name=name,
        onboarded_at="2026-01-01T00:00:00",
        heard_from="skipped",  # the after-first-like card has been answered
    )
    return c


def coordinator(org_index=0):
    """A signed-in coordinator of the nth seeded org; c.uid is the profile id."""
    uid, email = coordinator_for(org_index)
    c = TestClient(app)
    org_sign_in(c, email)
    c.uid = uid
    return c


def uid_of(client):
    return data.get_or_create_volunteer(client.phone)


def pantry_opening():
    return openings(where="title = ?", where_args=[PANTRY])[0]


def close_all_openings():
    for o in openings():
        openings.update(id=o.id, status="closed")


def make_opening(title, **fields):
    """Insert an open opening of org 0 and return its row. Pass causes (a slug list or a
    JSON string), lat, lon, neighborhood and starts_at; any field overrides its default."""
    fields = {
        "org_id": orgs()[0].id,
        "title": title,
        "about": "",
        "skills": "[]",
        "remote": False,
        "recurrence": "one_time",
        "duration_minutes": "180",
        "slots": 3,
        "status": "open",
        "created_at": now(),
        **fields,
    }
    if isinstance(fields["causes"], list):
        fields["causes"] = json.dumps(fields["causes"])
    return openings.insert(Opening(**fields))


def jpeg(size=(1200, 900), exif=None, color=(200, 90, 60)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", exif=exif or Image.Exif())
    return buf.getvalue()


def png(size=(400, 120), fill=(20, 60, 140, 255), text_white=False):
    """A transparent logo: a coloured bar, or a white wordmark-ish bar."""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    color = (255, 255, 255, 255) if text_white else fill
    ImageDraw.Draw(im).rectangle((40, 40, size[0] - 40, size[1] - 40), fill=color)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def coord():
    """Signed-in coordinator of the first seeded org."""
    _, email = coordinator_for(0)
    c = TestClient(app)
    org_sign_in(c, email)
    c.org_id = orgs()[0].id
    return c


@pytest.fixture
def live_url():
    """The real app on a free port, in this process so it shares the test DB and the fakes."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    # The browser's Origin is this server's, so it is the configured one while it runs.
    os.environ["PUBLIC_BASE_URL"] = url
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    yield url
    server.should_exit = True
    thread.join(timeout=10)
    os.environ["PUBLIC_BASE_URL"] = ORIGIN
    assert not thread.is_alive()
