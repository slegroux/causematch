"""Pilot metrics: hand-computed rates, the admin pages, the CSV and the stats script."""

import csv
import hashlib
import io
import subprocess
import sys
from datetime import UTC, datetime

import apsw
import pytest
from conftest import ROOT, admin_client, coordinator_for, org_sign_in, sign_in, uid_of
from starlette.testclient import TestClient

from app import data, metrics
from app.db import (
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

# Copies of the database are written through the app's own connection (VACUUM INTO) and
# opened with apsw, the app's SQLite: a second SQLite library in this process (sqlite3)
# shares the file's POSIX locks with it and can read stale pages, or corrupt the file.
from app.db import db as app_db
from app.metrics import METRICS, OTHER, csv_rows, fmt, pilot_metrics
from app.routes import admin as admin_routes
from main import app

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)  # 05:00 in Los Angeles
T0 = "2026-10-01T00:00:00+00:00"


def at(hours):
    h = int(hours)
    return f"2026-10-01T{h:02d}:{int((hours - h) * 60):02d}:00+00:00"


def build_pilot():
    """Five volunteers, a warm org's opening (food) and a cold org's (parks).

    Warm opening A: v0-v3 like, v4 passes. v0, v1, v2 accepted (replies 2, 6, 4 h), v3
    declined (10 h). Shifts: v0 completed twice (and a cancelled third), v1 completed and
    one past shift never marked, v2 no-show.
    Cold opening B: v0, v1, v3 like, v2 passes. v0 accepted (1 h), v3 accepted (3 h, shift
    still ahead), v1 pending for 12 days. v0 completes a shift.
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
                # v4 left; their one pass still counts
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
    shift(m_a[1], "2026-10-13T09:00", "scheduled")  # past, never marked
    shift(m_a[0], "2026-10-14T09:00", "cancelled")  # ignored everywhere
    for vi in (0, 1, 3):
        swipe(vi, b, True)
    swipe(2, b, False)
    m_b1 = match(0, b, "accepted", replied=1)
    match(1, b, "pending", created="2026-10-13T10:00:00+00:00")
    m_b4 = match(3, b, "accepted", replied=3)
    shift(m_b1, "2026-10-06T09:00", "completed")
    shift(m_b4, "2026-11-20T09:00", "scheduled")


@pytest.fixture
def pilot(monkeypatch):
    """The fixture's metrics with the small-group merging off (it has five volunteers)."""
    monkeypatch.setattr(metrics, "MIN_GROUP_VOLUNTEERS", 1)
    build_pilot()
    return pilot_metrics(data.metrics_rows(), NOW)


@pytest.fixture
def big_n(monkeypatch):
    monkeypatch.setattr(metrics, "MIN_DENOM", 1)
    monkeypatch.setattr(admin_routes, "_now", lambda: NOW)


def value(m, kind, group, metric):
    cells = m["overall"] if kind == "overall" else m[kind][group]
    return cells[metric]["value"]


def counts_of(m, kind, group, metric):
    cells = m["overall"] if kind == "overall" else m[kind][group]
    return cells[metric]["n"], cells[metric]["d"]


def test_overall_figures_are_the_hand_computed_ones(pilot):
    assert value(pilot, "overall", None, "volunteers") == 5
    assert counts_of(pilot, "overall", None, "like_rate") == (7, 9)
    assert counts_of(pilot, "overall", None, "accept_rate") == (5, 6)
    assert counts_of(pilot, "overall", None, "replied_within_48h_rate") == (6, 7)
    assert value(pilot, "overall", None, "median_reply_h") == 3.5  # 1 2 3 4 6 10
    assert value(pilot, "overall", None, "pending_over_48h") == 1
    assert counts_of(pilot, "overall", None, "shift1_rate") == (
        3,
        4,
    )  # B v3's shift is ahead
    assert value(pilot, "overall", None, "unmarked_first_shifts") == 0
    assert value(pilot, "overall", None, "unmarked_past_shifts") == 1
    assert value(pilot, "overall", None, "withdrew_before_first_shift") == 0
    assert counts_of(pilot, "overall", None, "shift2_rate") == (1, 2)  # only v0 repeats
    assert counts_of(pilot, "overall", None, "shift2_same_org_rate") == (1, 2)
    assert counts_of(pilot, "overall", None, "eligible_for_repeat") == (2, 2)
    assert counts_of(pilot, "overall", None, "no_show_rate") == (1, 5)


