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

import itertools
import re
import socket
import threading
import time

import pytest
import starlette.testclient
import uvicorn

ORIGIN = os.environ["PUBLIC_BASE_URL"]


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
    blocks,
    causes,
    claim_requests,
    deck_empties,
    ensure_causes,
    login_tokens,
    matches,
    messages,
    notifications,
    openings,
    org_members,
    orgs,
    places,
    profiles,
    rate_events,
    reports,
    retained_phones,
    seed_demo,
    shifts,
    swipes,
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
    ):
        table.delete_where()
    security.reset_buckets()
    chat_routes._sockets.clear()  # no socket outlives its test
    ensure_causes()
    seed_demo()
    notify.get_verifier().sent.clear()
    notify.get_emailer().sent.clear()


@pytest.fixture
def homelessness_off():
    """An inactive cause, for the tests of what one does. Any cause can be switched off;
    homelessness services was, until 2026-10-15, and its seeded openings stay."""
    causes.update(slug="homelessness-services", active=False)


_phones = itertools.count(3105550100)

FORM = {
    "first_name": "Sam",
    "adult": "1",
    "place1_where": "90012",  # Home: Chinatown's ZIP, by car within 30 minutes
    "place1_mode": "car",
    "place1_minutes": "30",
    "neighborhood": "Chinatown",
    "cause_food-insecurity": "love",
    "availability": ["sat_morning", "sun_morning"],
    "heard_from": "public",
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
    profiles.update(id=uid, radius_km=60, skills='["car"]', frequency="weekly")
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


def org_sign_in(client, email):
    """Sign in through the emailed link, the way a coordinator does."""
    client.post("/org/login/send", data={"email": email})
    _, _, body = notify.get_emailer().sent[-1]
    token = re.search(r"token=([\w-]+)", body).group(1)
    r = client.post("/org/login/verify", data={"token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/org"
    return token


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
