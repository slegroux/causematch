"""Trust and safety: report, block, suspension, org verification, account deletion, privacy."""

import json
import re
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
from conftest import (
    FIXTURES,
    FORM,
    PANTRY,
    ROOT,
    coordinator_for,
    last_link_token,
    org_sign_in,
    pantry_opening,
    sign_in,
    uid_of,
    volunteer,
)
from fasthtml.common import to_xml
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import data, notify, oauth, propublica
from app.choices import SHARED_WITH_NONPROFIT
from app.db import (
    blocks,
    identities,
    matches,
    messages,
    notifications,
    openings,
    orgs,
    profiles,
    reports,
    shifts,
    swipes,
)
from app.routes.matches import MatchRow
from main import app

FIXTURE = json.loads((FIXTURES / "organization_861620575.json").read_text())


@pytest.fixture
def w():
    w = SimpleNamespace()
    w.vol = volunteer("Sam Quinn")
    w.other = volunteer("Bea Quinn")
    w.admin = volunteer("Root Quinn")
    profiles.update(id=w.admin.uid, role="admin")
    w.coord_uid, email = coordinator_for(0)
    w.coord = TestClient(app)
    org_sign_in(w.coord, email)
    w.org = orgs()[0].id
    w.opening = pantry_opening()
    return w


def like(w, vol=None):
    return data.swipe(vol.uid if vol else w.vol.uid, w.opening.id, True)


def accept(w, m):
    data.decide(w.coord_uid, m.id, True)


def deck_ids(vol):
    return [c["id"] for c in data.deck(vol.uid, 50)]


# --- report ------------------------------------------------------------------------------------