def test_three_of_four_accepted_is_a_rate_of_075(pilot):
    assert value(pilot, "intro", "warm", "accept_rate") == 0.75


def test_warm_and_cold_split(pilot):
    assert value(pilot, "intro", "warm", "like_rate") == 4 / 5
    assert value(pilot, "intro", "cold", "like_rate") == 3 / 4
    assert value(pilot, "intro", "warm", "replied_within_48h_rate") == 1.0
    assert value(pilot, "intro", "cold", "replied_within_48h_rate") == 2 / 3
    assert value(pilot, "intro", "warm", "median_reply_h") == 5.0  # 2 4 6 10
    assert value(pilot, "intro", "cold", "median_reply_h") == 2.0  # 1 3
    assert value(pilot, "intro", "warm", "pending_over_48h") == 0
    assert value(pilot, "intro", "cold", "pending_over_48h") == 1
    assert value(pilot, "intro", "warm", "shift1_rate") == 2 / 3
    assert value(pilot, "intro", "cold", "shift1_rate") == 1.0
    assert value(pilot, "intro", "warm", "no_show_rate") == 0.25
    assert value(pilot, "intro", "cold", "no_show_rate") == 0.0
    assert value(pilot, "intro", "warm", "unmarked_past_shifts") == 1


def test_repeat_places_a_volunteer_by_their_first_completed_shift(pilot):
    # both volunteers first completed at the warm org, so warm = overall and cold is empty
    assert counts_of(pilot, "intro", "warm", "shift2_rate") == (1, 2)
    assert counts_of(pilot, "intro", "cold", "shift2_rate") == (0, 0)
    assert value(pilot, "intro", "cold", "shift2_rate") is None
    assert counts_of(pilot, "cause", "food-insecurity", "shift2_rate") == (1, 2)
    assert counts_of(pilot, "heard_from", "personal", "shift2_rate") == (1, 2)


def test_recent_first_shifts_are_not_judged_for_repeat():
    row = {"causes": [], "intro": "warm", "heard_from": "", "org_id": 1, "match_id": 1}
    shift = lambda vid, day: {
        **row, "volunteer_id": vid, "starts_at": f"2026-10-{day}T09:00", "status": "completed",
    }  # fmt: skip
    rows = {"causes": [], "swipes": [], "matches": [],
            "shifts": [shift(1, "05"), shift(1, "06"), shift(2, "24")]}  # fmt: skip
    o = pilot_metrics(rows, NOW)["overall"]
    assert (o["eligible_for_repeat"]["n"], o["eligible_for_repeat"]["d"]) == (1, 2)
    assert (o["shift2_rate"]["n"], o["shift2_rate"]["d"]) == (1, 1)


def test_same_org_repeat_groups_by_org_not_opening():
    def done(org, day):
        return {"org_id": org, "starts_at": f"2026-10-{day}T09:00"}

    two_matches = [{"status": "accepted", "volunteer_id": 1}] * 2
    two_orgs = made(two_matches, [done(1, "05"), {**done(2, "06"), "match_id": 2}])[
        "overall"
    ]
    assert (two_orgs["shift2_rate"]["n"], two_orgs["shift2_rate"]["d"]) == (1, 1)
    assert (
        two_orgs["shift2_same_org_rate"]["n"],
        two_orgs["shift2_same_org_rate"]["d"],
    ) == (0, 1)
    one_org = made(two_matches, [done(1, "05"), {**done(1, "06"), "match_id": 2}])[
        "overall"
    ]
    assert (
        one_org["shift2_same_org_rate"]["n"],
        one_org["shift2_same_org_rate"]["d"],
    ) == (1, 1)
    for o in (two_orgs, one_org):
        assert o["shift2_same_org_rate"]["n"] <= o["shift2_rate"]["n"]


