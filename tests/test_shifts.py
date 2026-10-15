"""Shifts: made on accept, scheduled again by a coordinator, marked after they start."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import coordinator_for, org_sign_in, sign_in
from starlette.testclient import TestClient

from app import data
from app.db import matches, openings, orgs, profiles, shifts
from main import app

LA = ZoneInfo("America/Los_Angeles")
TITLE = "Sort and pack pantry boxes"
FMT = "%Y-%m-%dT%H:%M"


class Clock:
    def __init__(self, monkeypatch):
        self.at = datetime.now(LA).replace(second=0, microsecond=0)
        monkeypatch.setattr(data, "_local_now", lambda: self.at.strftime(FMT))
        monkeypatch.setattr(
            data, "_now", lambda: self.at.astimezone(UTC).isoformat(timespec="seconds")
        )

    def advance(self, **delta):
        self.at += timedelta(**delta)

    def local(self, **delta):
        return (self.at + timedelta(**delta)).strftime(FMT)


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def volunteer(name="Zelda"):
    c = TestClient(app)
    sign_in(c)
    c.uid = data.get_or_create_volunteer(c.phone)
    profiles.update(id=c.uid, display_name=name, onboarded_at="2026-01-01T00:00:00")
    return c


def coordinator(org_index):
    uid, email = coordinator_for(org_index)
    c = TestClient(app)
    org_sign_in(c, email)
    c.uid = uid
    return c


@pytest.fixture
def world(clock):
    """Volunteer A with a pending match on org 0's opening; coordinators of org 0 and 1."""

    class W:
        pass

    w = W()
    w.a, w.b = volunteer("Zelda"), volunteer("Bianca")
    w.org0, w.org1 = coordinator(0), coordinator(1)
    w.opening = openings(where="title = ?", where_args=[TITLE])[0]
    assert w.opening.org_id == orgs()[0].id
    w.match = data.swipe(w.a.uid, w.opening.id, True)
    return w


@pytest.fixture
def accepted(world):
    data.decide(world.org0.uid, world.match.id, True)
    return world


def shifts_of(match):
    return shifts(where="match_id = ?", where_args=[match.id], order_by="starts_at")


def test_accept_creates_the_first_shift_at_the_openings_start(accepted):
    [s] = shifts_of(accepted.match)
    assert (s.starts_at, s.status) == (accepted.opening.starts_at, "scheduled")


def test_decline_and_pending_make_no_shift(world):
    assert shifts.count == 0
    data.decide(world.org0.uid, world.match.id, False)
    assert shifts.count == 0


def test_add_shift_for_another_orgs_match_is_refused(accepted, clock):
    with pytest.raises(PermissionError):
        data.add_shift(accepted.org1.uid, accepted.match.id, clock.local(days=3))
    assert shifts.count == 1


def test_add_shift_needs_an_accepted_match(world, clock):
    with pytest.raises(ValueError, match="not accepted"):
        data.add_shift(world.org0.uid, world.match.id, clock.local(days=3))


def test_add_shift_in_the_past_is_refused(accepted, clock):
    for bad in (clock.local(days=-1), clock.local()):
        with pytest.raises(ValueError, match="future"):
            data.add_shift(accepted.org0.uid, accepted.match.id, bad)
    assert shifts.count == 1


def test_add_shift_is_limited_to_366_days_and_to_real_times(accepted, clock):
    uid, mid = accepted.org0.uid, accepted.match.id
    with pytest.raises(ValueError, match="12 months"):
        data.add_shift(uid, mid, clock.local(days=367))
    for junk in ("", "tomorrow", "2030-02-30T10:00", None, 5):
        with pytest.raises(ValueError):
            data.add_shift(uid, mid, junk)
    data.add_shift(uid, mid, clock.local(days=366))
    assert shifts.count == 2


def test_a_second_shift_on_the_same_match_is_recorded_once(accepted, clock):
    when = clock.local(days=3)
    s = data.add_shift(accepted.org0.uid, accepted.match.id, when)
    assert (s.match_id, s.starts_at, s.status) == (accepted.match.id, when, "scheduled")
    with pytest.raises(ValueError, match="already"):
        data.add_shift(accepted.org0.uid, accepted.match.id, when)
    assert len(shifts_of(accepted.match)) == 2


