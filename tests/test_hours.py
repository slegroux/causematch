"""My hours: a volunteer's confirmed hours by month, from their own attended shifts."""

import json
import re

import pytest
from conftest import coordinator, pantry_opening, volunteer
from starlette.testclient import TestClient

from app import data, security, signing
from app.db import certificates, openings, orgs, profiles, shifts
from main import app


def _shift(match, day, minutes, by, status="completed", month=10, **more):
    shifts.insert(
        match_id=match.id,
        starts_at=f"2026-{month:02d}-{day:02d}T09:00",
        status=status,
        kind="added",
        created_by=by,
        confirmed_minutes=minutes,
        marked_by=by,
        marked_at="2026-10-30T10:00:00",
        **more,
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
    # a September shift whose hours were corrected by someone else: a month of its own,
    # confirmed by the corrector, not the first marker
    _shift(mine, 5, 60, org.uid, month=9, corrected_by=b.uid)
    _shift(
        mine, 6, 60, org.uid, status="scheduled", month=9
    )  # not attended: not listed

    page = a.get("/hours").text
    assert "October 2026" in page and "5.5 hours" in page
    assert "Confirmed by Pat Doe, coordinator" in page
    assert "September 2026" in page and "Confirmed by Bianca" in page
    assert page.index("October 2026") < page.index("September 2026")  # newest first
    assert "1 earlier shift has no confirmed hours" in page
    assert "2 hours" not in page and "7.5 hours" not in page and "8 hours" not in page
    assert 'href="/hours"' in a.get("/history").text
    assert 'href="/hours"' in a.get("/profile").text
    assert "No confirmed hours yet" in volunteer("Nobody").get("/hours").text


# --- certificates -------------------------------------------------------------------------


def _hired(name, org_index=0):
    """A volunteer accepted by the nth seeded org, with its coordinator: (client, match, org)."""
    v, org = volunteer(name), coordinator(org_index)
    opening = openings(where="org_id = ?", where_args=[orgs()[org_index].id])[0]
    m = data.swipe(v.uid, opening.id, True)
    data.decide(org.uid, m.id, True)
    return v, m, org


def _certify(v, org_id, period):
    r = v.post(
        f"/hours/orgs/{org_id}/certificate",
        data={"period": period},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    return r.headers["location"]


def test_certificate_lists_only_the_hours_one_nonprofit_confirmed():
    a, mine, org = _hired("Zelda")
    profiles.update(id=a.uid, last_name="Lee")
    orgs.update(id=orgs()[0].id, ein="12-3456789", website="https://pantry.example.org")
    other = coordinator(1)
    theirs = data.swipe(
        a.uid, openings(where="org_id = ?", where_args=[orgs()[1].id])[0].id, True
    )
    data.decide(other.uid, theirs.id, True)
    _shift(mine, 3, 180, org.uid)
    _shift(mine, 10, 150, org.uid)
    _shift(mine, 11, None, org.uid, status="no_show")
    _shift(mine, 12, None, org.uid)  # attended, hours never confirmed
    _shift(theirs, 4, 120, other.uid)  # another nonprofit's hours

    assert "Get a certificate" in a.get("/hours").text
    where = _certify(a, orgs()[0].id, "2026-10")
    page = a.get(where).text
    assert "Zelda Lee" in page and "5.5 hours" in page
    assert "Oct 3, 2026" in page and "3 hours" in page
    assert "Oct 10, 2026" in page and "2.5 hours" in page
    assert "Oct 4, 2026" not in page and "Oct 11, 2026" not in page
    assert "Oct 12, 2026" not in page and "2 hours" not in page.replace("2.5 hours", "")
    assert "EIN 12-3456789" in page and "https://pantry.example.org" in page
    assert "Pat Doe, coordinator" in page and "Signature code" in page
    cert = certificates()[0]
    assert re.search(r"[0-9A-F]{4}(-[0-9A-F]{4}){3}", page)
    assert cert.signature[:4].upper() in page and cert.minutes == 330

    assert _certify(a, orgs()[0].id, "all") != where  # a period of its own
    assert _certify(a, orgs()[0].id, "2026-10") == where  # same content: the same one
    assert certificates.count == 2
    # no confirmed hours at that nonprofit in the period: no certificate
    for org_id, period in ((orgs()[0].id, "2026-09"), (orgs()[2].id, "all")):
        r = a.post(
            f"/hours/orgs/{org_id}/certificate",
            data={"period": period},
            follow_redirects=False,
        )
        assert r.status_code == 404
    assert certificates.count == 2


def test_certificate_is_the_owners_to_see(anon):
    a, m, org = _hired("Zelda")
    _shift(m, 3, 180, org.uid)
    where = _certify(a, orgs()[0].id, "all")
    assert a.get(where).status_code == 200
    assert volunteer("Bianca").get(where).status_code == 404
    assert a.get("/hours/certificate/no-such-code").status_code == 404
    r = anon.get(where, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    # the daily limit counts what was issued: new hours make a new certificate each time
    with pytest.raises(data.CertificateLimit):
        for day in range(1, 1 + data.CERTIFICATES_PER_DAY):
            _shift(m, day, 30, org.uid, month=8)
            data.issue_certificate(a.uid, orgs()[0].id, "all")
    assert certificates.count == data.CERTIFICATES_PER_DAY


def test_deleting_the_account_withdraws_and_scrubs_its_certificates():
    a, m, org = _hired("Zelda")
    _shift(m, 3, 180, org.uid)
    _certify(a, orgs()[0].id, "all")
    assert "Zelda" in certificates()[0].snapshot
    data.delete_account(a.uid)
    (cert,) = certificates()
    assert cert.withdrawn_at and cert.snapshot is None and cert.signature is None
    with pytest.raises(LookupError):
        data.my_certificate(a.uid, cert.id)


def test_certificate_signature_fails_when_the_snapshot_is_changed():
    a, m, org = _hired("Zelda")
    _shift(m, 3, 180, org.uid)
    _certify(a, orgs()[0].id, "all")
    cert = certificates()[0]
    snap = json.loads(cert.snapshot)
    assert signing.verify(snap, cert.id, cert.signature)
    for change in (
        lambda s: s.update(minutes=s["minutes"] + 60),
        lambda s: s["shifts"][0].update(minutes=240),
        lambda s: s.update(volunteer="Zelda Leigh"),
    ):
        bad = json.loads(cert.snapshot)
        change(bad)
        assert not signing.verify(bad, cert.id, cert.signature)
    assert not signing.verify(snap, "another-id", cert.signature)
    assert not signing.verify(snap, cert.id, "")
    assert data.my_certificate(a.uid, cert.id)["genuine"]


# --- the check link -----------------------------------------------------------------------


def _issued(name="Zelda", **profile):
    """A volunteer with a 5.5 hour certificate at the first nonprofit: (client, id, shifts)."""
    a, m, org = _hired(name)
    profiles.update(id=a.uid, last_name="Lee", email="zelda@example.com", **profile)
    orgs.update(id=orgs()[0].id, ein="12-3456789")
    _shift(m, 3, 180, org.uid)
    _shift(m, 10, 150, org.uid)
    cert_id = _certify(a, orgs()[0].id, "all").rsplit("/", 1)[1]
    return a, cert_id, shifts(where="match_id = ?", where_args=[m.id])


def test_check_link_shows_what_the_certificate_says_and_no_more(anon):
    a, cid, _ = _issued()
    owner_page = a.get(f"/hours/certificate/{cid}").text
    assert f"/check/{cid}" in owner_page and "Anyone with this link" in owner_page

    r = anon.get(f"/check/{cid}")  # signed out
    assert r.status_code == 200
    assert "genuine Causematch certificate, unchanged since it was issued" in r.text
    assert "5.5 hours" in r.text and "Zelda Lee" in r.text and "12-3456789" in r.text
    assert re.search(r"[0-9A-F]{4}(-[0-9A-F]{4}){3}", r.text)
    assert certificates()[0].signature[:4].upper() in r.text
    assert (
        "Oct 3, 2026" not in r.text and "zelda@example.com" not in r.text
    )  # no shifts, no contact
    assert 'name="robots" content="noindex"' in r.text
    assert r.headers["x-robots-tag"].startswith("noindex")
    assert r.headers["cache-control"] == "no-store"

    # one changed character, other lengths and odd inputs: never an error, never a record
    flipped = cid[:-1] + ("A" if cid[-1] != "A" else "B")
    for odd in (
        flipped,
        cid[:-1],
        cid + "x",
        "%20",
        "a" * 5000,
        "%E2%98%83",
        "%27%20OR%201=1--",
        "%00",
    ):
        r = anon.get(f"/check/{odd}")
        assert r.status_code == 404 and "not a Causematch record" in r.text, odd
        assert "Zelda" not in r.text and "5.5 hours" not in r.text

    # a stored snapshot edited in the database no longer verifies
    snap = json.loads(certificates()[0].snapshot)
    snap["minutes"] += 60
    certificates.update(id=cid, snapshot=json.dumps(snap, sort_keys=True))
    r = anon.get(f"/check/{cid}")
    assert r.status_code == 404 and "not a Causematch record" in r.text
    assert "6.5 hours" not in r.text and "genuine" not in r.text


def test_check_link_says_when_the_nonprofit_changed_the_hours(anon):
    _a, cid, rows = _issued()
    (three_hours,) = (r for r in rows if r.confirmed_minutes == 180)
    shifts.update(id=three_hours.id, confirmed_minutes=120)  # a correction: 3 h to 2 h
    r = anon.get(f"/check/{cid}")
    assert "has since changed the hours it confirmed" in r.text
    assert "5.5 hours" in r.text and "4.5 hours" in r.text  # as issued, and now
    assert "unchanged since it was issued" not in r.text


def test_check_link_says_withdrawn_and_nothing_else(anon):
    a, cid, _ = _issued()
    b = volunteer("Bianca")
    assert b.get(f"/hours/certificate/{cid}/withdraw").status_code == 404
    assert b.post(f"/hours/certificate/{cid}/withdraw").status_code == 404
    assert "genuine" in anon.get(f"/check/{cid}").text  # not Bianca's to withdraw

    assert "Withdraw this certificate" in a.get(f"/hours/certificate/{cid}").text
    assert a.get(f"/hours/certificate/{cid}/withdraw").status_code == 200  # asks first
    assert "genuine" in anon.get(f"/check/{cid}").text  # a GET withdraws nothing
    for _ in range(2):  # twice: the same answer
        r = a.post(f"/hours/certificate/{cid}/withdraw", follow_redirects=False)
        assert r.status_code == 303
    cert = certificates()[0]
    assert cert.withdrawn_at and cert.snapshot is None and cert.signature is None

    r = anon.get(f"/check/{cid}")
    assert r.status_code == 200 and "This certificate was withdrawn." in r.text
    for text in (
        "Zelda",
        orgs()[0].name,
        "5.5 hours",
        "12-3456789",
        "genuine",
        "Total",
    ):
        assert text not in r.text, text
    assert a.get(f"/hours/certificate/{cid}").status_code == 404
    assert "Get a certificate" in a.get("/hours").text
    assert _certify(a, orgs()[0].id, "all").rsplit("/", 1)[1] != cid  # a new one

    # deleting the account withdraws the rest the same way
    other = anon.get(f"/check/{certificates(where='withdrawn_at IS NULL')[0].id}")
    assert "genuine" in other.text
    data.delete_account(a.uid)
    assert "was withdrawn" in anon.get(f"/check/{certificates()[-1].id}").text


def test_check_link_is_limited_per_address(monkeypatch):
    monkeypatch.setattr(security, "CHECK_PER_IP", (("check-ip", 3, 600),))
    visitor = TestClient(app, client=("198.51.100.40", 1))
    codes = [visitor.get("/check/nothing").status_code for _ in range(4)]
    assert codes == [404, 404, 404, 429]
