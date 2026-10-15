import atexit
import os
import shutil
import tempfile

# Must be set before app.db / main are imported.
_tmp = tempfile.mkdtemp()
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
os.environ["CAUSEMATCH_DB"] = os.path.join(_tmp, "test.db")
os.environ["CAUSEMATCH_FAKE_SMS"] = "1"
os.environ["CAUSEMATCH_ENV"] = "test"
os.environ.pop("K_SERVICE", None)

import itertools

import pytest
from starlette.testclient import TestClient

from app import notify
from app.auth import normalize_phone
from app.db import causes, ensure_causes, openings, orgs, profiles, seed_demo
from main import app


@pytest.fixture(autouse=True)
def fresh_db():
    """Every test starts from the same seeded rows, whatever ran before it."""
    for table in (profiles, openings, orgs, causes):
        table.delete_where()
    ensure_causes()
    seed_demo()
    notify.get_verifier().sent.clear()


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
