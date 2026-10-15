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
import uvicorn
from starlette.testclient import TestClient

from app import data, notify
from app.auth import normalize_phone
from app.db import (
    causes,
    ensure_causes,
    login_tokens,
    matches,
    openings,
    org_members,
    orgs,
    profiles,
    seed_demo,
    swipes,
)
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
    ):
        table.delete_where()
    ensure_causes()
    seed_demo()
    notify.get_verifier().sent.clear()
    notify.get_emailer().sent.clear()


_phones = itertools.count(3105550100)

FORM = {
    "first_name": "Sam",
    "adult": "1",
    "zip": "90012",
    "neighborhood": "Chinatown",
    "travel": "30",
    "causes": ["food-insecurity"],
    "availability": ["weekend_morning"],
    "skills": ["car"],
    "frequency": "weekly",
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


@pytest.fixture
def user(new_user):
    """Signed in and onboarded."""
    r = new_user.post("/onboarding", data=FORM, follow_redirects=False)
    assert r.headers["location"] == "/deck"
    return new_user


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
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive()
