"""Pilot metrics: hand-computed rates, the admin pages, the CSV and the stats script."""

import csv
import io
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import coordinator_for, org_sign_in, sign_in
from starlette.testclient import TestClient

from app import data, notify
from app.db import (
    DB_PATH,
    Match,
    Profile,
    Shift,
    Swipe,
    matches,
    openings,
    orgs,
    profiles,
    shifts,
    swipes,
)
from app.metrics import METRICS, csv_rows, pilot_metrics
from main import app

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)  # 05:00 in Los Angeles
T0 = "2026-10-01T00:00:00+00:00"


def at(hours):
    h = int(hours)
    return f"2026-10-01T{h:02d}:{int((hours - h) * 60):02d}:00+00:00"


@pytest.fixture
def pilot():
    """Five volunteers, a warm org's opening (food) and a cold org's (parks).

    Warm opening A: v1-v4 like, v5 passes. v1, v2, v3 accepted (replies 2, 6, 4 h), v4
    declined (10 h). Shifts: v1 completed twice, v2 completed, v3 no-show.
    Cold opening B: v1, v2, v4 like, v3 passes. v1 accepted (1 h), v4 accepted (3 h, shift
    still in the future), v2 pending for 50 h. v1 completes a shift.
    """
    orgs.update(id=orgs()[0].id, intro="warm")
    a = openings(where="org_id = ?", where_args=[orgs()[0].id])[0]
    b = openings(where="org_id = ?", where_args=[orgs()[2].id])[0]
    heard = ["personal", "personal", "public", "other", "public"]
    v = [
        profiles.insert(
            Profile(
                role="volunteer",
                phone=f"+1310555019{i}",
                display_name=f"Private{i}",
                heard_from=heard[i],
                created_at=T0,
                # v5 left; their one pass still counts
                deleted_at=T0 if i == 4 else None,
            )
        ).id
        for i in range(5)
    ]

    def swipe(vi, opening, liked):
        swipes.insert(
            Swipe(volunteer_id=v[vi], opening_id=opening.id, liked=liked, created_at=T0)
        )

    def match(vi, opening, status, created=T0, replied=None):
        return matches.insert(
            Match(
                opening_id=opening.id,
                volunteer_id=v[vi],
                status=status,
                created_at=created,
                decided_at=at(replied) if replied is not None else None,
            )
        ).id

    def shift(mid, starts, status):
        shifts.insert(
            Shift(match_id=mid, starts_at=starts, status=status, created_at=T0)
        )

    for vi in range(4):
        swipe(vi, a, True)
    swipe(4, a, False)
    m_a = [match(0, a, "accepted", replied=2), match(1, a, "accepted", replied=6),
           match(2, a, "accepted", replied=4), match(3, a, "declined", replied=10)]  # fmt: skip
    shift(m_a[0], "2026-10-05T09:00", "completed")
    shift(m_a[0], "2026-10-12T09:00", "completed")
    shift(m_a[1], "2026-10-05T09:00", "completed")
    shift(m_a[2], "2026-10-05T09:00", "no_show")
    for vi in (0, 1, 3):
        swipe(vi, b, True)
    swipe(2, b, False)
    m_b1 = match(0, b, "accepted", replied=1)
    match(1, b, "pending", created="2026-10-13T10:00:00+00:00")
    m_b4 = match(3, b, "accepted", replied=3)
    shift(m_b1, "2026-10-06T09:00", "completed")
    shift(m_b4, "2026-10-20T09:00", "scheduled")
    return pilot_metrics(data.metrics_rows(), NOW)


def value(m, kind, group, metric):
    cells = m["overall"] if kind == "overall" else m[kind][group]
    return cells[metric]["value"]


def test_overall_rates_are_the_hand_computed_ones(pilot):
    o = pilot["overall"]
    assert (o["like_rate"]["n"], o["like_rate"]["d"]) == (7, 9)
    assert (o["accept_rate"]["n"], o["accept_rate"]["d"]) == (5, 6)
    assert o["median_reply_h"]["value"] == 3.5  # 1 2 3 4 6 10
    assert o["pending_over_48h"]["value"] == 1
    assert (o["shift1_rate"]["n"], o["shift1_rate"]["d"]) == (3, 4)  # B v4 not started
    assert (o["shift2_rate"]["n"], o["shift2_rate"]["d"]) == (1, 2)  # only v1 repeats
    assert (o["no_show_rate"]["n"], o["no_show_rate"]["d"]) == (1, 5)


def test_three_of_four_accepted_is_a_rate_of_075(pilot):
    assert value(pilot, "intro", "warm", "accept_rate") == 0.75


def test_warm_and_cold_split(pilot):
    assert value(pilot, "intro", "warm", "like_rate") == 4 / 5
    assert value(pilot, "intro", "cold", "like_rate") == 3 / 4
    assert value(pilot, "intro", "warm", "median_reply_h") == 5.0  # 2 4 6 10
    assert value(pilot, "intro", "cold", "median_reply_h") == 2.0  # 1 3
    assert value(pilot, "intro", "warm", "pending_over_48h") == 0
    assert value(pilot, "intro", "cold", "pending_over_48h") == 1
    assert value(pilot, "intro", "warm", "shift1_rate") == 2 / 3
    assert value(pilot, "intro", "cold", "shift1_rate") == 1.0
    assert value(pilot, "intro", "warm", "no_show_rate") == 0.25
    assert value(pilot, "intro", "cold", "no_show_rate") == 0.0


