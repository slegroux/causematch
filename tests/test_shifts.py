"""Shifts: made on accept, scheduled again by a coordinator, marked after they start."""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import (
    PANTRY,
    ROOT,
    coordinator,
    coordinator_for,
    pantry_opening,
    volunteer,
)

from app import data
from app.db import matches, openings, orgs, shifts

LA = ZoneInfo("America/Los_Angeles")
FMT = "%Y-%m-%dT%H:%M"


# A fixed instant, clear of both DST changes, so no test depends on the day it runs.
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=LA)
OPENING_START = "2026-10-22T09:00"


class Clock:
    def __init__(self, monkeypatch):
        self.at = NOW
        self.monkeypatch = monkeypatch

    def freeze(self):
        """Stop the clock here. After sign-in: login tokens expire on the real clock."""
        self.monkeypatch.setattr(data, "_local_now", lambda: self.at.strftime(FMT))
        self.monkeypatch.setattr(
            data, "_now", lambda: self.at.astimezone(UTC).isoformat(timespec="seconds")
        )

    def advance(self, **delta):
        self.at += timedelta(**delta)

    def local(self, **delta):
        return (self.at + timedelta(**delta)).strftime(FMT)


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


@pytest.fixture
def world(clock):
    """Volunteer A with a pending match on org 0's opening; coordinators of org 0 and 1."""

    w = SimpleNamespace()
    w.a, w.b = volunteer("Zelda"), volunteer("Bianca")
    w.org0, w.org1 = coordinator(0), coordinator(1)
    w.opening = pantry_opening()
    openings.update(id=w.opening.id, starts_at=OPENING_START)
    w.opening = openings[w.opening.id]
    assert w.opening.org_id == orgs()[0].id
    w.match = data.swipe(w.a.uid, w.opening.id, True)
    clock.freeze()
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
    assert mine.status_code == 200 and PANTRY in mine.text
    assert "Scheduled" in mine.text
    assert "Waiting for the nonprofit to confirm" not in mine.text
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
    assert box["to_mark"][0]["name"].startswith("Zelda")
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


def test_accepting_after_the_opening_started_creates_no_shift(world, clock):
    clock.advance(days=8)  # the opening started while the like was pending
    data.decide(world.org0.uid, world.match.id, True)
    assert shifts_of(world.match) == []
    assert data.org_shifts(world.org0.uid)["to_mark"] == []
    # the coordinator can still schedule one
    s = data.add_shift(world.org0.uid, world.match.id, clock.local(days=2))
    assert s.kind == "added"


def test_shifts_record_who_made_and_who_corrected_them(accepted, clock):
    [first] = shifts_of(accepted.match)
    assert (first.kind, first.created_by) == ("accept", accepted.org0.uid)
    second = coordinator_for(0, "second@example.org")[0]
    added = data.add_shift(second, accepted.match.id, clock.local(days=3))
    assert (added.kind, added.created_by) == ("added", second)
    clock.at = datetime.fromisoformat(first.starts_at).replace(tzinfo=LA) + timedelta(
        hours=2
    )
    data.complete_shift(accepted.org0.uid, first.id, True)
    done = data.complete_shift(second, first.id, False)
    assert (done.marked_by, done.corrected_by) == (accepted.org0.uid, second)


@pytest.mark.parametrize("value", ["2026-10-20T10:00", "2026-10-20T10:00:00"])
def test_a_value_with_or_without_seconds_is_accepted(accepted, value):
    s = data.add_shift(accepted.org0.uid, accepted.match.id, value)
    assert s.starts_at == "2026-10-20T10:00"


@pytest.mark.parametrize(
    "value", ["", "tomorrow", "2026-10-20 10:00", "2026-10-20T10:00:30", "2026-10-20"]
)
def test_a_bad_date_gets_a_friendly_message(accepted, value):
    r = accepted.org0.post(
        f"/org/matches/{accepted.match.id}/shifts/new",
        data={"starts_at": value},
        follow_redirects=False,
    )
    assert r.status_code == 200 and "pick a date and a time" in r.text
    assert "strptime" not in r.text and "does not match format" not in r.text
    assert len(shifts_of(accepted.match)) == 1


