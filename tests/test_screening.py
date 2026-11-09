"""An opening that needs the nonprofit's own screening holds the first shift until a
coordinator confirms it (data.decide, data.confirm_screening)."""

from html import unescape

import pytest
from conftest import PANTRY, coordinator_for, volunteer
from test_match import opening

from app import data
from app.db import matches, messages, openings, screenings, shifts


def screened_opening(slots=1):
    o = opening(PANTRY)
    openings.update(
        id=o.id,
        slots=slots,
        starts_at="2030-01-05T09:00",
        screening_required=True,
        screening_kind="live_scan",
    )
    return o.id


def system_lines(match_id):
    return [
        m.body
        for m in messages(where="match_id = ?", where_args=[match_id], order_by="id")
        if m.sender_id == data.SYSTEM_SENDER
    ]


def test_an_accept_holds_the_first_shift_and_takes_no_spot():
    oid = screened_opening(slots=1)
    v1, v2 = volunteer(), volunteer()
    coord, _ = coordinator_for(0)
    m1, m2 = data.swipe(v1.uid, oid, True), data.swipe(v2.uid, oid, True)
    data.decide(coord, m1.id, True)
    data.decide(coord, m2.id, True)  # the held one took no spot
    for m in (m1, m2):
        assert (matches[m.id].status, matches[m.id].screening_status) == (
            "accepted",
            "pending",
        )
    assert not shifts(where="match_id IN (?, ?)", where_args=[m1.id, m2.id])
    assert openings[oid].status == "open"
    assert system_lines(m1.id) == [
        (
            f"{data.org_name_of(oid)} needs to confirm your screening "
            "(live scan fingerprinting) before your first shift."
        )
    ]
    with pytest.raises(ValueError):
        data.add_shift(coord, m1.id, "2030-01-12T09:00")


def test_confirming_books_the_shift_records_the_screening_and_fills_the_spot():
    oid = screened_opening(slots=1)
    v1, v2 = volunteer(), volunteer()
    coord, _ = coordinator_for(0)
    other, _ = coordinator_for(1)
    m1, m2 = data.swipe(v1.uid, oid, True), data.swipe(v2.uid, oid, True)
    data.decide(coord, m1.id, True)
    data.decide(coord, m2.id, True)
    with pytest.raises(PermissionError):
        data.confirm_screening(other, m1.id)
    data.confirm_screening(coord, m1.id)
    assert matches[m1.id].screening_status == "cleared"
    (sh,) = shifts(where="match_id = ?", where_args=[m1.id])
    assert (sh.starts_at, sh.status, sh.kind) == (
        "2030-01-05T09:00",
        "scheduled",
        "accept",
    )
    (row,) = screenings(where="volunteer_id = ?", where_args=[v1.uid])
    assert (row.org_id, row.kind, row.cleared_by) == (
        openings[oid].org_id,
        "live_scan",
        coord,
    )
    assert row.cleared_at
    assert system_lines(m1.id)[-1] == (
        "Screening confirmed: your shift on Sat Jan 5, 9:00 AM is booked."
    )
    assert openings[oid].status == "filled"
    with pytest.raises(ValueError, match="no slots left"):
        data.confirm_screening(coord, m2.id)  # the one spot is taken now
    with pytest.raises(ValueError):
        data.confirm_screening(coord, m1.id)  # already confirmed


def test_the_volunteer_sees_the_wait_then_the_shift_and_the_org_confirms(user, coord):
    oid = screened_opening(slots=2)
    org = data.org_name_of(oid)
    user.post("/swipe", data={"opening_id": oid, "liked": "1"})
    mid = matches()[0].id
    coord.post(f"/org/matches/{mid}/decide", data={"decision": "accept"})
    html = unescape(user.get("/matches").text)
    assert f"Matched · waiting for {org} to confirm your screening" in html
    assert "Sat Jan 5, 9:00 AM" not in html and ".ics" not in html
    inbox = coord.get("/org/inbox").text
    assert "Screening pending" in inbox and f"/org/matches/{mid}/screening" in inbox
    r = coord.post(f"/org/matches/{mid}/screening", follow_redirects=False)
    assert r.headers["location"] == "/org/inbox?notice=screened"
    html = unescape(user.get("/matches").text)
    assert "confirm your screening" not in html
    (sh,) = shifts(where="match_id = ?", where_args=[mid])
    assert "Sat Jan 5, 9:00 AM" in html and f"/shifts/{sh.id}.ics" in html
    assert "Screening pending" not in coord.get("/org/inbox").text
