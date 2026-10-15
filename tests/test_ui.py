"""Every page's form fields look like fields, the demo banner, and scripts/demo.py."""

import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import pytest
from conftest import coordinator_for, sign_in
from starlette.routing import Route
from starlette.testclient import TestClient
from test_claim import admin_client

from app import data
from app.config import demo_banner
from app.db import openings, orgs
from app.ui import DEMO_NOTICE
from main import app

ROOT = Path(__file__).resolve().parent.parent
# Franken UI's class for each kind of field; an <input> with none of them draws no box
FIELD_CLASSES = {"uk-input", "uk-checkbox", "uk-radio", "uk-range"}
# GET routes that render no page of fields
NOT_PAGES = {
    "/",
    "/favicon.ico",
    "/admin/metrics.csv",
    "/photos/{key}.jpg",
    "/photos/thumb/{key}.jpg",
}


class Fields(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = []

    def handle_starttag(self, tag, attrs):
        if tag in ("input", "textarea", "select"):
            a = dict(attrs)
            self.fields.append(
                (tag, a.get("type", ""), set((a.get("class") or "").split()), a)
            )


def unstyled(html):
    """Every field without its Franken class, as (tag, attributes)."""
    p = Fields()
    p.feed(html)
    bad = []
    for tag, kind, cls, attrs in p.fields:
        ok = {
            "input": kind == "hidden" or bool(cls & FIELD_CLASSES),
            "textarea": "uk-textarea" in cls,
            "select": "uk-select" in cls,
        }[tag]
        if not ok:
            bad.append((tag, attrs))
    return bad, len(p.fields)


@pytest.fixture
def pages(anon, user, coord):
    """(client, path) for every page, with a match so its chat and safety pages exist."""
    uid = data.get_or_create_volunteer(user.phone)
    opening = next(
        o for o in openings() if o.org_id == coord.org_id and o.status == "open"
    )
    m = data.swipe(uid, opening.id, True)
    data.decide(data.coordinator_by_email(coordinator_for(0)[1]), m.id, True)
    newcomer = TestClient(app)
    sign_in(newcomer)
    admin = admin_client()
    return [
        (anon, "/login"),
        (anon, "/org/login"),
        (anon, "/org/login/verify"),
        (anon, "/org/claim"),
        (anon, "/privacy"),
        (newcomer, "/onboarding"),
        (user, "/deck"),
        (user, "/matches"),
        (user, "/history"),
        (user, "/profile"),
        (user, "/profile/delete"),
        (user, "/me/profile"),
        (user, f"/chat/{m.id}"),
        (user, f"/matches/{m.id}/withdraw"),
        (user, f"/matches/{m.id}/report"),
        (user, f"/matches/{m.id}/block"),
        (coord, "/org"),
        (coord, "/org/inbox"),
        (coord, "/org/openings/new"),
        (coord, "/org/shifts"),
        (coord, f"/org/chat/{m.id}"),
        (coord, f"/org/matches/{m.id}/shifts/new"),
        (coord, f"/org/matches/{m.id}/report"),
        (coord, f"/org/matches/{m.id}/block"),
        (admin, "/admin"),
        (admin, "/admin/metrics"),
        (admin, "/admin/reports"),
        (admin, "/admin/blocks"),
        (admin, "/admin/claims"),
        (admin, "/admin/orgs"),
    ]


def test_pages_cover_every_get_route(pages):
    """A new page joins the list above, so its fields are checked too."""
    gets = {
        r.path
        for r in app.routes
        if isinstance(r, Route) and "GET" in (r.methods or ())
    }
    uncovered = {
        g
        for g in gets - NOT_PAGES
        if not any(re.fullmatch(re.sub(r"\{\w+\}", "[^/]+", g), p) for _, p in pages)
    }
    assert uncovered == set()


def test_every_field_has_its_franken_class(pages):
    seen = 0
    for client, path in pages:
        r = client.get(path)
        assert r.status_code == 200, path
        bad, n = unstyled(r.text)
        assert bad == [], path
        seen += n
    assert seen > 50  # the pages above really hold forms


def test_code_step_field_has_its_class(anon):
    r = anon.post("/login/send", data={"phone": "3105550177"})
    assert 'name="code"' in r.text
    assert unstyled(r.text)[0] == []


def test_sign_in_pages_keep_their_labels_and_buttons(anon):
    login = anon.get("/login").text
    assert 'aria-label="Mobile number"' in login and "Text me a code" in login
    org = anon.get("/org/login").text
    assert 'aria-label="Email"' in org and "Email me a link" in org


def test_demo_banner_shows_on_every_page_only_when_on(pages, monkeypatch):
    sample = {
        "/login",
        "/org/login",
        "/org/login/verify",
        "/deck",
        "/matches",
        "/org",
        "/org/inbox",
        "/admin/claims",
        "/admin/orgs",
    }
    off = [(client, path) for client, path in pages if path in sample]
    assert len(off) == len(sample)
    for client, path in off:
        assert DEMO_NOTICE not in client.get(path).text, path
    monkeypatch.setenv("CAUSEMATCH_DEMO_BANNER", "1")
    for client, path in pages:
        assert DEMO_NOTICE in client.get(path).text, path


def test_demo_banner_is_refused_in_production():
    on = {"CAUSEMATCH_DEMO_BANNER": "1"}
    assert demo_banner({**on, "CAUSEMATCH_ENV": "development"})
    assert not demo_banner({"CAUSEMATCH_ENV": "development"})
    for prod in ({"CAUSEMATCH_ENV": "production"}, {"K_SERVICE": "causematch"}):
        with pytest.raises(RuntimeError):
            demo_banner({**on, **prod})
        assert not demo_banner(prod)


# --- scripts/demo.py ---------------------------------------------------------------


def _demo():
    spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("prod", [{"CAUSEMATCH_ENV": "production"}, {"K_SERVICE": "x"}])
def test_demo_refuses_production(prod, tmp_path):
    env = {
        k: v for k, v in os.environ.items() if k not in ("CAUSEMATCH_ENV", "K_SERVICE")
    }
    r = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "demo.py"),
            "--db",
            str(tmp_path / "x.db"),
        ],
        env={**env, **prod},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode != 0
    assert "local demos only" in r.stderr
    assert not (tmp_path / "x.db").exists()