def test_by_cause_and_heard_from(pilot):
    assert value(pilot, "cause", "food-insecurity", "accept_rate") == 0.75
    assert value(pilot, "cause", "environment-parks", "accept_rate") == 1.0
    assert value(pilot, "heard_from", "personal", "like_rate") == 1.0  # v0, v1: 4 of 4
    assert value(pilot, "heard_from", "public", "like_rate") == 1 / 3  # v2, v4: 1 of 3
    assert pilot["labels"]["food-insecurity"] == "Food insecurity"


def test_soft_deleted_volunteers_count_and_inactive_causes_have_no_row(
    homelessness_off, pilot
):
    assert "homelessness-services" not in pilot["cause"]
    assert pilot["overall"]["like_rate"]["d"] == 9  # v4's pass is in the 9


def test_zero_denominators_are_none(pilot):
    shelters = pilot["cause"]["animal-shelters"]
    for metric in ("like_rate", "accept_rate", "median_reply_h", "shift1_rate",
                   "shift2_rate", "no_show_rate", "replied_within_48h_rate"):  # fmt: skip
        assert shelters[metric]["value"] is None, metric
    assert shelters["pending_over_48h"]["value"] == 0
    assert pilot["heard_from"]["unknown"]["like_rate"]["value"] is None


def test_an_org_without_an_intro_gets_an_unknown_row(pilot):
    orgs.update(id=orgs()[2].id, intro=None)
    m = pilot_metrics(data.metrics_rows(), NOW)
    assert value(m, "intro", "unknown", "like_rate") == 3 / 4
    assert value(m, "intro", "cold", "like_rate") is None


def test_an_empty_database_gives_none_everywhere():
    m = pilot_metrics(data.metrics_rows(), NOW)
    assert all(m["overall"][k]["value"] in (None, 0) for k in METRICS)


# --- edge cases on hand-made rows ------------------------------------------------------

TAGS = {"causes": [], "intro": "warm", "heard_from": "personal", "org_id": 1}


def made(matches_=(), shifts_=()):
    ms = [
        {**TAGS, "id": i, "volunteer_id": i, "created_at": T0, "decided_at": T0,
         "withdrawn_at": None, **m}
        for i, m in enumerate(matches_, 1)
    ]  # fmt: skip
    ss = [
        {**TAGS, "match_id": 1, "volunteer_id": 1, "starts_at": "2026-10-05T09:00",
         "status": "completed", **s}
        for s in shifts_
    ]  # fmt: skip
    return pilot_metrics({"causes": [], "swipes": [], "matches": ms, "shifts": ss}, NOW)


def test_an_unmarked_first_shift_is_in_neither_count_but_is_reported():
    o = made([{"status": "accepted"}], [{"status": "scheduled"}])["overall"]
    assert (o["shift1_rate"]["n"], o["shift1_rate"]["d"]) == (0, 0)
    assert o["unmarked_first_shifts"]["value"] == 1
    assert o["unmarked_past_shifts"]["value"] == 1


def test_accepted_matches_with_no_shift_are_counted_apart():
    o = made(
        [{"status": "accepted"}, {"status": "withdrawn", "decided_at": T0}, {"status": "accepted"}],
        [{"match_id": 3}],
    )["overall"]  # fmt: skip
    assert o["accepted_no_shift"]["value"] == 2  # matches 1 and 2
    assert o["withdrew_before_first_shift"]["value"] == 1  # match 2 only
    assert (o["shift1_rate"]["n"], o["shift1_rate"]["d"]) == (1, 2)


def test_the_first_shift_is_the_earliest_not_cancelled_one():
    o = made(
        [{"status": "accepted"}],
        [{"starts_at": "2026-10-04T09:00", "status": "cancelled"},
         {"starts_at": "2026-10-05T09:00", "status": "no_show"},
         {"starts_at": "2026-10-06T09:00", "status": "completed"}],
    )["overall"]  # fmt: skip
    assert (o["shift1_rate"]["n"], o["shift1_rate"]["d"]) == (0, 1)
    # a no-show then a completed shift is one completed shift: not a repeat
    assert (o["shift2_rate"]["n"], o["shift2_rate"]["d"]) == (0, 1)