def test_complete_before_the_start_is_refused(accepted, clock):
    [s] = shifts_of(accepted.match)
    with pytest.raises(ValueError, match="not started"):
        data.complete_shift(accepted.org0.uid, s.id, True)
    assert shifts[s.id].status == "scheduled"


def test_complete_marks_the_status_and_who(accepted, clock):
    [s] = shifts_of(accepted.match)
    clock.at = datetime.fromisoformat(s.starts_at).replace(tzinfo=LA) + timedelta(
        hours=2
    )
    done = data.complete_shift(accepted.org0.uid, s.id, True)
    assert (done.status, done.marked_by) == ("completed", accepted.org0.uid)
    assert done.marked_at
    with pytest.raises(PermissionError):
        data.complete_shift(accepted.org1.uid, s.id, False)


def test_one_correction_within_seven_days_then_no_more(accepted, clock):
    [s] = shifts_of(accepted.match)
    clock.at = datetime.fromisoformat(s.starts_at).replace(tzinfo=LA) + timedelta(
        hours=2
    )
    uid = accepted.org0.uid
    data.complete_shift(uid, s.id, True)
    clock.advance(days=6)
    assert data.complete_shift(uid, s.id, False).status == "no_show"
    with pytest.raises(ValueError, match="corrected"):
        data.complete_shift(uid, s.id, True)
    assert shifts[s.id].status == "no_show"


def test_a_mark_after_seven_days_is_refused(accepted, clock):
    [s] = shifts_of(accepted.match)
    clock.at = datetime.fromisoformat(s.starts_at).replace(tzinfo=LA) + timedelta(
        hours=2
    )
    data.complete_shift(accepted.org0.uid, s.id, True)
    clock.advance(days=7, minutes=1)
    with pytest.raises(ValueError, match="7 days"):
        data.complete_shift(accepted.org0.uid, s.id, False)
    assert shifts[s.id].status == "completed"
    # marking the same result again is harmless
    assert data.complete_shift(accepted.org0.uid, s.id, True).status == "completed"


def test_withdraw_cancels_future_shifts_but_not_past_completed_ones(accepted, clock):
    uid, mid = accepted.org0.uid, accepted.match.id
    [first] = shifts_of(accepted.match)
    later = data.add_shift(uid, mid, clock.local(days=30))
    clock.at = datetime.fromisoformat(first.starts_at).replace(tzinfo=LA) + timedelta(
        hours=2
    )
    data.complete_shift(uid, first.id, True)
    data.withdraw(accepted.a.uid, mid)
    assert shifts[first.id].status == "completed"
    assert shifts[later.id].status == "cancelled"
    assert matches[mid].status == "withdrawn"


def test_withdraw_leaves_a_past_unmarked_shift_to_be_marked(accepted, clock):
    [first] = shifts_of(accepted.match)
    clock.at = datetime.fromisoformat(first.starts_at).replace(tzinfo=LA) + timedelta(
        hours=2
    )
    data.withdraw(accepted.a.uid, accepted.match.id)
    assert shifts[first.id].status == "scheduled"
    assert data.complete_shift(accepted.org0.uid, first.id, False).status == "no_show"


def test_cancelled_shift_cannot_be_marked(accepted, clock):
    [first] = shifts_of(accepted.match)
    data.withdraw(accepted.a.uid, accepted.match.id)
    clock.at = datetime.fromisoformat(first.starts_at).replace(tzinfo=LA) + timedelta(
        hours=2
    )
    assert shifts[first.id].status == "cancelled"
    with pytest.raises(ValueError, match="cancelled"):
        data.complete_shift(accepted.org0.uid, first.id, True)


def test_volunteer_history_shows_only_their_own(accepted, clock):
    other = data.swipe(accepted.b.uid, accepted.opening.id, True)
    data.decide(accepted.org0.uid, other.id, True)
    data.add_shift(accepted.org0.uid, accepted.match.id, clock.local(days=3))
    mine = accepted.a.get("/history")
    assert mine.status_code == 200 and TITLE in mine.text
    assert "Waiting for the nonprofit to confirm" in mine.text
    assert len(data.my_shifts(accepted.a.uid)["upcoming"]) == 2
    assert len(data.my_shifts(accepted.b.uid)["upcoming"]) == 1
    # A's history never names B's shift: compare ids
    ids = {s["id"] for s in data.my_shifts(accepted.a.uid)["upcoming"]}
    assert ids == {s.id for s in shifts_of(accepted.match)}
    assert not any(s["id"] in ids for s in data.my_shifts(accepted.b.uid)["upcoming"])