def test_demo_first_start_reads_free_text_times():
    first_start = _demo().first_start
    today = date(2026, 10, 15)  # a Thursday
    assert first_start("Saturdays 9:30am-noon", today=today) == "2026-10-17T09:30"
    assert first_start("Wednesdays 5-7pm (4-5:30pm Nov-Mar)", today=today) == (
        "2026-10-21T17:00"
    )
    assert first_start("Daily, 12-2pm or 3-5pm", today=today) == "2026-10-17T12:00"
    assert first_start("Third Saturday, 10am-noon", today=today) == "2026-10-17T10:00"
    # no day, no time ("3-hour" is not a time): a weekend morning, at least 2 days out
    assert first_start("3-hour shifts", 0, today=today) == "2026-10-17T09:00"
    assert first_start("", 1, today=today) == "2026-10-18T10:00"
    assert first_start("", 2, today=today) == "2026-10-24T09:00"


@pytest.mark.parametrize(
    "text, starts",
    [
        # a decimal duration is not a time; the am/pm time later on is
        (
            "Mon/Tue/Thu/Fri, 1.5-2 hour slots between 9:30am and 3pm",
            "2026-10-19T09:30",
        ),
        ("Thursdays 7-9pm", "2026-10-22T19:00"),  # the start takes the end's pm
        ("Tues 11-1pm", "2026-10-20T11:00"),  # ... unless that runs past the end
        ("Sundays 10-12pm", "2026-10-18T10:00"),
        ("Thurs 10am", "2026-10-22T10:00"),  # short day names
        ("Weds 9-noon", "2026-10-21T09:00"),
        ("2-3 hours, Saturdays", "2026-10-17T09:00"),  # a duration: no time given
        ("monthly", "2026-10-17T09:00"),  # not a Monday
    ],
)
def test_demo_first_start_reads_am_pm_ranges_and_skips_durations(text, starts):
    assert _demo().first_start(text, today=date(2026, 10, 15)) == starts


def test_demo_seed_file_orgs_are_verified_samples():
    before = {o.id for o in orgs()}
    _demo().load_orgs(
        [
            {
                "name": "Invented Riverside Helpers",
                "ein": "000000001",
                "cause": "environment-parks",
                "mission": "An invented mission.",
                "neighborhood": "Elysian Valley",
                "zip": "90039",
                "lat": 34.1,
                "lon": -118.25,
                "openings": [
                    {
                        "title": "Pick up litter",
                        "about": "Bags provided.",
                        "skills": ["physical", "juggling"],
                        "typical_time": "Saturdays 8-11am",
                        "recurrence": "one-time",
                        "orientation_required": "",
                    }
                ],
            }
        ]
    )
    (org,) = [o for o in orgs() if o.id not in before]
    assert org.verified_at and org.about == "An invented mission."
    (o,) = openings(where="org_id = ?", where_args=[org.id])
    assert o.title == "Sample: Pick up litter"
    assert json.loads(o.skills) == ["physical"]
    assert o.recurrence == "one_time" and o.starts_at.endswith("T08:00")
    assert 4 <= o.slots <= 8 and o.status == "open"


def test_demo_refuses_a_seed_file_inside_the_repo(tmp_path, monkeypatch):
    # a copy, so a regressed guard can't leave the demo's environment behind
    monkeypatch.setattr(os, "environ", dict(os.environ))
    with pytest.raises(SystemExit):
        _demo().configure(5999, tmp_path / "x.db", ROOT / "README.md")


def test_screenshots_of_real_orgs_stay_out_of_the_repo(tmp_path):
    out = ROOT / "screens-refused-by-test"
    r = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "screenshots.py"),
            "--out",
            str(out),
            "--seed-file",
            str(tmp_path / "orgs.json"),
        ],
        env={k: v for k, v in os.environ.items() if k != "K_SERVICE"},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode != 0 and "outside the repo" in r.stderr
    assert not out.exists()
    # and screenshots written into the repo anyway are never committable
    for png in ("screens-x/v01.png", "docs/screenshots/a.png", "screens/o1.png"):
        ignored = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", png],
            check=False,
        )
        assert ignored.returncode == 0, png