def test_accepted_then_withdrawn_before_the_first_shift_counts_as_not_showing_up():
    o = made(
        [{"status": "accepted"}, {"status": "withdrawn", "decided_at": T0}],
        [{}, {"match_id": 2, "volunteer_id": 2, "status": "cancelled"}],
    )["overall"]  # fmt: skip
    assert (o["accept_rate"]["n"], o["accept_rate"]["d"]) == (2, 2)
    assert (o["shift1_rate"]["n"], o["shift1_rate"]["d"]) == (1, 2)
    assert o["withdrew_before_first_shift"]["value"] == 1


def test_accepted_then_withdrawn_after_a_completed_first_shift_keeps_the_success():
    o = made([{"status": "withdrawn", "decided_at": T0}], [{}])["overall"]
    assert (o["shift1_rate"]["n"], o["shift1_rate"]["d"]) == (1, 1)


def test_a_withdrawn_pending_match_is_not_an_accept():
    o = made([{"status": "withdrawn", "decided_at": None}])["overall"]
    assert o["accept_rate"]["value"] is None


# --- small numbers and small groups ----------------------------------------------------


def test_small_denominators_read_too_few(pilot):
    cell = pilot["intro"]["warm"]["accept_rate"]
    assert fmt("accept_rate", cell) == "too few (n<10)"
    assert (
        fmt("median_reply_h", pilot["intro"]["warm"]["median_reply_h"])
        == "too few (n<10)"
    )


def test_a_row_of_few_volunteers_is_merged_and_the_remainder_is_folded_in():
    build_pilot()
    m = pilot_metrics(data.metrics_rows(), NOW)
    # cold has 4 volunteers; merged alone it is still under 5, so warm folds in as well
    assert "cold" not in m["intro"] and "warm" not in m["intro"]
    assert m["intro"][OTHER]["volunteers"]["n"] == 5
    assert "personal" not in m["heard_from"] and "public" not in m["heard_from"]
    assert m["heard_from"][OTHER]["volunteers"]["n"] == 5
    assert m["overall"]["volunteers"]["n"] == 5  # overall is never suppressed


@pytest.mark.parametrize("kind", ["intro", "heard_from"])
def test_overall_minus_the_shown_rows_does_not_rebuild_a_hidden_group(kind):
    build_pilot()
    m = pilot_metrics(data.metrics_rows(), NOW)
    cold_like, warm_like = (3, 4), (4, 5)
    for metric in ("like_rate", "accept_rate", "no_show_rate"):
        left = m["overall"][metric]["n"] - sum(g[metric]["n"] for g in m[kind].values())
        # the residual is zero: the shown rows already cover everything
        assert left == 0, (kind, metric, left)
    shown = [(g["like_rate"]["n"], g["like_rate"]["d"]) for g in m[kind].values()]
    assert cold_like not in shown and warm_like not in shown


def test_a_hidden_rate_cannot_be_subtracted_from_its_shown_neighbour(monkeypatch):
    """warm has 20 swipes, cold only 6: cold reads "too few", so warm is withheld too."""
    monkeypatch.setattr(metrics, "MIN_GROUP_VOLUNTEERS", 1)
    tags = {"causes": [], "heard_from": "personal"}
    swipes_ = [
        {**tags, "intro": "warm", "volunteer_id": i % 10, "liked": i % 2 == 0}
        for i in range(20)
    ] + [
        {**tags, "intro": "cold", "volunteer_id": 20 + i % 5, "liked": i < 2}
        for i in range(6)
    ]
    m = pilot_metrics(
        {"causes": [], "swipes": swipes_, "matches": [], "shifts": []}, NOW
    )
    cells = {k: m["intro"][k]["like_rate"] for k in ("warm", "cold")}
    assert fmt("like_rate", cells["cold"]) == "too few (n<10)"
    assert fmt("like_rate", cells["warm"]) == "withheld"
    assert fmt("like_rate", m["overall"]["like_rate"]) == "46%"  # 12 of 26, still shown
    cols = {r[1]: r for r in csv_rows(m) if r[0] == "intro" and r[2] == "like_rate"}
    assert cols["warm"][3:] == (20, "", 20, "") and cols["cold"][4] == ""