def test_history_splits_upcoming_and_past_and_hides_cancelled(accepted, clock):
    uid, mid = accepted.org0.uid, accepted.match.id
    [first] = shifts_of(accepted.match)
    clock.at = datetime.fromisoformat(first.starts_at).replace(tzinfo=LA) + timedelta(
        hours=2
    )
    data.complete_shift(uid, first.id, True)
    soon = data.add_shift(uid, mid, clock.local(days=2))
    gone = data.add_shift(uid, mid, clock.local(days=4))
    data.withdraw(accepted.a.uid, mid)
    h = data.my_shifts(accepted.a.uid)
    assert [s["id"] for s in h["upcoming"]] == []
    assert [s["id"] for s in h["past"]] == [first.id]
    assert {soon.id, gone.id}.isdisjoint(s["id"] for s in h["past"])
    page = accepted.a.get("/history").text
    assert "Attended" in page


def test_org_shifts_lists_to_mark_upcoming_and_recent(accepted, clock):
    uid = accepted.org0.uid
    [first] = shifts_of(accepted.match)
    later = data.add_shift(uid, accepted.match.id, clock.local(days=40))
    clock.at = datetime.fromisoformat(first.starts_at).replace(tzinfo=LA) + timedelta(
        hours=2
    )
    box = data.org_shifts(uid)
    assert [s["id"] for s in box["to_mark"]] == [first.id]
    assert [s["id"] for s in box["upcoming"]] == [later.id]
    assert box["to_mark"][0]["first_name"] == "Zelda"
    assert data.org_shifts(accepted.org1.uid) == {
        "upcoming": [],
        "to_mark": [],
        "marked": [],
    }
    data.complete_shift(uid, first.id, True)
    assert [s["id"] for s in data.org_shifts(uid)["marked"]] == [first.id]
    clock.advance(days=8)
    assert data.org_shifts(uid)["marked"] == []


def test_coordinator_pages_mark_and_schedule(accepted, clock):
    [first] = shifts_of(accepted.match)
    clock.at = datetime.fromisoformat(first.starts_at).replace(tzinfo=LA) + timedelta(
        hours=2
    )
    page = accepted.org0.get("/org/shifts")
    assert page.status_code == 200 and "Attended" in page.text and "Zelda" in page.text
    r = accepted.org0.post(
        f"/org/shifts/{first.id}/mark",
        data={"result": "no_show"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and shifts[first.id].status == "no_show"
    r = accepted.org0.post(
        f"/org/shifts/{first.id}/mark", data={"result": "maybe"}, follow_redirects=False
    )
    assert r.status_code == 400
    form = accepted.org0.get(f"/org/matches/{accepted.match.id}/shifts/new")
    assert form.status_code == 200 and "datetime-local" in form.text
    r = accepted.org0.post(
        f"/org/matches/{accepted.match.id}/shifts/new",
        data={"starts_at": clock.local(days=-1)},
        follow_redirects=False,
    )
    assert r.status_code == 200 and "future" in r.text
    r = accepted.org0.post(
        f"/org/matches/{accepted.match.id}/shifts/new",
        data={"starts_at": clock.local(days=5)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and len(shifts_of(accepted.match)) == 2
    inbox = accepted.org0.get("/org/inbox").text
    assert "Schedule another shift" in inbox


def test_another_orgs_shift_ids_get_403(accepted, clock):
    [first] = shifts_of(accepted.match)
    shifts.update(id=first.id, starts_at="2020-01-01T09:00")
    r = accepted.org1.post(
        f"/org/shifts/{first.id}/mark",
        data={"result": "attended"},
        follow_redirects=False,
    )
    assert r.status_code == 403 and shifts[first.id].status == "scheduled"
    r = accepted.org1.post(
        f"/org/matches/{accepted.match.id}/shifts/new",
        data={"starts_at": clock.local(days=5)},
        follow_redirects=False,
    )
    assert r.status_code == 403 and shifts.count == 1
    for bad in (0, 2**63, 999999):
        r = accepted.org0.post(
            f"/org/shifts/{bad}/mark",
            data={"result": "attended"},
            follow_redirects=False,
        )
        assert r.status_code == 403


def test_shifts_vanish_with_their_match(accepted):
    assert shifts.count == 1
    matches.delete_where()
    assert shifts.count == 0