def test_cancel_shift_only_before_it_starts(accepted, clock):
    uid = accepted.org0.uid
    [first] = shifts_of(accepted.match)
    later = data.add_shift(uid, accepted.match.id, clock.local(days=30))
    gone = data.cancel_shift(uid, later.id)
    assert (gone.status, gone.cancelled_by) == ("cancelled", uid) and gone.cancelled_at
    with pytest.raises(ValueError):
        data.cancel_shift(uid, later.id)
    with pytest.raises(PermissionError):
        data.cancel_shift(accepted.org1.uid, first.id)
    assert shifts[first.id].status == "scheduled"
    clock.at = datetime.fromisoformat(first.starts_at).replace(tzinfo=LA) + timedelta(
        hours=1
    )
    with pytest.raises(ValueError):
        data.cancel_shift(uid, first.id)
    assert data.my_shifts(accepted.a.uid)["upcoming"] == []


def test_cancel_route(accepted, clock):
    [first] = shifts_of(accepted.match)
    page = accepted.org0.get("/org/shifts").text
    assert "Cancel this shift" in page
    r = accepted.org0.post(f"/org/shifts/{first.id}/cancel", follow_redirects=False)
    assert r.status_code == 303 and shifts[first.id].status == "cancelled"
    r = accepted.org0.post(f"/org/shifts/{first.id}/cancel", follow_redirects=False)
    assert r.status_code == 303 and "notice=unavailable" in r.headers["location"]


def test_closing_an_opening_cancels_its_future_shifts_only(accepted, clock):
    uid = accepted.org0.uid
    [first] = shifts_of(accepted.match)
    later = data.add_shift(uid, accepted.match.id, clock.local(days=30))
    clock.at = datetime.fromisoformat(first.starts_at).replace(tzinfo=LA) + timedelta(
        hours=2
    )
    data.complete_shift(uid, first.id, True)
    data.close_opening(uid, accepted.opening.id)
    assert shifts[later.id].status == "cancelled"
    assert shifts[first.id].status == "completed"


def weekly(world):
    openings.update(id=world.opening.id, recurrence="weekly")


def test_same_time_next_week_adds_a_shift_seven_days_after_the_latest(world, clock):
    weekly(world)
    data.decide(world.org0.uid, world.match.id, True)
    uid = world.org0.uid
    assert data.repeat_shift(uid, world.match.id).starts_at == "2026-10-29T09:00"
    assert data.repeat_shift(uid, world.match.id).starts_at == "2026-11-05T09:00"
    # a cancelled latest shift does not count
    last = shifts_of(world.match)[-1]
    data.cancel_shift(uid, last.id)
    assert data.repeat_shift(uid, world.match.id).starts_at == "2026-11-05T09:00"
    with pytest.raises(PermissionError):
        data.repeat_shift(world.org1.uid, world.match.id)


def test_same_time_next_week_keeps_the_wall_clock_across_dst(world, clock):
    weekly(world)
    openings.update(id=world.opening.id, starts_at="2026-10-29T09:00")
    data.decide(world.org0.uid, world.match.id, True)  # next week crosses Nov 1
    assert data.repeat_shift(world.org0.uid, world.match.id).starts_at == (
        "2026-11-05T09:00"
    )


def test_same_time_next_week_needs_a_weekly_opening(accepted):
    with pytest.raises(ValueError, match="weekly"):
        data.repeat_shift(accepted.org0.uid, accepted.match.id)


def test_same_time_next_week_button_and_route(world, clock):
    weekly(world)
    data.decide(world.org0.uid, world.match.id, True)
    assert "Same time next week" in world.org0.get("/org/shifts").text
    assert "Same time next week" in world.org0.get("/org/inbox").text
    r = world.org0.post(
        f"/org/matches/{world.match.id}/shifts/repeat", follow_redirects=False
    )
    assert r.status_code == 303 and len(shifts_of(world.match)) == 2
    page = world.org0.get("/org/shifts").text
    assert page.count("Same time next week") == 1  # only on the latest shift
    r = world.org1.post(
        f"/org/matches/{world.match.id}/shifts/repeat", follow_redirects=False
    )
    assert r.status_code == 403 and len(shifts_of(world.match)) == 2