def test_csv_cells_are_not_formulas(pilot):
    pilot["labels"]["food-insecurity"] = '=HYPERLINK("http://x")'
    cells = [r[1] for r in csv_rows(pilot) if len(r) > 1 and r[0] == "cause"]
    assert '\'=HYPERLINK("http://x")' in cells
    assert not any(c.startswith("=") for c in cells)


# --- the pages -------------------------------------------------------------------------


def test_admin_signs_in_and_sees_the_pages(pilot, big_n):
    c = admin_client()
    assert c.get("/admin").status_code == 200
    page = c.get("/admin/metrics")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "75%" in page.text and "3/4" in page.text  # the rate and its counts
    assert "—" in page.text
    assert "Definitions" in page.text and "Median hours from like" in page.text
    assert c.get("/org", follow_redirects=False).status_code == 403
    assert c.get("/deck", follow_redirects=False).status_code == 403


def test_the_page_explains_withheld(pilot):
    assert "Withheld" in admin_client().get("/admin/metrics").text


def test_the_page_says_too_few_for_small_denominators(pilot):
    page = admin_client().get("/admin/metrics")
    assert "too few (n&lt;10)" in page.text or "too few (n<10)" in page.text


def test_admin_sign_out_goes_to_the_staff_login(pilot):
    r = admin_client().post("/logout", follow_redirects=False)
    assert r.headers["location"] == "/org/login"


@pytest.mark.parametrize("path", ["/admin", "/admin/metrics", "/admin/metrics.csv"])
def test_others_cannot_read_the_admin_pages(path, pilot):
    anon = TestClient(app)
    assert anon.get(path, follow_redirects=False).headers["location"] == "/org/login"
    vol = TestClient(app)
    sign_in(vol)
    profiles.update(id=uid_of(vol), onboarded_at=T0)
    assert vol.get(path, follow_redirects=False).status_code == 403
    _, email = coordinator_for(0)
    coord = TestClient(app)
    org_sign_in(coord, email)
    r = coord.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/org"


def test_one_address_one_role():
    _, email = coordinator_for(0)
    with pytest.raises(ValueError):
        data.add_admin(email, "Dup")
    data.add_admin("root@example.org", "Root")
    with pytest.raises(ValueError):
        data.add_coordinator("root@example.org", orgs()[0].id, name="Dup")


def test_a_removed_admin_is_signed_out_at_once():
    c = admin_client("gone@example.org")
    assert c.get("/admin", follow_redirects=False).status_code == 200
    data.remove_admin("gone@example.org")
    r = c.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/org/login"
    with pytest.raises(LookupError):
        data.remove_admin("gone@example.org")
    assert data.admin_by_email("gone@example.org") is None


def test_the_csv_hides_small_rates_and_keeps_the_sample_size(pilot):
    rows = list(csv.reader(io.StringIO(admin_client().get("/admin/metrics.csv").text)))
    head = rows.index(
        ["breakdown", "group", "metric", "n", "numerator", "denominator", "value"]
    )
    assert rows[0][0].startswith("# volunteers:")
    warm = {r[2]: r for r in rows[head + 1 :] if r[:2] == ["intro", "warm"]}
    assert warm["accept_rate"][3:] == ["4", "", "4", ""]  # n kept, figure withheld
    assert warm["median_reply_h"][3] == "4" and warm["median_reply_h"][6] == ""