def test_volunteer_reports_the_org_from_a_match(w):
    m = like(w)
    page = w.vol.get(f"/matches/{m.id}/report")
    assert page.status_code == 200 and "Harassment" in page.text
    r = w.vol.post(
        f"/matches/{m.id}/report",
        data={"reason": "spam", "details": "  too many texts  "},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/matches?notice=reported"
    (rep,) = reports()
    assert (rep.reporter_id, rep.target_org_id, rep.target_user_id) == (
        w.vol.uid,
        w.org,
        None,
    )
    assert (rep.match_id, rep.reason, rep.details, rep.status) == (
        m.id,
        "spam",
        "too many texts",
        "open",
    )
    assert not blocks.count  # a report alone blocks nothing
    assert "Thanks" in w.vol.get("/matches?notice=reported").text


def test_coordinator_reports_the_volunteer_from_the_inbox(w):
    m = like(w)
    assert f"/org/matches/{m.id}/report" in w.coord.get("/org/inbox").text
    r = w.coord.post(
        f"/org/matches/{m.id}/report",
        data={"reason": "harassment", "details": "rude"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    (rep,) = reports()
    assert (rep.reporter_id, rep.target_user_id, rep.target_org_id) == (
        w.coord_uid,
        w.vol.uid,
        None,
    )


def test_chat_pages_link_to_report_and_block(w):
    m = like(w)
    accept(w, m)
    assert f"/matches/{m.id}/report" in w.vol.get(f"/chat/{m.id}").text
    assert f"/org/matches/{m.id}/block" in w.coord.get(f"/org/chat/{m.id}").text


@pytest.mark.parametrize(
    "form",
    [{"reason": ""}, {"reason": "nonsense"}, {"reason": "spam", "details": "x" * 1001}],
)
def test_a_bad_report_is_refused_and_keeps_the_form(w, form):
    m = like(w)
    r = w.vol.post(f"/matches/{m.id}/report", data=form, follow_redirects=False)
    assert r.status_code == 200 and "Choose a reason" in r.text
    assert not reports.count
    ok = w.vol.post(
        f"/matches/{m.id}/report",
        data={"reason": "other", "details": "x" * 1000},
        follow_redirects=False,
    )
    assert ok.status_code == 303 and reports.count == 1


def test_a_repeat_report_on_the_same_match_adds_nothing(w):
    m = like(w)
    for _ in range(3):
        w.vol.post(f"/matches/{m.id}/report", data={"reason": "spam"})
    assert reports.count == 1


def test_report_with_also_block_blocks_too(w):
    m = like(w)
    w.vol.post(f"/matches/{m.id}/report", data={"reason": "unsafe", "also_block": "1"})
    assert (
        reports.count == 1 and blocks.count == 1 and matches[m.id].status == "withdrawn"
    )


# --- block ---------------------------------------------------------------------------------------


def test_blocked_org_leaves_deck(w):
    org_openings = {o.id for o in openings(where="org_id = ?", where_args=[w.org])}
    assert len(org_openings & set(deck_ids(w.vol))) >= 1
    m = like(w)
    assert w.vol.get(f"/matches/{m.id}/block").status_code == 200
    assert not blocks.count  # the confirm page blocks nothing
    r = w.vol.post(f"/matches/{m.id}/block", follow_redirects=False)
    assert r.status_code == 303
    assert not org_openings & set(deck_ids(w.vol))
    assert deck_ids(w.vol)  # other orgs' openings are still there
    assert "Blocked" in w.vol.get("/matches").text
    with pytest.raises(LookupError):  # and a like sent by id is refused
        data.swipe(w.vol.uid, next(iter(org_openings)), True)


def test_volunteer_block_withdraws_matches_cancels_shifts_and_freezes_chat(w):
    m = like(w)
    accept(w, m)
    data.send_message(w.vol.uid, m.id, "hello")
    assert shifts(where="match_id = ?", where_args=[m.id])[0].status == "scheduled"
    assert w.vol.get(f"/matches/{m.id}/block").status_code == 200
    assert matches[m.id].status == "accepted"  # the confirm page changes nothing
    w.vol.post(f"/matches/{m.id}/block")
    assert matches[m.id].status == "withdrawn"
    assert shifts(where="match_id = ?", where_args=[m.id])[0].status == "cancelled"
    assert w.vol.get(f"/chat/{m.id}").status_code == 200  # history stays readable
    for uid in (w.vol.uid, w.coord_uid):
        with pytest.raises(PermissionError):
            data.send_message(uid, m.id, "more")
    assert messages.count == 1
    inbox = w.coord.get("/org/inbox").text
    assert "Past matches" in inbox and "Withdrawn" in inbox
    assert f"/org/matches/{m.id}/report" in inbox  # the coordinator can still report
    assert data.org_inbox(w.coord_uid)["pending"] == []


def test_volunteer_block_withdraws_a_pending_match(w):
    m = like(w)
    data.block_org(w.vol.uid, m.id)
    assert matches[m.id].status == "withdrawn"
    assert "Sam" not in w.coord.get("/org/inbox").text


def test_coordinator_block_declines_pending_and_closes_accepted(w):
    pending = like(w)
    # a second like by the same volunteer on another opening of the org, accepted
    second = openings.insert(
        openings.dataclass()(
            **{**openings[w.opening.id].__dict__, "id": None, "title": "Second"}
        )
    )
    m2 = data.swipe(w.vol.uid, second.id, True)
    accept(w, pending)
    data.send_message(w.vol.uid, pending.id, "hi")
    r = w.coord.post(f"/org/matches/{m2.id}/block", follow_redirects=False)
    assert r.status_code == 303
    assert (
        matches[m2.id].status == "declined"
        and matches[pending.id].status == "withdrawn"
    )
    assert (
        shifts(where="match_id = ?", where_args=[pending.id])[0].status == "cancelled"
    )
    for uid in (w.vol.uid, w.coord_uid):
        with pytest.raises(PermissionError):
            data.send_message(uid, pending.id, "more")
    inbox = data.org_inbox(w.coord_uid)
    assert (
        inbox["pending"] == [] and inbox["decided"] == []
    )  # the declined one is hidden
    assert [r["id"] for r in inbox["past"]] == [
        pending.id
    ]  # the accepted one stays reportable
    assert "Sam" not in w.coord.get("/org/shifts").text


def test_a_blocked_volunteers_like_gets_the_same_notice_and_survey(w):
    first = like(w)
    data.block_volunteer(w.coord_uid, first.id)
    third, fourth = (
        openings.insert(
            openings.dataclass()(
                **{**openings[w.opening.id].__dict__, "id": None, "title": t}
            )
        )
        for t in ("Third", "Fourth")
    )
    org = data.org_name_of(third.id)
    seen = []
    for who in (w.vol, w.other):
        profiles.update(id=who.uid, heard_from="")  # the first like asks the survey
        r = who.post("/swipe", data={"opening_id": third.id, "liked": "1"},
                     follow_redirects=False)  # fmt: skip
        survey = who.get(r.headers["location"]).text
        r2 = who.post("/swipe", data={"opening_id": fourth.id, "liked": "1"},
                      headers={"HX-Request": "true"})  # fmt: skip
        notice = re.search(r'<p role="status"[^>]*>([^<]*)', r2.text)[1]
        seen.append((r.headers["location"], f"Sent to {org}." in survey, notice))
    assert seen[0] == seen[1] and seen[0][1], seen
    assert matches(where="volunteer_id = ? AND opening_id = ?",
                   where_args=[w.vol.uid, third.id])[0].org_blind  # fmt: skip


def test_a_blocked_volunteers_like_looks_normal_to_them_and_never_reaches_the_org(w):
    first = like(w)
    data.block_volunteer(w.coord_uid, first.id)
    third = openings.insert(
        openings.dataclass()(
            **{**openings[w.opening.id].__dict__, "id": None, "title": "Third"}
        )
    )
    control = w.other
    notify.get_emailer().sent.clear()
    assert third.id in deck_ids(w.vol)  # the deck does not reveal the block
    for who in (w.vol, control):
        r = who.post(
            "/swipe",
            data={"opening_id": str(third.id), "liked": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 303  # the same answer as any like
    mine = data.my_matches(w.vol.uid)[0]
    theirs = data.my_matches(control.uid)[0]
    assert mine["status"] == theirs["status"] == "pending"
    strip = lambda row: to_xml(MatchRow({**row, "id": 0}))
    assert strip(mine) == strip(theirs)  # indistinguishable to the volunteer
    assert "Waiting for the nonprofit" in w.vol.get("/matches").text
    blind = matches(
        where="volunteer_id = ? AND opening_id = ?", where_args=[w.vol.uid, third.id]
    )
    assert len(blind) == 1 and blind[0].org_blind
    # the org never hears of it: no inbox row, one email only for the control
    inbox = data.org_inbox(w.coord_uid)
    assert [r["id"] for r in inbox["pending"]] == [
        matches(where="volunteer_id = ?", where_args=[control.uid])[0].id
    ]
    assert (
        len(notify.get_emailer().sent) == 1
        and "1 volunteer is" in notify.get_emailer().sent[0][2]
    )
    assert "Sam" not in w.coord.get("/org/inbox").text
    with pytest.raises(PermissionError):  # nor can it be decided by id
        data.decide(w.coord_uid, blind[0].id, True)
    # and it counts toward no rate
    rows = data.metrics_rows()
    assert blind[0].id not in {m["id"] for m in rows["matches"]}
    n_swipes = swipes.count
    assert len(rows["swipes"]) == n_swipes - 1
    assert not any(
        x["volunteer_id"] == w.vol.uid and x["id"] == blind[0].id
        for x in rows["matches"]
    )


def test_blocks_are_per_org(w):
    m = like(w)
    data.block_volunteer(w.coord_uid, m.id)
    other_org = openings(where="org_id = ?", where_args=[orgs()[1].id])[0]
    assert data.swipe(w.vol.uid, other_org.id, True) is not None


def test_a_block_sends_no_texts(w):
    m = like(w)
    accept(w, m)
    data.block_org(w.vol.uid, m.id)
    sms = notify.get_sms()
    sms.sent.clear()
    notify.on_new_message(m.id, w.coord_uid)
    notify.on_accepted(m.id)
    assert sms.sent == []


def test_only_an_admin_unblocks(w):
    m = like(w)
    data.block_org(w.vol.uid, m.id)
    bid = blocks()[0].id
    with pytest.raises(PermissionError):
        data.unblock(w.coord_uid, bid)
    assert blocks.count == 1
    assert w.admin.get("/admin/blocks").status_code == 200
    assert (
        w.vol.post(f"/admin/blocks/{bid}/unblock", follow_redirects=False).status_code
        == 403
    )
    w.admin.post(f"/admin/blocks/{bid}/unblock")
    assert blocks.count == 0
    assert w.opening.id not in deck_ids(w.vol)  # it was liked before; others return
    assert any(i for i in deck_ids(w.vol))


# --- the admin queue -----------------------------------------------------------------------------


def test_the_queue_is_admin_only_and_shows_first_names_only(w):
    m = like(w)
    data.report_org(w.vol.uid, m.id, "spam", "too many texts")
    data.report_volunteer(w.coord_uid, m.id, "no_show_pattern", "never came")
    page = w.admin.get("/admin/reports")
    assert page.status_code == 200
    for text in (
        "Sam",
        PANTRY,
        "too many texts",
        "never came",
        "A pattern of no-shows",
    ):
        assert text in page.text
    for secret in (w.vol.phone, "Quinn", "Pat Doe", "coord0@example.org"):
        assert secret not in page.text
    assert w.vol.get("/admin/reports", follow_redirects=False).status_code == 403
    assert w.coord.get("/admin/reports", follow_redirects=False).status_code == 303
    assert (
        TestClient(app).get("/admin/reports", follow_redirects=False).status_code == 303
    )


def test_dismiss_records_who_and_when(w):
    m = like(w)
    data.report_org(w.vol.uid, m.id, "spam")
    rid = reports()[0].id
    w.admin.post(f"/admin/reports/{rid}/dismiss")
    r = reports[rid]
    assert (r.status, r.resolved_by) == ("dismissed", w.admin.uid) and r.resolved_at
    assert "No open reports" in w.admin.get("/admin/reports").text
    w.admin.post(f"/admin/reports/{rid}/dismiss")  # a repeat is a no-op
    assert reports[rid].resolved_by == w.admin.uid


def test_suspend_blocks_sign_in_ends_sessions_and_kills_sockets(w):
    m = like(w)
    accept(w, m)
    data.report_volunteer(w.coord_uid, m.id, "harassment")
    rid = reports()[0].id
    before = profiles[w.vol.uid].session_version
    with w.vol.websocket_connect(f"/chat/{m.id}/ws") as ws:
        w.admin.post(f"/admin/reports/{rid}/suspend")
        ws.send_text('{"msg": "still here"}')
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert messages.count == 0
    p = profiles[w.vol.uid]
    assert p.suspended_at and p.session_version == before + 1
    r = reports[rid]
    assert (r.status, r.resolved_by) == ("actioned", w.admin.uid) and r.resolved_at
    assert w.vol.get("/deck", follow_redirects=False).headers["location"] == "/login"
    # signing in again with the code is refused
    again = TestClient(app)
    again.post("/login/send", data={"phone": w.vol.phone})
    r = again.post("/login/verify", data={"code": "000000"}, follow_redirects=False)
    assert r.status_code == 200 and "can't sign in" in r.text
    assert matches[m.id].status == "withdrawn"
    assert "Sam" not in w.coord.get("/org/inbox").text


def test_suspending_a_coordinator_ends_their_session(w):
    m = like(w)
    # a volunteer cannot report a coordinator directly, so file the report row by hand
    reports.insert(
        data.__dict__["_db"].Report(
            reporter_id=w.vol.uid,
            target_user_id=w.coord_uid,
            match_id=m.id,
            reason="harassment",
            details="",
            status="open",
            created_at="2026-10-15T00:00:00+00:00",
        )
    )
    rid = reports()[0].id
    w.admin.post(f"/admin/reports/{rid}/suspend")
    assert (
        w.coord.get("/org", follow_redirects=False).headers["location"] == "/org/login"
    )
    fresh = TestClient(app)
    fresh.post("/org/login/send", data={"email": "coord0@example.org"})
    r = fresh.post(
        "/org/login/verify", data={"token": last_link_token()}, follow_redirects=False
    )
    assert r.status_code == 200  # the form again, not a redirect home


def test_an_admin_cannot_be_suspended(w):
    m = like(w)
    reports.insert(
        data.__dict__["_db"].Report(
            reporter_id=w.vol.uid,
            target_user_id=w.admin.uid,
            match_id=m.id,
            reason="spam",
            details="",
            status="open",
            created_at="2026-10-15T00:00:00+00:00",
        )
    )
    w.admin.post(f"/admin/reports/{reports()[0].id}/suspend")
    assert not profiles[w.admin.uid].suspended_at


def test_deactivating_an_org_closes_its_openings_and_hides_it(w):
    m = like(w)
    accept(w, m)
    data.report_org(w.vol.uid, m.id, "unsafe")
    rid = reports()[0].id
    w.admin.post(f"/admin/reports/{rid}/deactivate")
    assert orgs[w.org].deactivated_at
    assert {o.status for o in openings(where="org_id = ?", where_args=[w.org])} == {
        "closed"
    }
    assert shifts(where="match_id = ?", where_args=[m.id])[0].status == "cancelled"
    assert not {o.id for o in openings(where="org_id = ?", where_args=[w.org])} & set(
        deck_ids(w.other)
    )
    r = reports[rid]
    assert (r.status, r.resolved_by) == ("actioned", w.admin.uid) and r.resolved_at
    with pytest.raises(PermissionError):
        data.create_opening(
            w.coord_uid,
            w.org,
            title="t",
            about="a",
            cause="food-insecurity",
            lat=1.0,
            lon=1.0,
            neighborhood="n",
            starts_at="2030-01-01T10:00",
            recurrence="one_time",
            slots=1,
            orientation_required="no",
            notes="",
            reply_channel="email",
        )


def test_resolving_needs_the_right_kind_of_report(w):
    m = like(w)
    data.report_org(w.vol.uid, m.id, "spam")
    rid = reports()[0].id
    w.admin.post(f"/admin/reports/{rid}/suspend")  # names an org, not a user
    assert reports[rid].status == "open"


# --- org verification ----------------------------------------------------------------------------


def client_for(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=10)


def serve_fixture(request):
    assert str(request.url) == propublica.URL.format(ein="861620575")
    return httpx.Response(200, json=FIXTURE)


def test_lookup_reads_the_recorded_response():
    c = httpx.Client(transport=httpx.MockTransport(serve_fixture))
    assert propublica.lookup("86-1620575", c) == {
        "ein": "861620575",
        "name": "Los Angeles Food Bank",
        "city": "Los Angeles",
        "state": "CA",
        "zip": "90019",
        "ntee": "K31",
    }


@pytest.mark.parametrize("raw", ["861620575", "86-1620575", " 86 1620575 "])
def test_eins_normalize_to_nine_digits(raw):
    assert propublica.normalize_ein(raw) == "861620575"


@pytest.mark.parametrize("raw", ["", "5301966", "86162057512", "53-019660x", None])
def test_a_malformed_ein_never_reaches_the_network(raw):
    def boom(request):
        raise AssertionError("no request expected")

    with pytest.raises(ValueError):
        propublica.lookup(raw, httpx.Client(transport=httpx.MockTransport(boom)))


def test_an_unknown_ein_is_none():
    c = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    )
    assert propublica.lookup("123456789", c) is None


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
        lambda r: httpx.Response(500),
        lambda r: httpx.Response(200, text="<html>"),
        lambda r: httpx.Response(200, json={"nope": 1}),
        lambda r: httpx.Response(200, json={"organization": {"name": ""}}),
        lambda r: httpx.Response(200, json=[1]),
    ],
)
def test_a_timeout_or_a_malformed_answer_is_a_lookup_failure(handler):
    c = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(propublica.LookupFailed):
        propublica.lookup("861620575", c)


def test_admin_looks_up_then_approves(w, monkeypatch):
    monkeypatch.setattr(propublica, "make_client", client_for(serve_fixture))
    orgs.update(id=w.org, verified_at=None)
    assert "Not verified" in w.admin.get("/admin/orgs").text
    page = w.admin.post(f"/admin/orgs/{w.org}/lookup", data={"ein": "86-1620575"})
    assert "Los Angeles Food Bank, Los Angeles, CA" in page.text
    assert not orgs[w.org].verified_at  # looking up approves nothing
    r = w.admin.post(
        f"/admin/orgs/{w.org}/verify",
        data={"ein": "86-1620575"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (orgs[w.org].ein, bool(orgs[w.org].verified_at)) == ("861620575", True)


def test_the_admin_page_words_a_404_a_timeout_and_a_bad_ein(w, monkeypatch):
    cases = [
        ("123456789", lambda r: httpx.Response(404), "no nonprofit with that EIN"),
        (
            "123456789",
            lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("t")),
            "did not answer",
        ),
        ("12", None, "nine digits"),
    ]
    for ein, handler, text in cases:
        if handler:
            monkeypatch.setattr(propublica, "make_client", client_for(handler))
        r = w.admin.post(f"/admin/orgs/{w.org}/lookup", data={"ein": ein})
        assert r.status_code == 200 and text in r.text


def test_an_ein_belongs_to_one_org(w):
    data.verify_org(orgs()[0].id, "861620575")
    with pytest.raises(ValueError):
        data.verify_org(orgs()[1].id, "861620575")
    with pytest.raises(ValueError):
        data.verify_org(orgs()[1].id, "5301966")
    with pytest.raises(LookupError):
        data.verify_org(10**6, "111111111")


def test_openings_from_unverified_orgs_are_hidden(w):
    org_openings = {o.id for o in openings(where="org_id = ?", where_args=[w.org])}
    assert org_openings & set(deck_ids(w.vol))
    orgs.update(id=w.org, verified_at=None)
    assert not org_openings & set(deck_ids(w.vol))
    with pytest.raises(LookupError):
        data.swipe(w.vol.uid, w.opening.id, True)
    assert deck_ids(w.vol)  # verified orgs still show
    data.verify_org(w.org, "861620575")
    assert w.opening.id in deck_ids(w.vol)


def test_demo_orgs_are_verified_only_in_development():
    assert all(o.verified_at for o in orgs())  # CAUSEMATCH_ENV=test
    src = (ROOT / "app" / "db.py").read_text()
    assert "verified_at=now() if is_dev() else None" in src


def test_verify_org_script_refuses_a_bad_ein_without_the_network(tmp_path):
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_org.py"), "--ein", "12"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": "",
            "CAUSEMATCH_ENV": "test",
            "CAUSEMATCH_DB": str(tmp_path / "t.db"),
        },
    )
    assert r.returncode != 0 and "nine digits" in r.stderr


# --- account deletion ----------------------------------------------------------------------------


PII = (
    "phone",
    "email",
    "display_name",
    "contact_phone",
    "lat",
    "lon",
    "neighborhood",
    "other_causes",
    "language_other",
)


def test_delete_account_scrubs_pii(w):
    uid = w.vol.uid
    profiles.update(
        id=uid,
        email="sam@example.com",
        other_causes="rescue owls",
        language_other="Tagalog",
        neighborhood="Chinatown",
        lat=34.06,
        lon=-118.24,
        contact_phone="+13105550199",
        heard_from="public",
        causes='["food-insecurity"]',
        frequency="weekly",
    )
    m = like(w)
    accept(w, m)
    data.send_message(uid, m.id, "my address is 12 Elm St")
    data.send_message(w.coord_uid, m.id, "see you there")
    phone = w.vol.phone
    notifications.insert(
        data.__dict__["_db"].Notification(
            kind="accepted",
            key="accepted:x",
            recipient_id=uid,
            sent_at="2026-10-15T00:00:00+00:00",
            status="failed",
            attempts=1,
        )
    )
    before_version = profiles[uid].session_version

    assert (
        "Delete my account" not in w.vol.get("/profile").text
    )  # it lives in the profile
    page = w.vol.get("/me/profile")
    assert 'href="/profile/delete"' in page.text
    assert profiles[uid].deleted_at is None  # neither page deletes
    assert w.vol.get("/profile/delete").status_code == 200
    assert profiles[uid].deleted_at is None
    r = w.vol.post("/profile/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"

    p = profiles[uid]
    assert p.deleted_at and p.session_version == before_version + 1
    for col in PII:
        assert getattr(p, col) is None, col
    # kept: what the metrics group by
    assert (p.heard_from, p.frequency, json.loads(p.causes)) == (
        "public",
        "weekly",
        ["food-insecurity"],
    )
    assert matches[m.id].status == "withdrawn"
    assert shifts(where="match_id = ?", where_args=[m.id])[0].status == "cancelled"
    bodies = {x.sender_id: x.body for x in messages()}
    assert bodies[uid] == "[message removed]" and bodies[w.coord_uid] == "see you there"
    assert notifications(where="key = 'accepted:x'")[0].status == "cancelled"

    # no page or metrics row shows them
    seen = (
        "".join(
            w.coord.get(path).text
            for path in ("/org", "/org/inbox", "/org/shifts", f"/org/chat/{m.id}")
        )
        + w.admin.get("/admin/metrics").text
        + w.admin.get("/admin/metrics.csv").text
    )
    seen += json.dumps(data.metrics_rows())
    for secret in (
        phone,
        "sam@example.com",
        "Sam",
        "Quinn",
        "rescue owls",
        "Tagalog",
        "Chinatown",
        "12 Elm St",
        "34.06",
        "-118.24",
    ):
        assert secret not in seen, secret
    assert data.metrics_rows()["matches"][0]["volunteer_id"] == uid  # still counted

    # the session is dead, the number is free for a new profile
    assert w.vol.get("/deck", follow_redirects=False).headers["location"] == "/login"
    fresh = TestClient(app)
    sign_in(fresh, phone)
    new_uid = data.get_or_create_volunteer(phone)
    assert new_uid != uid and profiles[new_uid].deleted_at is None
    assert (
        fresh.get("/deck", follow_redirects=False).headers["location"] == "/onboarding"
    )


def test_delete_account_through_onboarding_form_data(new_user):
    new_user.post("/onboarding", data=FORM)
    uid = uid_of(new_user)
    # the email moved from onboarding to the profile (UX item 3)
    data.save_profile_details(uid, first_name="Sam", last_name="Quinn", pronouns="",
                              bio="", interests=[], email="a@b.co")  # fmt: skip
    assert profiles[uid].email == "a@b.co" and profiles[uid].onboarded_at
    data.delete_account(uid)
    p = profiles[uid]
    assert all(getattr(p, c) is None for c in PII)
    data.delete_account(uid)  # a repeat changes nothing


def test_only_a_volunteer_can_delete_through_delete_account(w):
    for uid in (w.coord_uid, w.admin.uid, 10**6, 0):
        with pytest.raises(PermissionError):
            data.delete_account(uid)
    assert not profiles[w.coord_uid].deleted_at


def test_remove_coordinator_scrubs_and_signs_out(w):
    data.remove_coordinator("coord0@example.org")
    p = profiles[w.coord_uid]
    assert p.deleted_at and p.email is None and p.display_name is None
    assert not data.coordinator_org_ids(w.coord_uid)
    assert (
        w.coord.get("/org", follow_redirects=False).headers["location"] == "/org/login"
    )
    with pytest.raises(LookupError):
        data.remove_coordinator("coord0@example.org")


# --- privacy -------------------------------------------------------------------------------------


def test_privacy_is_public_and_says_what_the_consent_says(anon):
    r = anon.get("/privacy", follow_redirects=False)
    assert r.status_code == 200
    assert SHARED_WITH_NONPROFIT in r.text.replace("&#x27;", "'")
    assert "hello@causematch.test" in r.text  # CONTACT_EMAIL, defaulted in test
    for word in ("STOP", "keep", "delete"):
        assert word.lower() in r.text.lower()


def test_privacy_is_linked_from_consent_login_pages_and_footer(anon, new_user):
    assert 'href="/privacy"' in anon.get("/login").text
    assert 'href="/privacy"' in anon.get("/org/login").text
    assert 'href="/privacy"' in new_user.get("/onboarding").text
    assert "I agree to the" in new_user.get("/onboarding").text  # the consent's link


def test_contact_email_is_required_in_production():
    from app.config import contact_email

    assert contact_email({"CONTACT_EMAIL": "a@b.org"}) == "a@b.org"
    with pytest.raises(RuntimeError):
        contact_email({"CAUSEMATCH_ENV": "production"})


# --- deactivating an org ------------------------------------------------------------------


def sms_log():
    return list(notify.get_sms().sent)


def test_deactivating_an_org_ends_every_live_match_and_silences_it(w):
    accepted = like(w)
    accept(w, accepted)
    pending = like(w, w.other)
    data.send_message(w.vol.uid, accepted.id, "hello")
    data.report_org(w.vol.uid, accepted.id, "unsafe")
    rid = reports()[0].id
    sms_before = sms_log()
    w.admin.post(f"/admin/reports/{rid}/deactivate")
    assert matches[pending.id].status == "declined"
    assert matches[accepted.id].status == "withdrawn"
    assert (
        shifts(where="match_id = ?", where_args=[accepted.id])[0].status == "cancelled"
    )
    # chat is read-only for both sides, by page, post and data call
    assert w.vol.get(f"/chat/{accepted.id}").status_code == 200
    r = w.coord.post(
        f"/org/chat/{accepted.id}/send", data={"msg": "hi"}, follow_redirects=False
    )
    assert r.status_code == 403
    for uid in (w.vol.uid, w.coord_uid):
        with pytest.raises(PermissionError):
            data.send_message(uid, accepted.id, "more")
    assert messages.count == 1
    # even a match that is somehow still live is refused by every coordinator action
    matches.update(id=pending.id, status="pending", decided_at=None)
    with pytest.raises(PermissionError):
        data.decide(w.coord_uid, pending.id, True)
    r = w.coord.post(
        f"/org/matches/{pending.id}/decide",
        data={"decision": "accept"},
        follow_redirects=False,
    )
    assert r.status_code == 403 and matches[pending.id].status == "pending"
    matches.update(id=accepted.id, status="accepted")
    future = "2030-01-01T10:00"
    with pytest.raises(PermissionError):
        data.add_shift(w.coord_uid, accepted.id, future)
    with pytest.raises(PermissionError):
        data.repeat_shift(w.coord_uid, accepted.id)
    with pytest.raises(PermissionError):
        data.send_message(w.coord_uid, accepted.id, "still here?")
    # nothing was texted or emailed to anyone
    notify.on_accepted(accepted.id)
    notify.on_new_message(accepted.id, w.coord_uid)
    notify.on_new_like(pending.id)
    assert sms_log() == sms_before and not shifts(
        where="starts_at = ?", where_args=[future]
    )


def test_a_deactivated_orgs_reminders_stop(w):
    m = like(w)
    accept(w, m)
    sid = shifts(where="match_id = ?", where_args=[m.id])[0].id
    data.report_org(w.vol.uid, m.id, "unsafe")
    w.admin.post(f"/admin/reports/{reports()[0].id}/deactivate")
    start = shifts[sid].starts_at
    assert data.due_reminders(start, start) == []


def test_the_deactivated_filter_alone_hides_a_reopened_opening(w):
    m = like(w)
    data.report_org(w.vol.uid, m.id, "unsafe")
    w.admin.post(f"/admin/reports/{reports()[0].id}/deactivate")
    openings.update(id=w.opening.id, status="open")  # reopened by hand: still hidden
    assert w.opening.id not in deck_ids(w.other)


# a number keeps its blocks and suspension through delete and sign-up


def retained():
    return list(data.__dict__["_db"].retained_phones())


def test_delete_and_sign_up_again_keeps_a_block(w):
    m = like(w)
    data.block_volunteer(w.coord_uid, m.id)
    phone = w.vol.phone
    data.delete_account(w.vol.uid)
    (row,) = retained()
    assert phone not in json.dumps(row.__dict__) and "block" in row.reason
    fresh = TestClient(app)
    sign_in(fresh, phone)
    new_uid = data.get_or_create_volunteer(phone)
    assert new_uid != w.vol.uid and not profiles[new_uid].suspended_at
    assert blocks(where="blocked_user_id = ?", where_args=[new_uid])
    profiles.update(
        id=new_uid, display_name="Sam New", onboarded_at="2026-01-01T00:00:00"
    )
    liked = data.swipe(new_uid, w.opening.id, True)
    assert matches[liked.id].org_blind  # the block followed the number
    assert "Sam New" not in w.coord.get("/org/inbox").text


def test_a_suspended_account_cannot_come_back_by_deleting_itself(w):
    m = like(w)
    accept(w, m)
    data.report_volunteer(w.coord_uid, m.id, "harassment")
    rid = reports()[0].id
    phone = w.vol.phone
    data.delete_account(w.vol.uid)  # deleted while the report is open
    assert len(retained()) == 1
    w.admin.post(f"/admin/reports/{rid}/suspend")  # suspended after the fact
    assert retained()[0].suspended
    again = TestClient(app)
    again.post("/login/send", data={"phone": phone})
    r = again.post("/login/verify", data={"code": "000000"}, follow_redirects=False)
    assert r.status_code == 200 and "can't sign in" in r.text


def test_a_suspended_google_account_cannot_come_back_by_deleting_itself(w):
    claims = oauth.Claims("google", "g-gone", "gone@example.com", True, "Gale", "", "")
    uid = data.volunteer_for_identity(claims)
    profiles.update(id=uid, display_name="Gale", onboarded_at="2026-01-01T00:00:00")
    m = data.swipe(uid, w.opening.id, True)
    accept(w, m)
    data.report_volunteer(w.coord_uid, m.id, "harassment")
    data.delete_account(uid)  # deleted while the report is open
    assert identities.count_where("user_id = ?", [uid]) == 0
    w.admin.post(
        f"/admin/reports/{reports()[0].id}/suspend"
    )  # suspended after the fact
    (row,) = data.__dict__["_db"].retained_identities()
    assert row.suspended and "g-gone" not in json.dumps(row.__dict__)
    again = TestClient(app)
    r = again.get("/auth/google/start", follow_redirects=False)
    flow = dict(
        re.findall(r"(state|nonce|code_challenge)=([^&]+)", r.headers["location"])
    )
    form = {**flow, "sub": "g-gone", "email": "gone@example.com", "email_verified": "1"}
    r = again.post("/auth/fake/consent", data=form, follow_redirects=False)
    r = again.get(r.headers["location"], follow_redirects=False)
    assert r.status_code == 200 and "can't sign in" in r.text
    (i,) = identities(where="subject = ?", where_args=["g-gone"])  # the new sign-up's
    assert i.user_id != uid and profiles[i.user_id].suspended_at


def test_an_account_with_no_blocks_or_reports_leaves_no_hash(w):
    like(w)
    data.delete_account(w.vol.uid)
    assert retained() == []
    assert profiles[w.vol.uid].phone is None


def test_a_dismissed_report_or_lifted_block_releases_the_hash(w):
    m = like(w)
    data.report_org(w.other.uid, like(w, w.other).id, "spam")  # unrelated report
    data.report_volunteer(w.coord_uid, m.id, "spam")
    data.block_volunteer(w.coord_uid, m.id)
    data.delete_account(w.vol.uid)
    assert len(retained()) == 1
    rid = next(r.id for r in reports() if r.target_user_id)
    data.dismiss_report(w.admin.uid, rid)
    assert len(retained()) == 1  # the block still needs it
    data.unblock(w.admin.uid, blocks()[0].id)
    assert retained() == []


def test_phone_hash_key_is_required_in_production():
    from app.config import phone_hash_key

    assert phone_hash_key({"PHONE_HASH_KEY": "k"}) == "k"
    with pytest.raises(RuntimeError):
        phone_hash_key({"CAUSEMATCH_ENV": "production"})


def test_env_example_lists_contact_email_and_phone_hash_key():
    text = (ROOT / ".env.example").read_text()
    for name in ("CONTACT_EMAIL=", "PHONE_HASH_KEY="):
        assert name in text
        assert name.rstrip("=") in (ROOT / "docs" / "OPERATIONS.md").read_text()


# no data call on the event loop


def test_no_async_route_calls_data_directly():
    import ast

    offenders = []
    for f in sorted((ROOT / "app" / "routes").glob("*.py")):
        for fn in ast.walk(ast.parse(f.read_text())):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            if "data" in {a.arg for a in fn.args.args}:
                continue  # chat's ws handler: `data` is the frame there
            for call in ast.walk(fn):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id in ("data", "notify")
                ):
                    offenders.append(f"{f.name}:{fn.name}:{call.func.attr}")
    assert offenders == []


# the privacy page


def test_privacy_page_is_complete_and_honest(anon):
    text = anon.get("/privacy").text.replace("&#x27;", "'").replace("&quot;", '"')
    for needed in (
        "Draft, pending legal review.",
        "Effective ",
        "Twilio",
        "Resend",
        "Google Cloud",
        "Do Not Track",
        "no-show",
        "[message removed]",
        "one-way keyed hash",
        "other causes",
        "hello@causematch.test",
        SHARED_WITH_NONPROFIT,
    ):
        assert needed in text, needed
    assert "only if it is part of a report" not in text


# ProPublica


def test_a_made_up_ein_that_answers_200_with_nulls_is_unknown():
    nulls = {"organization": {"id": None, "ein": None, "name": None, "city": None}}
    c = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=nulls))
    )
    assert propublica.lookup("123456789", c) is None
    c = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"organization": None})
        )
    )
    assert propublica.lookup("123456789", c) is None


