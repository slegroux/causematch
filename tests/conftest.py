import os
import tempfile

# Must be set before app.db / main are imported.
os.environ["CAUSEMATCH_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["CAUSEMATCH_FAKE_SMS"] = "1"
os.environ.pop("CAUSEMATCH_ENV", None)

import itertools

import pytest
from starlette.testclient import TestClient

from main import app

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
    client.post("/login/send", data={"phone": phone})
    r = client.post(
        "/login/verify", data={"phone": phone, "code": "000000"}, follow_redirects=False
    )
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