def test_the_csv_matches_the_page_numbers(pilot, big_n):
    c = admin_client()
    r = c.get("/admin/metrics.csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    head = rows.index(
        ["breakdown", "group", "metric", "n", "numerator", "denominator", "value"]
    )
    body = rows[head + 1 :]
    by_key = {(x[0], x[1], x[2]): x for x in body}
    assert by_key[("intro", "warm", "accept_rate")][3:] == ["4", "3", "4", "0.75"]
    expected = list(csv_rows(pilot_metrics(data.metrics_rows(), NOW)))
    assert len(rows) == len(expected)
    page = c.get("/admin/metrics").text
    for _, _, metric, n, num, den, val in body:
        if metric in metrics.RATES and val != "":
            assert f"{num}/{den}" in page, (metric, num, den)
            assert f"{round(float(val) * 100)}%" in page


def test_no_personal_data_on_the_page_or_in_the_file(pilot, big_n):
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


def run_stats(path):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "stats.py"), "--db", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_stats_script_prints_parseable_lines_and_leaves_the_file_alone(pilot, tmp_path):
    copy = tmp_path / "copy.db"
    app_db.execute("VACUUM INTO ?", [str(copy)])
    before = hashlib.sha256(copy.read_bytes()).hexdigest()
    out = run_stats(copy)
    assert out.returncode == 0, out.stderr
    got = dict(line.split(": ", 1) for line in out.stdout.splitlines())
    assert int(got["open_openings"]) == len(openings(where="status = 'open'"))
    assert int(got["completed_shifts"]) == 4
    assert int(got["completed_pairs"]) == 3  # v0 at two orgs, v1 at one
    assert float(got["accept_rate"]) == round(5 / 6, 4)
    assert set(METRICS) <= set(got)
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == before


def test_stats_script_will_not_create_or_migrate_a_database(tmp_path):
    assert run_stats(tmp_path / "missing.db").returncode != 0
    other = tmp_path / "other.db"
    c = apsw.Connection(str(other))
    c.execute("CREATE TABLE t (x)")
    c.close()
    before = other.read_bytes()
    assert run_stats(other).returncode != 0
    assert other.read_bytes() == before


# --- last-minute calls -----------------------------------------------------------------

FILL_MINUTES = [10, 20, 30, 40, 50, 60, 70, 80]  # median 45


def build_urgent(urgent_vols=6, normal_vols=6):
    """12 urgent calls: 8 filled after FILL_MINUTES, 3 ended unfilled, 1 open. Shifts:
    urgent fills by `urgent_vols` volunteers, 12 marked (3 no-shows); other shifts by
    `normal_vols`, 10 marked (1 no-show); a scheduled and a cancelled one of each, ignored.
    """
    from app.db import urgent_calls

    o = openings(where="status = 'open'")[0]
    ids = []
    for i, minutes in enumerate(FILL_MINUTES + [None] * 4):
        status = "filled" if minutes else "open" if i == 11 else "closed"
        ended = None
        if status != "open":
            m = 60 + (minutes or 90)  # posted at 01:00
            ended = f"2026-10-01T{m // 60:02d}:{m % 60:02d}:00+00:00"
        ids.append(
            urgent_calls.insert(
                opening_id=o.id, org_id=o.org_id, starts_at="2026-10-02T09:00",
                needed=2, auto_accept=False, status=status, created_by=0,
                created_at=at(1), ended_at=ended,
            ).id
        )  # fmt: skip

    def people(n, tag):
        return [
            profiles.insert(
                Profile(role="volunteer", phone=f"+1213777{tag}{i:03d}", created_at=T0)
            ).id
            for i in range(n)
        ]

    def shifts_for(vols, marked, no_shows, call):
        mids = [
            matches.insert(
                Match(opening_id=o.id, volunteer_id=vid, status="accepted",
                      created_at=T0, decided_at=at(2), urgent_call_id=call)
            ).id
            for vid in vols
        ]  # fmt: skip
        for k in range(marked + 2):
            status = (
                "no_show" if k < no_shows
                else "completed" if k < marked
                else "scheduled" if k == marked
                else "cancelled"
            )  # fmt: skip
            shifts.insert(
                Shift(match_id=mids[k % len(mids)], starts_at=f"2026-10-{2 + k:02d}T09:00",
                      status=status, created_at=T0)
            )  # fmt: skip

    shifts_for(people(urgent_vols, 1), 12, 3, ids[0])
    shifts_for(people(normal_vols, 2), 10, 1, None)


