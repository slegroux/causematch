"""Every page's form fields look like fields, the demo banner, and scripts/demo.py."""

import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import date
from html.parser import HTMLParser

import pytest
from conftest import (
    ROOT,
    admin_client,
    close_all_openings,
    coordinator_for,
    sign_in,
    uid_of,
    volunteer,
)
from starlette.routing import Route
from starlette.testclient import TestClient

from app import data, demo_seed
from app.config import demo_banner
from app.db import blocks, now, openings, org_members, orgs, profiles, shifts
from app.ui import DEMO_NOTICE
from main import app

# every page check runs twice: with the Home place onboarding saves, and as a profile
# from before places (conftest.pytest_generate_tests)
HOME_PLACES = True
# Franken UI's class for each kind of field; an <input> with none of them draws no box
FIELD_CLASSES = {"uk-input", "uk-checkbox", "uk-radio", "uk-range"}
# GET routes that render no page of fields
NOT_PAGES = {
    "/",
    "/favicon.ico",
    "/admin/metrics.csv",
    "/photos/{key}.jpg",
    "/photos/thumb/{key}.jpg",
    "/shifts/{shift_id}.ics",
    "/hours/certificate/{cert_id}.pdf",
    "/org/zip",
    "/org/claim/search",  # JSON for static/claim.js
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
    uid = uid_of(user)
    opening = next(
        o for o in openings() if o.org_id == coord.org_id and o.status == "open"
    )
    m = data.swipe(uid, opening.id, True)
    data.decide(data.coordinator_by_email(coordinator_for(0)[1]), m.id, True)
    newcomer = TestClient(app)
    sign_in(newcomer)
    liker = volunteer("Lee")  # liked once, not yet asked the after-first-like card
    profiles.update(id=liker.uid, heard_from="")
    admin = admin_client()
    (shift,) = shifts(where="match_id = ? AND status = 'scheduled'", where_args=[m.id])
    shifts.insert(  # confirmed hours, so My hours offers a certificate and one exists
        match_id=m.id,
        starts_at="2026-10-03T09:00",
        status="completed",
        kind="added",
        created_by=shift.created_by,
        confirmed_minutes=180,
        marked_by=shift.created_by,
        marked_at=now(),  # inside the correction window: "Marked this week"
    )
    shifts.insert(  # started, not marked: "Did they come?"
        match_id=m.id,
        starts_at="2026-10-02T09:00",
        status="scheduled",
        kind="added",
        created_by=shift.created_by,
        duration_minutes=120,
    )
    profiles.update(id=uid, last_name="Lee")
    certificate = data.issue_certificate(uid, opening.org_id, "all")["id"]
    return [
        (anon, "/login"),
        (anon, "/org/login"),
        (anon, "/org/login/verify"),
        (anon, "/org/claim"),
        (anon, "/privacy"),
        (anon, f"/urgent/off/{data.urgent_off_key(uid)}"),
        (anon, f"/check/{certificate}"),
        (user, f"/hours/certificate/{certificate}/withdraw"),
        (newcomer, "/onboarding"),
        (liker, "/onboarding/more"),
        (user, "/deck"),
        (user, "/matches"),
        (user, "/history"),
        (user, "/hours"),
        (user, f"/hours/certificate/{certificate}"),
        (user, "/profile"),
        (user, "/me/preferences"),
        (user, "/profile/delete"),
        (user, "/me/profile"),
        (user, f"/chat/{m.id}"),
        (user, f"/matches/{m.id}/withdraw"),
        (user, f"/matches/{m.id}/report"),
        (user, f"/matches/{m.id}/block"),
        (user, f"/shifts/{shift.id}/cancel"),
        (user, f"/shifts/{shift.id}/confirm"),
        (coord, "/org"),
        (coord, "/org/inbox"),
        (coord, "/org/openings/new"),
        (coord, "/org/shifts"),
        (coord, "/org/organization"),
        (coord, f"/org/openings/{opening.id}/media"),
        (coord, f"/org/openings/{opening.id}/urgent"),
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


# --- scripts/demo.py and app/demo_seed.py ---------------------------------------------


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
    first_start = demo_seed.first_start
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
    assert demo_seed.first_start(text, today=date(2026, 10, 15)) == starts


def test_demo_seed_file_orgs_are_verified_samples():
    before = {o.id for o in orgs()}
    demo_seed.load_orgs(
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


def in_git():
    return (
        subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def test_screenshot_folders_are_never_committable():
    if not in_git():
        pytest.skip("not a git checkout (an export or an sdist): nothing to commit to")
    for png in ("screens-x/v01.png", "docs/screenshots/a.png", "screens/o1.png"):
        ignored = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", png],
            check=False,
        )
        assert ignored.returncode == 0, png


def test_real_org_data_stays_out_of_every_checkout_of_this_repo(tmp_path):
    inside = _demo().inside_this_repo
    assert inside(ROOT / "screens") and inside(ROOT / "a" / "b.json")
    assert not inside(tmp_path / "screens")
    if not in_git():
        pytest.skip("not a git checkout: no other checkout to compare with")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.org"]
    # another checkout of this repo (say the primary one, seen from a worktree)
    clone = tmp_path / "clone"
    subprocess.run(
        [*git, "clone", "-q", "--no-checkout", str(ROOT), str(clone)], check=True
    )
    assert inside(clone / "screens" / "v01.png")
    # an unrelated repository is fine
    other = tmp_path / "other"
    subprocess.run([*git, "init", "-q", str(other)], check=True)
    subprocess.run(
        [*git, "-C", str(other), "commit", "-q", "--allow-empty", "-m", "x"],
        check=True,
    )
    assert not inside(other / "screens")


# --- the nonprofit's pages ----------------------------------------------------------


class Joined(HTMLParser):
    """Links that render as one word: an <a> straight after an </a>, nothing between,
    neither one with a side margin, inside a parent with no gap between its children
    (any of which would set them apart)."""

    VOID = frozenset({"input", "img", "br", "hr", "meta", "link", "source", "wbr"})

    def __init__(self):
        super().__init__()
        self.stack, self.joined, self.last, self.text = [], [], None, ""
        self.link = ""  # the class of the last link opened

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class") or ""
        if tag == "a" and self.last == "/a":
            parent = self.stack[-1] if self.stack else ""
            apart = (
                re.search(r"(^|\s)(gap|gap-x|space-x)-", parent)
                or re.search(r"\bm[rx]-[1-9]", self.link)
                or re.search(r"\bm[lx]-[1-9]", cls)
            )
            if not apart:
                self.joined.append(self.text)
        if tag == "a":
            self.link = cls
        if tag not in self.VOID:
            self.stack.append(cls)
        self.last = tag

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()
        self.last = "/" + tag

    def handle_data(self, data):
        self.last, self.text = None, data


def test_no_run_together_links(pages):
    org_pages = [(c, p) for c, p in pages if p.startswith("/org")]
    assert len(org_pages) >= 8
    for client, path in org_pages:
        html = client.get(path).text
        j = Joined()
        j.feed(html)
        assert j.joined == [], path
        assert "shiftReport" not in html, path


def test_org_pages_share_the_top_nav(pages, coord):
    where = {
        "/org": "Openings",
        "/org/inbox": "Inbox",
        "/org/shifts": "Shifts",
        "/org/openings/new": "Openings",
    }
    for path, tab in where.items():
        html = coord.get(path).text
        assert 'aria-label="Nonprofit"' in html, path
        current = re.search(r'<a [^>]*aria-current="page"[^>]*>\s*([A-Za-z]+)', html)
        assert current and current[1] == tab, path


def test_dashboard_counts_waiting_likes_and_fills_the_bar(user, coord):
    uid = uid_of(user)
    coord_uid = data.coordinator_by_email(coordinator_for(0)[1])
    a = next(o for o in openings() if o.org_id == coord.org_id and o.status == "open")
    m = data.swipe(uid, a.id, True)
    assert data.opening_counts(coord_uid)[a.id] == {"pending": 1, "accepted": 0}
    page = coord.get("/org").text
    assert "1 waiting for you" in page
    assert re.search(r'<nav[^>]*aria-label="Nonprofit".*?>1<', page, re.DOTALL)
    data.decide(coord_uid, m.id, True)
    assert data.opening_counts(coord_uid)[a.id] == {"pending": 0, "accepted": 1}
    page = coord.get("/org").text
    assert f"1 of {a.slots} spots filled" in page
    assert f'<progress value="1" max="{a.slots}"' in page


def test_upcoming_shifts_say_upcoming_not_unmarked(pages, coord):
    html = coord.get("/org/shifts").text
    upcoming = html.split(">Upcoming</h2>")[1]
    assert "Upcoming" in upcoming and "Not marked yet" not in upcoming


# --- admin, privacy and error pages ---------------------------------------------------


def test_admin_pages_share_the_top_nav():
    admin = admin_client()
    for path, tab in {
        "/admin": "Admin",
        "/admin/metrics": "Metrics",
        "/admin/reports": "Reports",
        "/admin/orgs": "Orgs",
        "/admin/claims": "Claims",
        "/admin/blocks": "Blocks",
    }.items():
        html = admin.get(path).text
        assert 'aria-label="Admin"' in html, path
        current = re.search(r'<a [^>]*aria-current="page"[^>]*>\s*([A-Za-z]+)', html)
        assert current and current[1] == tab, path
    assert 'href="/admin/metrics.csv"' in admin.get("/admin/metrics").text


def test_not_found_and_refusal_pages_wear_the_shared_look(anon):
    r = anon.get("/no-such-page")
    assert r.status_code == 404
    assert "Page not found" in r.text and 'href="/"' in r.text
    assert "<title>Page not found - Causematch</title>" in r.text
    assert "content-security-policy" in r.headers
    assert r.headers["cache-control"] == "no-store"
    r = anon.get("/static/no-such-file.css")  # a fetch for a file, not a page
    assert r.status_code == 404 and "<html" not in r.text
    assert "uk-theme-warm" in anon.get("/static/theme.js").text  # the page's theme
    from app import security

    page = security.too_many().body.decode()
    assert "rounded-3xl" in page and "cause<span class='text-brand'>match" in page
    assert "Too many tries" in page and "Back to Causematch" in page


def second_opening(first, title="Second opening"):
    return openings.insert(
        {**{k: v for k, v in first.__dict__.items() if k != "id"}, "title": title}
    )


def badge(html):
    """The Inbox badge's number in the nonprofit nav, 0 when there is none."""
    nav = re.search(r'<nav[^>]*aria-label="Nonprofit".*?</nav>', html, re.DOTALL)[0]
    found = re.search(r">(\d+)<span[^>]*>\s*waiting", nav)
    return int(found[1]) if found else 0


def test_opening_counts_hide_exactly_what_the_inbox_hides(coord):
    """One waiting like hidden by each of the inbox's rules, plus two it shows: the
    dashboard's cards, its badge, the inbox page and org_inbox all count the same two."""
    coord_uid = data.coordinator_by_email(coordinator_for(0)[1])
    first = openings(where="org_id = ? AND status = 'open'", where_args=[coord.org_id])[
        0
    ]
    second = second_opening(first)
    vol = iter(range(191, 199))

    def like(opening):
        uid = data.get_or_create_volunteer(f"+13105550{next(vol)}")
        data.swipe(uid, opening.id, True)
        return uid

    like(first), like(second)  # shown
    # org_blind: liked while the org blocked them; hidden even once an admin lifts it
    blind = like(first)
    data.block_volunteer(coord_uid, data.my_matches(blind)[0]["id"])
    data.swipe(blind, second.id, True)
    admin_uid = data.add_admin("boss@example.org", "Boss Person")
    for b in blocks(where="blocked_user_id = ?", where_args=[blind]):
        data.unblock(admin_uid, b.id)
    # a block row each way, with the like still waiting
    blocks.insert(
        blocker_id=coord_uid,
        blocked_user_id=like(second),
        blocked_org_id=coord.org_id,
        created_at=now(),
    )
    blocks.insert(
        blocker_id=like(second),
        blocked_user_id=None,
        blocked_org_id=coord.org_id,
        created_at=now(),
    )
    profiles.update(id=like(second), deleted_at=now())  # a deleted volunteer
    profiles.update(id=like(second), suspended_at=now())  # a suspended one

    counts = data.opening_counts(coord_uid)
    assert counts[first.id]["pending"] == counts[second.id]["pending"] == 1
    dashboard, inbox = coord.get("/org").text, coord.get("/org/inbox").text
    assert dashboard.count("1 waiting for you") == 2
    assert badge(dashboard) == badge(inbox) == data.org_waiting(coord_uid) == 2
    assert inbox.count("/decide") == len(data.org_inbox(coord_uid)["pending"]) == 2


def test_each_coordinator_counts_only_their_own_orgs(coord):
    c0 = data.coordinator_by_email(coordinator_for(0)[1])
    c1 = data.coordinator_by_email(coordinator_for(1)[1])
    both = data.coordinator_by_email(coordinator_for(0, "both@example.org")[1])
    org_members.insert(org_id=orgs()[1].id, user_id=both, role="coordinator")
    mine = {
        org.id: {o.id for o in openings(where="org_id = ?", where_args=[org.id])}
        for org in orgs()[:2]
    }
    for org_id in mine:
        o = openings(where="org_id = ? AND status = 'open'", where_args=[org_id])[0]
        data.swipe(data.get_or_create_volunteer(f"+1310555017{org_id}"), o.id, True)
    o0, o1 = orgs()[0].id, orgs()[1].id
    assert set(data.opening_counts(c0)) == mine[o0] and data.org_waiting(c0) == 1
    assert set(data.opening_counts(c1)) == mine[o1] and data.org_waiting(c1) == 1
    assert set(data.opening_counts(both)) == mine[o0] | mine[o1]
    assert data.org_waiting(both) == 2
    assert badge(coord.get("/org").text) == 1  # org 1's like never reaches org 0


def test_org_and_admin_refusals_wear_the_shared_look(coord):
    theirs = next(o for o in openings() if o.org_id != coord.org_id)
    r = coord.post(f"/org/openings/{theirs.id}/close")
    assert (
        r.status_code == 403 and "Not available" in r.text and "rounded-3xl" in r.text
    )
    r = coord.post("/org/matches/1/decide", data={"decision": "maybe"})
    assert r.status_code == 400 and "Something was missing" in r.text


def test_admin_metrics_show_the_empty_deck_count(user):
    admin = admin_client()
    assert "Empty decks: 0 views by 0 volunteers" in admin.get("/admin/metrics").text
    close_all_openings()
    assert "seen every opening" in user.get("/deck").text
    assert "Empty decks: 1 views by 1 volunteers" in admin.get("/admin/metrics").text


class Labels(HTMLParser):
    """Every label's `for`, every id, and the text-like fields with how they are named."""

    def __init__(self):
        super().__init__()
        self.fors, self.ids, self.fields, self.wrapped = [], [], [], 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.append(a["id"])
        if tag == "label":
            if a.get("for"):
                self.fors.append(a["for"])
            else:
                self.wrapped += 1
        if tag in ("input", "select", "textarea") and a.get("type") not in (
            "hidden",
            "submit",
            "checkbox",
            "radio",
        ):
            self.fields.append(
                (a.get("name"), a.get("id"), a.get("aria-label"), self.wrapped)
            )

    def handle_endtag(self, tag):
        if tag == "label" and self.wrapped:
            self.wrapped -= 1


def test_every_label_points_at_a_field_and_every_field_is_named(pages):
    for client, path in pages:
        p = Labels()
        p.feed(client.get(path).text)
        assert len(p.ids) == len(set(p.ids)), (path, "duplicate ids")
        assert set(p.fors) <= set(p.ids), (path, set(p.fors) - set(p.ids))
        for name, ident, aria, wrapped in p.fields:
            assert aria or wrapped or ident in p.fors, (path, name)
