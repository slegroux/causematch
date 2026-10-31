"""My hours: a volunteer's confirmed hours by month, from their own attended shifts."""

import json
import re
import zlib

import pytest
from conftest import coordinator, pantry_opening, volunteer
from starlette.testclient import TestClient

from app import data, security, signing
from app.certificate import build_pdf, pdf_text, qr_matrix
from app.config import public_base_url
from app.db import certificates, matches, openings, orgs, profiles, shifts
from main import app


def _shift(
    match,
    day,
    minutes,
    by,
    status="completed",
    month=10,
    marked_at="2026-10-30T10:00:00",
    **more,
):
    shifts.insert(
        match_id=match.id,
        starts_at=f"2026-{month:02d}-{day:02d}T09:00",
        status=status,
        kind="added",
        created_by=by,
        confirmed_minutes=minutes,
        marked_by=by,
        marked_at=marked_at,
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
    profiles.update(id=v.uid, last_name="Lee")  # a certificate carries the full name
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
    # a stored snapshot that no longer matches its signature is not shown, nor offered as PDF
    bad = {**snap, "minutes": 999}
    certificates.update(id=cert.id, snapshot=json.dumps(bad, sort_keys=True))
    assert a.get(f"/hours/certificate/{cert.id}").status_code == 404
    assert a.get(f"/hours/certificate/{cert.id}.pdf").status_code == 404


def _rename(a, first, last="Lee"):
    data.save_profile_details(
        a.uid,
        first_name=first,
        last_name=last,
        pronouns="",
        bio="",
        interests=[],
    )


def test_one_persons_hours_are_certified_under_one_name_only(anon):
    a, m, org = _hired("Zelda")
    _shift(m, 3, 180, org.uid, marked_at="2000-01-01T00:00:00+00:00")
    org_id = orgs()[0].id
    first = _certify(a, org_id, "all")  # as Zelda Lee
    for new_name in ("Zed", "Zelda Q"):
        _rename(a, new_name)
        r = a.post(
            f"/hours/orgs/{org_id}/certificate",
            data={"period": "all"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"] == "/hours?notice=renamed"
    assert "before you changed your name" in a.get("/hours?notice=renamed").text
    assert [c.withdrawn_at for c in certificates()] == [None]  # still the one
    assert "Zelda Lee" in a.get(first).text
    # hours confirmed after the change are theirs under the new name
    _shift(m, 10, 150, org.uid, month=11, marked_at="2999-01-01T00:00:00+00:00")
    new = _certify(a, org_id, "2026-11").rsplit("/", 1)[1]
    assert data.check_certificate(new)["volunteer"] == "Zelda Q Lee"


def test_changing_a_name_before_any_confirmed_hours_is_fine():
    a, m, org = _hired("Zelda")
    _rename(a, "Zed")
    _shift(m, 3, 180, org.uid, marked_at="2999-01-01T00:00:00+00:00")
    _certify(a, orgs()[0].id, "all")
    assert data.check_certificate(certificates()[0].id)["volunteer"] == "Zed Lee"


def test_certificate_needs_a_last_name():
    a, m, org = _hired("Zelda")
    profiles.update(id=a.uid, last_name=None)
    _shift(m, 3, 180, org.uid)
    r = a.post(
        f"/hours/orgs/{orgs()[0].id}/certificate",
        data={"period": "all"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/hours?notice=name" and certificates.count == 0
    assert "last name" in a.get("/hours?notice=name").text


def test_a_new_certificate_for_the_same_period_replaces_the_earlier_one(anon):
    a, cid, _ = _issued()
    (m,) = matches()
    _shift(m, 12, 60, coordinator(0).uid)
    new = _certify(a, orgs()[0].id, "all").rsplit("/", 1)[1]
    assert new != cid
    assert "This certificate was withdrawn." in anon.get(f"/check/{cid}").text
    assert "Genuine certificate" in anon.get(f"/check/{new}").text
    assert [c.id for c in certificates() if not c.withdrawn_at] == [new]
    assert a.get(f"/hours/certificate/{cid}").status_code == 404


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
    assert r.headers["referrer-policy"] == "no-referrer"

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


def test_check_link_says_changed_only_for_a_correction_to_its_own_shifts(anon):
    _a, cid, rows = _issued()
    later = matches[rows[0].match_id]
    _shift(later, 20, 240, rows[0].created_by)  # worked after issuing: not on it
    r = anon.get(f"/check/{cid}")
    assert "Genuine certificate" in r.text and "Hours have changed" not in r.text
    assert "9.5 hours" not in r.text

    (three_hours,) = (r for r in rows if r.confirmed_minutes == 180)
    shifts.update(id=three_hours.id, confirmed_minutes=120)  # a correction: 3 h to 2 h
    r = anon.get(f"/check/{cid}")
    assert "Hours have changed" in r.text and "corrected the hours" in r.text
    assert "5.5 hours" in r.text  # the total as issued, and no current total
    for now in ("4.5 hours", "8.5 hours", "6.5 hours", "confirmed now"):
        assert now not in r.text, now
    assert "Genuine certificate" not in r.text


def test_check_link_compares_each_shift_not_just_the_total(anon):
    _a, cid, rows = _issued()  # 3 h + 2.5 h
    three = next(r for r in rows if r.confirmed_minutes == 180)
    two_and_a_half = next(r for r in rows if r.confirmed_minutes == 150)
    shifts.update(id=three.id, confirmed_minutes=120)
    shifts.update(id=two_and_a_half.id, confirmed_minutes=210)  # 2 h + 3.5 h = 5.5 h
    assert "Hours have changed" in anon.get(f"/check/{cid}").text
    shifts.update(id=three.id, confirmed_minutes=180)
    shifts.update(id=two_and_a_half.id, confirmed_minutes=150)
    assert "Genuine certificate" in anon.get(f"/check/{cid}").text


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


# --- the PDF ------------------------------------------------------------------------------


def _inflate(data):
    try:
        return zlib.decompress(data)
    except zlib.error:
        return data


def _pdf_content(raw):
    """The page content of a PDF made by app.certificate, inflated: its drawing and text
    operators, so a test can read what was put on the pages without a PDF library."""
    streams = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.DOTALL)
    return "\n".join(
        _inflate(s).decode("latin-1") for s in streams if b"BT" in _inflate(s)
    )


def _pdf_strings(raw):
    """Every string shown on the pages, as text: the page content writes 2-byte glyph ids
    of the embedded subset fonts, and each font's /ToUnicode map says which character a
    glyph id is."""
    objects = {
        int(i): body
        for i, body in re.findall(rb"(\d+) 0 obj\n(.*?)\nendobj", raw, re.DOTALL)
    }
    maps = {}
    for name, obj in re.findall(rb"/F(\d+) (\d+) 0 R", raw):
        cmap = objects[int(re.search(rb"/ToUnicode (\d+) 0 R", objects[int(obj)])[1])]
        table = _inflate(cmap.split(b"stream\n", 1)[-1]).decode("latin-1")
        maps[name.decode()] = {
            int(g, 16): chr(int(u, 16))
            for g, u in re.findall(r"<([0-9A-F]{4})> <([0-9A-F]{4})>", table)
        }
    out, font = [], None
    for tf, text in re.findall(
        r"/F(\d+) [\d.]+ Tf|\(((?:\\.|[^\\)])*)\) Tj", _pdf_content(raw)
    ):
        if tf:
            font = tf
            continue
        codes = re.sub(r"\\(.)", lambda m: m[1].replace("r", "\r"), text).encode(
            "latin-1"
        )
        out.append(
            "".join(
                maps[font][int.from_bytes(codes[i : i + 2], "big")]
                for i in range(0, len(codes), 2)
            )
        )
    return out


def _qr_drawn(content):
    """The dark modules drawn as filled squares, as rows of 0/1 (top row first)."""
    squares = {
        (float(x), float(y), float(w))
        for x, y, w in re.findall(r"([\d.]+) ([\d.]+) ([\d.]+) -?[\d.]+ re f", content)
    }
    side = min(w for _, _, w in squares)
    left, top = min(x for x, _, _ in squares), max(y for _, y, _ in squares)
    dark = {(round((top - y) / side), round((x - left) / side)) for x, y, _ in squares}
    size = max(max(r for r, _ in dark), max(c for _, c in dark)) + 1
    return [[int((r, c) in dark) for c in range(size)] for r in range(size)]


def test_certificate_pdf_is_the_owners_and_carries_the_check_link(anon):
    a, cid, _ = _issued()
    r = a.get(f"/hours/certificate/{cid}.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["cache-control"] == "private, no-store"
    assert re.fullmatch(
        r'attachment; filename="causematch-hours-[a-z0-9-]+-all\.pdf"',
        r.headers["content-disposition"],
    )
    assert "Download PDF" in a.get(f"/hours/certificate/{cid}").text

    check = f"{public_base_url()}/check/{cid}"
    content = _pdf_content(r.content)
    code = signing.code(certificates()[0].signature)
    text = _pdf_strings(r.content)
    assert "Zelda Lee" in text and "Total: 5.5 hours" in text
    assert f"Signature code: {code}" in text
    assert _qr_drawn(content) == qr_matrix(check)  # the QR encodes the check link
    assert f"/URI ({check})".encode() in r.content  # and the address is a link
    assert b"zelda@example.com" not in r.content and b"/Author" not in r.content

    assert volunteer("Bianca").get(f"/hours/certificate/{cid}.pdf").status_code == 404
    r = anon.get(f"/hours/certificate/{cid}.pdf", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def _snapshot(name, shifts=1):
    return {
        "volunteer": name,
        "org": {"name": "Riverside Food Bank", "ein": "123456789", "website": ""},
        "period": "all",
        "shifts": [
            {"id": i, "date": "2026-10-03", "minutes": 150, "role": "Sorting donations"}
            for i in range(shifts)
        ],
        "minutes": 150 * shifts,
        "coordinators": ["Pat Lee"],
        "issued": "2026-10-31",
    }


def test_certificate_pdf_downloads_are_limited_per_user():
    a, cid, _ = _issued()
    path = f"/hours/certificate/{cid}.pdf"
    capacity = security.BUCKETS["pdf"][0]
    assert all(a.get(path).status_code == 200 for _ in range(capacity))
    r = a.get(path)
    assert r.status_code == 429 and "retry-after" in r.headers
    assert (
        a.get(f"/hours/certificate/{cid}").status_code == 200
    )  # the page is not limited


def test_pdf_text_shows_only_letters_marks_numbers_punctuation_symbols_and_spaces():
    for hidden in (
        "\u202e",  # right-to-left override
        "\u2066",
        "\u2069",
        "\u200b",
        "\u200f",
        "\u2028",
        "\u2029",
        "\x00",
        "\n",
        "\ufeff",
    ):
        assert pdf_text(f"a{hidden}b") == "a?b", hex(ord(hidden))
    assert pdf_text("José O'Neil-Núñez, Jr. 3rd") == "José O'Neil-Núñez, Jr. 3rd"
    assert pdf_text("李") == "?"


def test_certificate_pdf_builder_runs_on_its_own_for_long_lists_and_any_name():
    check = "http://localhost:5001/check/abc"
    many = build_pdf(_snapshot("Sam Rivera", 60), "abc", check)
    assert re.search(rb"/Count ([2-9])", many)  # more than one page, header repeated
    content = _pdf_content(many)
    text = _pdf_strings(many)
    assert text.count("Date") > 1 and "Total: 150 hours" in text
    assert _qr_drawn(content) == qr_matrix(check)
    for name in ("a" * 300, "x\ny\x00", "", "\U0001f600", "\u0e01\u0301"):
        assert build_pdf(_snapshot(name), "abc", check).startswith(b"%PDF")
    # characters the font has come out as stored; the ones it lacks as "?", one each
    for name, shown in (
        ("Nguyễn Thị Ánh", "Nguyễn Thị Ánh"),
        ("José Muñoz", "José Muñoz"),
        ("Иван Σωκράτης", "Иван Σωκράτης"),
        ("李雷", "??"),
        ("Ann 안", "Ann ?"),
    ):
        assert shown in _pdf_strings(build_pdf(_snapshot(name), "abc", check))