def test_urgent_figures_are_the_hand_computed_ones():
    build_urgent()
    u = pilot_metrics(data.metrics_rows(), NOW)["urgent"]
    assert u["urgent_posted"]["value"] == 12
    assert (u["urgent_filled_rate"]["n"], u["urgent_filled_rate"]["d"]) == (8, 11)
    assert u["urgent_median_fill_min"]["value"] == pytest.approx(45)
    assert u["urgent_median_fill_min"]["n"] == 8
    assert (u["urgent_no_show_rate"]["n"], u["urgent_no_show_rate"]["d"]) == (3, 12)
    assert (u["normal_no_show_rate"]["n"], u["normal_no_show_rate"]["d"]) == (1, 10)
    shown = {k: metrics.fmt(k, c) for k, c in u.items()}
    assert shown == {
        "urgent_posted": "12",
        "urgent_filled_rate": "73%",
        "urgent_median_fill_min": "too few (n<10)",  # 8 filled calls
        "urgent_no_show_rate": "25%",
        "normal_no_show_rate": "10%",
    }


def test_urgent_median_shows_once_enough_calls_filled(monkeypatch):
    monkeypatch.setattr(metrics, "MIN_DENOM", 8)
    build_urgent()
    u = pilot_metrics(data.metrics_rows(), NOW)["urgent"]
    assert metrics.fmt("urgent_median_fill_min", u["urgent_median_fill_min"]) == "45"


def test_urgent_no_show_of_a_small_group_is_withheld_with_its_complement():
    """3 volunteers behind the urgent fills: both no-show rates withheld, since overall's
    no-show rate minus the other one would give it back."""
    build_urgent(urgent_vols=3)
    m = pilot_metrics(data.metrics_rows(), NOW)
    u = m["urgent"]
    for k in ("urgent_no_show_rate", "normal_no_show_rate"):
        assert metrics.fmt(k, u[k]) == "withheld"
        assert metrics.counts(k, u[k]).startswith("n=")
    assert metrics.fmt("no_show_rate", m["overall"]["no_show_rate"]) == "18%"  # 4/22


def test_urgent_none_yet_reads_as_dashes():
    u = pilot_metrics(data.metrics_rows(), NOW)["urgent"]
    assert u["urgent_posted"]["value"] == 0
    assert {metrics.fmt(k, c) for k, c in u.items() if k != "urgent_posted"} == {"—"}


def test_urgent_on_the_admin_page_csv_and_stats(tmp_path):
    build_urgent()
    c = admin_client()
    page = c.get("/admin/metrics").text
    assert "Last-minute calls" in page and "Median minutes to fill" in page
    assert "73%" in page and "8/11" in page and "3/12" in page
    assert "Calls that filled / calls that ended" in page
    rows = list(csv.reader(io.StringIO(c.get("/admin/metrics.csv").text)))
    urgent = {r[2]: r for r in rows if r and r[0] == "urgent"}
    assert urgent["urgent_filled_rate"][3:] == ["11", "8", "11", "0.7273"]
    assert urgent["urgent_median_fill_min"][4:] == ["", "", ""]  # too few: withheld
    copy = tmp_path / "copy.db"
    app_db.execute("VACUUM INTO ?", [str(copy)])
    out = run_stats(copy)
    assert out.returncode == 0, out.stderr
    got = dict(line.split(": ", 1) for line in out.stdout.splitlines())
    assert got["urgent_posted"] == "12" and got["urgent_filled_rate"] == "0.7273"
    # a database from before urgent calls: no table, no column
    dst = apsw.Connection(str(copy))
    dst.execute("DROP TABLE urgent_call")
    dst.execute("ALTER TABLE match DROP COLUMN urgent_call_id")
    dst.close()
    out = run_stats(copy)
    assert out.returncode == 0, out.stderr
    got = dict(line.split(": ", 1) for line in out.stdout.splitlines())
    assert got["urgent_posted"] == "0" and got["urgent_filled_rate"] == ""
