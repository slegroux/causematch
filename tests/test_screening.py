"""An opening that needs the nonprofit's own screening holds the first shift until a
coordinator confirms it (data.decide, data.confirm_screening)."""

from html import unescape

import pytest
from conftest import PANTRY, coordinator, coordinator_for, volunteer
from test_match import opening

from app import data, notify
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


def test_an_accept_holds_the_first_shift_and_keeps_a_spot_without_filling_it():
    oid = screened_opening(slots=2)
    v1, v2, v3 = volunteer(), volunteer(), volunteer()
    coord, _ = coordinator_for(0)
    m1, m2, m3 = (data.swipe(v.uid, oid, True) for v in (v1, v2, v3))
    data.decide(coord, m1.id, True)
    data.decide(coord, m2.id, True)
    for m in (m1, m2):
        assert (matches[m.id].status, matches[m.id].screening_status) == (
            "accepted",
            "pending",
        )
    assert not shifts(where="match_id IN (?, ?)", where_args=[m1.id, m2.id])
    assert openings[oid].status == "open"  # held, so nothing filled
    assert system_lines(m1.id) == [
        (
            f"{data.org_name_of(oid)} needs to confirm your screening "
            "(Live Scan fingerprinting) before your first shift."
        )
    ]
    # each held volunteer keeps a spot: no third accept, no fewer spots than holds
    with pytest.raises(ValueError, match="screening pending"):
        data.decide(coord, m3.id, True)
    with pytest.raises(ValueError):
        data.update_opening(coord, oid, slots=1)
    with pytest.raises(ValueError):
        data.add_shift(coord, m1.id, "2030-01-12T09:00")


def test_confirming_books_the_shift_records_the_screening_and_fills_the_spot():
    oid = screened_opening(slots=2)
    v1, v2 = volunteer(), volunteer()
    coord, _ = coordinator_for(0)
    other, _ = coordinator_for(1)
    m1, m2 = data.swipe(v1.uid, oid, True), data.swipe(v2.uid, oid, True)
    data.decide(coord, m1.id, True)
    data.decide(coord, m2.id, True)
    with pytest.raises(PermissionError):
        data.confirm_screening(other, m1.id)
    assert data.opening_counts(coord)[oid]["accepted"] == 0
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
    assert data.opening_counts(coord)[oid]["accepted"] == 1
    with pytest.raises(ValueError):
        data.confirm_screening(coord, m1.id)  # already confirmed
    openings.update(id=oid, status="closed")
    with pytest.raises(ValueError, match="opening closed"):
        data.confirm_screening(coord, m2.id)
    assert matches[m2.id].screening_status == "pending"


def test_deleting_the_account_leaves_no_screening_and_ends_a_held_match():
    oid = screened_opening(slots=2)
    cleared, waiting = volunteer(), volunteer()
    coord, _ = coordinator_for(0)
    m1 = data.swipe(cleared.uid, oid, True)
    m2 = data.swipe(waiting.uid, oid, True)
    data.decide(coord, m1.id, True)
    data.decide(coord, m2.id, True)
    data.confirm_screening(coord, m1.id)
    assert screenings.count_where("volunteer_id = ?", [cleared.uid]) == 1
    data.delete_account(cleared.uid)
    data.delete_account(waiting.uid)
    ids = [cleared.uid, waiting.uid]
    assert screenings.count_where("volunteer_id IN (?, ?)", ids) == 0
    assert matches[m2.id].status == "withdrawn"


def test_the_volunteer_sees_the_wait_then_the_shift_and_the_org_confirms(user, coord):
    oid = screened_opening(slots=2)
    org = data.org_name_of(oid)
    user.post("/swipe", data={"opening_id": oid, "liked": "1"})
    mid = matches()[0].id
    coord.post(f"/org/matches/{mid}/decide", data={"decision": "accept"})
    html = unescape(user.get("/matches").text)
    assert f"Waiting for {org} to confirm your screening" in html
    assert "Sat Jan 5, 9:00 AM" not in html and ".ics" not in html
    assert "(not booked yet)" in user.get(f"/chat/{mid}").text
    inbox = coord.get("/org/inbox").text
    assert "Screening pending" in inbox and f"/org/matches/{mid}/screening" in inbox
    ask = unescape(coord.get(f"/org/matches/{mid}/screening").text)
    assert "cleared your screening (Live Scan fingerprinting)?" in ask
    assert "This books their shift on Sat Jan 5, 9:00 AM" in ask
    assert coordinator(1).post(f"/org/matches/{mid}/screening").status_code == 403
    notify.get_sms().sent.clear()
    r = coord.post(f"/org/matches/{mid}/screening", follow_redirects=False)
    assert r.headers["location"] == "/org/inbox?notice=screened"
    assert any(f"New message from {org}" in str(t) for t in notify.get_sms().sent)
    html = unescape(user.get("/matches").text)
    assert "confirm your screening" not in html
    (sh,) = shifts(where="match_id = ?", where_args=[mid])
    assert "Sat Jan 5, 9:00 AM" in html and f"/shifts/{sh.id}.ics" in html
    assert "Screening pending" not in coord.get("/org/inbox").text
