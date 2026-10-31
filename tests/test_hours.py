"""My hours: a volunteer's confirmed hours by month, from their own attended shifts."""

from conftest import coordinator, pantry_opening, volunteer

from app import data
from app.db import shifts


def _shift(match, day, minutes, by, status="completed"):
    shifts.insert(
        match_id=match.id,
        starts_at=f"2026-10-{day:02d}T09:00",
        status=status,
        kind="added",
        created_by=by,
        confirmed_minutes=minutes,
        marked_by=by,
        marked_at="2026-10-30T10:00:00",
    )


def test_my_hours_counts_only_confirmed_hours_of_attended_shifts():
    a, b, org = volunteer("Zelda"), volunteer("Bianca"), coordinator(0)
    mine = data.swipe(a.uid, pantry_opening().id, True)
    theirs = data.swipe(b.uid, pantry_opening().id, True)
    for m in (mine, theirs):
        data.decide(org.uid, m.id, True)
    _shift(mine, 3, 180, org.uid)
    _shift(mine, 10, 150, org.uid)
    _shift(mine, 11, None, org.uid, status="no_show")
    _shift(mine, 12, None, org.uid)  # attended before hours were confirmed
    _shift(theirs, 4, 120, org.uid)

    page = a.get("/hours").text
    assert "October 2026" in page and "5.5 hours" in page
    assert "Confirmed by Pat Doe, coordinator" in page
    assert "1 earlier shift has no confirmed hours" in page
    assert "2 hours" not in page and "7.5 hours" not in page and "8 hours" not in page
    assert 'href="/hours"' in a.get("/history").text
    assert 'href="/hours"' in a.get("/profile").text
    assert "No confirmed hours yet" in volunteer("Nobody").get("/hours").text