def test_repeat_rate_counts_within_the_slice(pilot):
    # v1 has two completed shifts at the warm org and one at the cold org
    assert value(pilot, "intro", "warm", "shift2_rate") == 0.5
    assert value(pilot, "intro", "cold", "shift2_rate") == 0.0
    assert value(pilot, "overall", None, "shift2_rate") == 0.5


def test_by_cause_and_heard_from(pilot):
    assert value(pilot, "cause", "Food insecurity", "accept_rate") == 0.75
    assert value(pilot, "cause", "Environment & parks", "accept_rate") == 1.0
    assert value(pilot, "heard_from", "personal", "like_rate") == 1.0  # v1, v2: 4 of 4
    assert value(pilot, "heard_from", "public", "like_rate") == 1 / 3  # v3, v5: 1 of 3


def test_soft_deleted_volunteers_count_and_inactive_causes_have_no_row(pilot):
    assert "Homelessness services" not in pilot["cause"]
    assert pilot["overall"]["like_rate"]["d"] == 9  # v5's pass is in the 9


def test_zero_denominators_are_none(pilot):
    shelters = pilot["cause"]["Animal shelters"]
    for metric in ("like_rate", "accept_rate", "median_reply_h", "shift1_rate",
                   "shift2_rate", "no_show_rate"):  # fmt: skip
        assert shelters[metric]["value"] is None, metric
    assert shelters["pending_over_48h"]["value"] == 0
    assert pilot["heard_from"]["unknown"]["like_rate"]["value"] is None


def test_an_empty_database_gives_none_everywhere():
    m = pilot_metrics(data.metrics_rows(), NOW)
    assert all(m["overall"][k]["value"] in (None, 0) for k in METRICS)


# --- the pages -------------------------------------------------------------------------


def admin_client(email="boss@example.org"):
    data.add_admin(email, "Boss Person")
    c = TestClient(app)
    c.post("/org/login/send", data={"email": email})
    _, _, body = notify.get_emailer().sent[-1]
    token = re.search(r"token=([\w-]+)", body).group(1)
    r = c.post("/org/login/verify", data={"token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    return c


def test_admin_signs_in_and_sees_the_pages(pilot):
    c = admin_client()
    assert c.get("/admin").status_code == 200
    page = c.get("/admin/metrics")
    assert page.status_code == 200
    assert "75%" in page.text and "3/4" in page.text  # the rate and its counts
    assert "—" in page.text
    assert c.get("/org", follow_redirects=False).status_code == 403
    assert c.get("/deck", follow_redirects=False).status_code == 403


@pytest.mark.parametrize("path", ["/admin", "/admin/metrics", "/admin/metrics.csv"])
def test_others_cannot_read_the_admin_pages(path, pilot):
    anon = TestClient(app)
    assert anon.get(path, follow_redirects=False).headers["location"] == "/org/login"
    vol = TestClient(app)
    sign_in(vol)
    profiles.update(id=data.get_or_create_volunteer(vol.phone), onboarded_at=T0)
    assert vol.get(path, follow_redirects=False).status_code == 403
    _, email = coordinator_for(0)
    coord = TestClient(app)
    org_sign_in(coord, email)
    r = coord.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/org"


def test_an_admin_address_cannot_also_be_a_coordinator():
    _, email = coordinator_for(0)
    with pytest.raises(ValueError):
        data.add_admin(email, "Dup")


def test_the_csv_matches_the_page_numbers(pilot):
    c = admin_client()
    r = c.get("/admin/metrics.csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == [
        "breakdown",
        "group",
        "metric",
        "numerator",
        "denominator",
        "value",
    ]
    by_key = {(x[0], x[1], x[2]): x for x in rows[1:]}
    warm = by_key[("intro", "warm", "accept_rate")]
    assert warm[3:] == ["3", "4", "0.75"]
    # every cell the module computes is in the file, with the same figure
    expected = list(csv_rows(pilot_metrics(data.metrics_rows(), datetime.now(UTC))))
    assert len(rows) == len(expected)
    # and the page shows the same counts as the file
    page = c.get("/admin/metrics").text
    for key in (("overall", "overall", "like_rate"), ("intro", "warm", "accept_rate")):
        assert f"{by_key[key][3]}/{by_key[key][4]}" in page


def test_no_personal_data_on_the_page_or_in_the_file(pilot):
    c = admin_client("private.boss@example.org")
    _, coord_email = coordinator_for(0)
    secrets = [
        *(p.display_name for p in profiles() if p.display_name),
        *(p.phone for p in profiles() if p.phone),
        "private.boss@example.org",
        coord_email,
    ]
    for path in ("/admin", "/admin/metrics", "/admin/metrics.csv"):
        text = c.get(path).text
        for s in secrets:
            if path == "/admin" and s == "Boss Person":
                continue
            assert s not in text, (path, s)


# --- the stats script ------------------------------------------------------------------


def test_stats_script_prints_parseable_lines(pilot):
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "stats.py"), "--db", str(DB_PATH)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    got = dict(line.split(": ", 1) for line in out.stdout.splitlines())
    assert int(got["open_openings"]) == len(openings(where="status = 'open'"))
    assert int(got["completed_shifts"]) == 4
    assert float(got["accept_rate"]) == round(5 / 6, 4)
    assert set(METRICS) <= set(got)
