"""My hours: a volunteer's confirmed hours by month, from their own attended shifts."""

import json
import re

import pytest
from conftest import coordinator, pantry_opening, volunteer

from app import data, signing
from app.db import certificates, openings, orgs, profiles, shifts


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