def test_the_recorded_404_body_is_unknown():
    body = json.loads((FIXTURES / "organization_not_found.json").read_text())
    c = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json=body))
    )
    assert propublica.lookup("123456789", c) is None
    c = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    )
    assert propublica.lookup("123456789", c) is None


# reports, blocks and admin checks


def test_a_coordinator_can_report_after_the_volunteer_withdraws(w):
    m = like(w)
    accept(w, m)
    w.vol.post(f"/matches/{m.id}/withdraw")
    assert f"/org/matches/{m.id}/report" in w.coord.get("/org/inbox").text
    r = w.coord.post(
        f"/org/matches/{m.id}/report",
        data={"reason": "harassment"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and reports.count == 1


def test_admin_functions_refuse_a_non_admin(w):
    m = like(w)
    data.report_org(w.vol.uid, m.id, "spam")
    rid = reports()[0].id
    org = orgs()[0].id
    calls = [
        lambda u: data.admin_reports(u),
        lambda u: data.admin_blocks(u),
        lambda u: data.admin_orgs(u),
        lambda u: data.dismiss_report(u, rid),
        lambda u: data.suspend_target(u, rid),
        lambda u: data.deactivate_target_org(u, rid),
        lambda u: data.unblock(u, 1),
        lambda u: data.verify_org_as(u, org, "530196605"),
    ]
    for call in calls:
        for uid in (w.coord_uid, w.vol.uid):
            with pytest.raises(PermissionError):
                call(uid)
    assert reports[rid].status == "open" and not orgs[org].ein


def test_a_reporter_is_capped_at_ten_reports_a_day(w):
    others = [volunteer(f"V{i} Quinn") for i in range(data.REPORTS_PER_DAY)]
    ms = [like(w, v) for v in others]
    for m in ms[: data.REPORTS_PER_DAY - 1]:
        data.report_volunteer(w.coord_uid, m.id, "spam")
    data.report_volunteer(w.coord_uid, ms[-1].id, "spam")
    extra = like(w)
    with pytest.raises(ValueError, match="reports a day"):
        data.report_volunteer(w.coord_uid, extra.id, "spam")
    assert reports.count == data.REPORTS_PER_DAY
    r = w.coord.post(f"/org/matches/{extra.id}/report", data={"reason": "spam"})
    assert r.status_code == 200 and "reports a day" in r.text


def test_two_coordinators_blocking_one_volunteer_make_one_block(w):
    second, _ = coordinator_for(0, "second@example.org")
    m = like(w)
    data.block_volunteer(w.coord_uid, m.id)
    data.block_volunteer(second, m.id)
    assert blocks.count == 1


def test_a_suspended_number_is_sent_no_code(w):
    m = like(w)
    data.report_volunteer(w.coord_uid, m.id, "spam")
    w.admin.post(f"/admin/reports/{reports()[0].id}/suspend")
    notify.get_verifier().sent.clear()
    fresh = TestClient(app)
    r = fresh.post("/login/send", data={"phone": w.vol.phone})
    assert r.status_code == 200 and notify.get_verifier().sent == []


def test_report_details_with_a_bidi_control_are_refused():
    """A right-to-left override can make a report read as something else (review 4)."""
    with pytest.raises(ValueError, match="can't show"):
        data._checked_report_text("spam", "he said \u202eyas ton")
    assert data._checked_report_text("spam", " plain text ") == "plain text"


# --- names after a block, and on pages the inbox wouldn't list ----------------------------


def test_chat_messages_name_a_blocked_volunteer_volunteer_only(w):
    """L1: the message rows, not only the header, stop reading the live first name."""
    m = like(w)
    accept(w, m)
    data.send_message(w.vol.uid, m.id, "See you Saturday")
    data.block_volunteer(w.coord_uid, m.id)
    profiles.update(id=w.vol.uid, display_name="CALLME5551234")  # a rename after it
    page = w.coord.get(f"/org/chat/{m.id}").text
    assert "CALLME" not in page and "Sam" not in page and data.BLOCKED_NAME in page
    rows = data.list_messages(w.coord_uid, m.id)
    assert [r["sender"] for r in rows] == [data.BLOCKED_NAME]
    assert "You" in w.vol.get(f"/chat/{m.id}").text  # their own view is unchanged


def _report_page_name(w, m):
    return w.coord.get(f"/org/matches/{m.id}/report").text


@pytest.mark.parametrize(
    "hide",
    [
        "org_blind",
        "withdrawn_undecided",
        "suspended",
        "deleted",
        "org_block",
        "vol_block",
    ],
)
def test_report_and_block_pages_name_no_one_the_inbox_would_hide(w, hide):
    """L2: org_match_volunteer follows org_inbox's rule; the report itself still goes."""
    m = like(w)
    assert "Report Sam" in _report_page_name(w, m)  # positive control: listed, named
    if hide == "org_blind":
        matches.update(id=m.id, org_blind=1)
    elif hide == "withdrawn_undecided":
        data.withdraw(w.vol.uid, m.id)
    elif hide in ("suspended", "deleted"):
        profiles.update(id=w.vol.uid, **{f"{hide}_at": "2026-10-15T00:00:00+00:00"})
    elif hide == "org_block":
        blocks.insert(
            blocker_id=w.coord_uid,
            blocked_user_id=w.vol.uid,
            blocked_org_id=w.org,
            created_at="2026-10-15T00:00:00+00:00",
        )
    else:
        blocks.insert(
            blocker_id=w.vol.uid,
            blocked_user_id=None,
            blocked_org_id=w.org,
            created_at="2026-10-15T00:00:00+00:00",
        )
    assert data.org_match_volunteer(w.coord_uid, m.id) == data.BLOCKED_NAME
    for page, title in (("report", "Report"), ("block", "Block")):
        html = w.coord.get(f"/org/matches/{m.id}/{page}").text
        assert f"{title} Sam" not in html and f"{title} Volunteer" in html, (hide, page)
    r = w.coord.post(f"/org/matches/{m.id}/report", data={"reason": data.REASONS[0][0]})
    assert r.status_code in (200, 303)
    assert reports(where="match_id = ?", where_args=[m.id])  # still reported


def test_a_claim_name_is_checked_like_any_name(anon):
    """L3: the claim form refuses hidden characters and names over 80; an older claim
    that slipped through is approved without its name."""
    form = {
        "ein": "53-0196605",
        "org_name": "Helpers",
        "name": "Ann Lee",
        "email": "ann@example.org",
        "authorized": "1",
    }
    for over in ({"name": "Ann\u202e Lee"}, {"org_name": "Help\u200bers"}):
        r = anon.post("/org/claim", data={**form, **over})
        assert "hidden or special characters" in r.text, over
    r = anon.post("/org/claim", data={**form, "name": "n" * 81})
    assert "Enter your name" in r.text
    with pytest.raises(ValueError):
        data.submit_claim("530196605", "Helpers", "Ann\u202e Lee", "ann@example.org")


def test_the_inbox_lists_a_volunteers_causes_a_to_z(w):
    """I4: the stored order puts loved causes first; A to Z hints at no rating."""
    profiles.update(
        id=w.vol.uid,
        causes='["food-insecurity", "animal-shelters", "environment-parks"]',
    )
    like(w)
    (row,) = data.org_inbox(w.coord_uid)["pending"]
    assert row["causes"] == [
        "Animal shelters",
        "Environment & parks",
        "Food insecurity",
    ]