def test_a_time_skipped_by_dst_is_refused(accepted):
    with pytest.raises(ValueError, match="doesn't exist"):
        data.add_shift(accepted.org0.uid, accepted.match.id, "2027-03-14T02:30")
    assert len(shifts_of(accepted.match)) == 1


def test_a_time_in_the_repeated_fall_back_hour_is_accepted_as_written(accepted):
    s = data.add_shift(accepted.org0.uid, accepted.match.id, "2026-11-01T01:30")
    assert s.starts_at == "2026-11-01T01:30"
    # the same string again is a duplicate, not a second 01:30
    with pytest.raises(ValueError, match="already"):
        data.add_shift(accepted.org0.uid, accepted.match.id, "2026-11-01T01:30")


def run_python(code, db_path):
    env = {**os.environ, "CAUSEMATCH_DB": str(db_path)}
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_adding_a_column_to_shift_keeps_its_rows_and_heals_the_old_trigger(tmp_path):
    """The early build's trigger broke every later transform of `shift`: the rename
    failed half-way and the rows were stranded. Starting up must drop it first."""
    code = """
import importlib
import app.db as d
d.db.execute(
    "CREATE TRIGGER shift_with_match AFTER DELETE ON match "
    "BEGIN DELETE FROM shift WHERE match_id = OLD.id; END"
)
d.shifts.insert(d.Shift(match_id=1, starts_at="2030-01-01T09:00", status="scheduled"))
d = importlib.reload(d)  # a restart: the trigger must go before any transform
assert not d.db.q("select 1 from sqlite_master where type = 'trigger'")
class Shift:
    __annotations__ = {**d.Shift.__annotations__, "note": str}
d.db.create(Shift, pk="id", transform=True)
rows = d.db.q("select match_id, starts_at, note from shift")
assert [(r["match_id"], r["starts_at"], r["note"]) for r in rows] == [
    (1, "2030-01-01T09:00", None)
], rows
print("ok")
"""
    done = run_python(code, tmp_path / "t.db")
    assert done.returncode == 0 and "ok" in done.stdout, done.stderr[-800:]


def test_the_ics_is_one_event_in_los_angeles_time(accepted):
    openings.update(id=accepted.opening.id, title="Sort, pack; then\nstack")
    [s] = shifts_of(accepted.match)
    r = accepted.a.get(f"/shifts/{s.id}.ics")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/calendar; charset=utf-8"
    assert r.headers["content-disposition"].startswith("attachment;")
    raw = r.content.decode()
    assert raw.endswith("\r\n") and "\n" not in raw.replace("\r\n", "")
    assert all(len(line.encode()) <= 75 for line in raw.split("\r\n"))
    lines = raw.replace("\r\n ", "").split("\r\n")  # unfolded
    assert lines.count("BEGIN:VEVENT") == 1 and "END:VEVENT" in lines
    assert "DTSTART;TZID=America/Los_Angeles:20261022T090000" in lines
    assert "DTEND;TZID=America/Los_Angeles:20261022T110000" in lines
    assert f"SUMMARY:Sort\\, pack\\; then\\nstack · {orgs()[0].name}" in lines
    assert f"DESCRIPTION:Chat with {orgs()[0].name}: " in "\n".join(lines)
    assert f"https://causematch.test/chat/{accepted.match.id}" in raw.replace(
        "\r\n ", ""
    )
    assert any(line.startswith(f"UID:shift-{s.id}@") for line in lines)


def test_only_the_volunteer_on_an_upcoming_shift_gets_its_ics(accepted, clock, anon):
    [s] = shifts_of(accepted.match)
    path = f"/shifts/{s.id}.ics"
    assert accepted.b.get(path).status_code == 404  # another volunteer
    for staff in (accepted.org0, accepted.org1):  # turned away before the handler
        assert staff.get(path, follow_redirects=False).status_code in (303, 403)
    r = anon.get(path, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/login")
    clock.advance(days=8)  # past
    assert accepted.a.get(path).status_code == 404
    clock.at = NOW
    data.cancel_shift(accepted.org0.uid, s.id)
    assert accepted.a.get(path).status_code == 404
