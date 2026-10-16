"""A coordinator claims their org: request, admin approval, magic link; coordinator claims org."""

import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import (
    FIXTURES,
    admin_client,
    coordinator_for,
    org_sign_in,
    sign_in,
    uid_of,
)
from starlette.testclient import TestClient

from app import data, notify, propublica
from app.db import claim_requests, org_members, orgs, profiles
from main import app

EIN = "861620575"
FORM = {
    "ein": "86-1620575",
    "org_name": "Los Angeles Food Bank",
    "name": "Test Claimant",
    "email": "Claimant@FoodBank.example",
    "phone": "(310) 555-0123",
    "authorized": "1",
}
RECEIVED = "We'll review your request"


def imported(ein=EIN, name="Los Angeles Food Bank"):
    data.upsert_imported_org(
        ein, name, "Los Angeles", "90019", "K31", "food-insecurity", 34.05, -118.34
    )
    return orgs(where="ein = ?", where_args=[ein])[0]


def claim(client=None, **over):
    client = client or TestClient(app)
    return client.post("/org/claim", data={**FORM, **over}, follow_redirects=False)


@pytest.fixture
def admin():
    return admin_client()


def approve(admin, claim_id, **form):
    return admin.post(
        f"/admin/claims/{claim_id}/approve", data=form, follow_redirects=False
    )


def the_claim():
    return claim_requests()[0]


def test_the_claim_page_is_public_and_asks_for_the_right_things():
    r = TestClient(app).get("/org/claim")
    assert r.status_code == 200
    for name in ("ein", "org_name", "name", "email", "phone", "authorized"):
        assert f'name="{name}"' in r.text
    assert "authorized to represent this organization" in r.text


def test_claimed_org_reaches_the_dashboard_through_admin_approval(admin):
    org = imported()
    r = claim()
    assert r.status_code == 200 and RECEIVED in r.text
    c = the_claim()
    assert (c.status, c.email, c.ein, c.phone) == (
        "pending",
        "claimant@foodbank.example",
        EIN,
        "+13105550123",
    )
    assert not org_members(
        where="org_id = ?", where_args=[org.id]
    )  # submitting grants nothing
    assert not orgs[org.id].verified_at
    assert not any(to == c.email for to, _, _ in notify.get_emailer().sent)

    page = admin.get("/admin/claims").text
    for shown in (
        "Test Claimant",
        "claimant@foodbank.example",
        "Los Angeles Food Bank",
        "Los Angeles",
        "NTEE K31",
        "not verified",
    ):
        assert shown in page
    assert "Already claimed" not in page

    r = approve(admin, c.id)
    assert r.status_code == 303 and r.headers["location"] == "/admin/claims"
    c = the_claim()
    assert (c.status, c.org_id, c.decided_by) == (
        "approved",
        org.id,
        data.admin_by_email("boss@example.org"),
    )
    assert orgs[org.id].verified_at
    (member,) = org_members(where="org_id = ?", where_args=[org.id])
    assert member.role == "owner"

    to, subject, body = notify.get_emailer().sent[-1]
    assert to == "claimant@foodbank.example" and "approved" in subject
    assert body.startswith(
        "Your request to manage Los Angeles Food Bank on Causematch was approved."
    )
    assert "https://causematch.test/org/login" in body
    assert "token=" not in body and "ignore" not in body  # nothing to expire or ignore
    claimant = TestClient(app)
    org_sign_in(
        claimant, "claimant@foodbank.example"
    )  # asks for a link, as the email says
    home = claimant.get("/org")
    assert home.status_code == 200 and "Los Angeles Food Bank" in home.text
    assert profiles[member.user_id].display_name == "Test Claimant"
    assert profiles[member.user_id].contact_phone == "+13105550123"
    assert (
        the_claim().status == "approved"
        and "No pending" in admin.get("/admin/claims").text
    )


def test_approval_keeps_an_earlier_verification(admin):
    org = imported()
    orgs.update(id=org.id, verified_at="2026-01-01T00:00:00+00:00")
    claim()
    approve(admin, the_claim().id)
    assert orgs[org.id].verified_at == "2026-01-01T00:00:00+00:00"


def test_a_rejection_is_recorded_and_grants_nothing(admin):
    org = imported()
    claim()
    sent = len(notify.get_emailer().sent)
    r = admin.post(f"/admin/claims/{the_claim().id}/reject", follow_redirects=False)
    assert r.status_code == 303
    c = the_claim()
    assert c.status == "rejected" and c.decided_at and c.decided_by and not c.org_id
    assert not org_members(where="org_id = ?", where_args=[org.id])
    assert data.coordinator_by_email(c.email) is None
    assert not orgs[org.id].verified_at
    # one short, neutral notice: no reason, none of what they typed
    assert len(notify.get_emailer().sent) == sent + 1
    to, _, body = notify.get_emailer().sent[-1]
    assert to == c.email and "was not approved" in body
    assert "Los Angeles Food Bank" not in body and "Claimant" not in body
    # asking for a link as that address sends nothing
    TestClient(app).post("/org/login/send", data={"email": c.email})
    assert len(notify.get_emailer().sent) == sent + 1
    # a decided request cannot be approved afterwards
    approve(admin, c.id)
    assert the_claim().status == "rejected" and not data.coordinator_by_email(c.email)


def test_the_answer_does_not_reveal_which_eins_exist_or_are_claimed(admin):
    imported()
    owned = imported("530196605", "Some Claimed Org")
    data.add_coordinator("owner@example.org", owned.id, "owner", name="Olive Owner")
    answers = {
        "known": claim(),
        "unknown": claim(ein="12-3456789", email="x1@example.org"),
        "claimed": claim(ein="53-0196605", email="x2@example.org"),
    }
    texts = {k: (r.status_code, r.text) for k, r in answers.items()}
    assert len({v for v in texts.values()}) == 1, "the three answers differ"
    assert RECEIVED in answers["known"].text
    assert "Olive" not in answers["claimed"].text
    assert claim_requests.count == 3  # all three are queued for an admin to look at


def test_a_repeated_pending_request_is_one_row():
    imported()
    first, second = claim(), claim()
    assert first.text == second.text and claim_requests.count == 1
    claim(email="other@example.org")
    assert claim_requests.count == 2


def test_an_org_with_an_owner_needs_the_admins_confirmation(admin):
    org = imported()
    data.add_coordinator("olive@example.org", org.id, "owner", name="Olive Owner")
    claim()
    cid = the_claim().id
    page = admin.get("/admin/claims").text
    assert "Already claimed by Olive Owner (owner)" in page
    r = approve(admin, cid)  # no confirmation
    assert r.status_code == 200 and "already claimed by Olive Owner (owner)" in r.text
    assert the_claim().status == "pending"
    assert len(org_members(where="org_id = ?", where_args=[org.id])) == 1
    assert not data.coordinator_by_email("claimant@foodbank.example")

    r = approve(admin, cid, confirm="1")
    assert r.status_code == 303 and the_claim().status == "approved"
    roles = {
        m.user_id: m.role for m in org_members(where="org_id = ?", where_args=[org.id])
    }
    assert sorted(roles.values()) == ["coordinator", "owner"]
    assert roles[data.coordinator_by_email("olive@example.org")] == "owner"


def test_a_deleted_owner_does_not_count_as_claiming(admin):
    org = imported()
    uid = data.add_coordinator("gone@example.org", org.id, "owner", name="Gone Owner")
    profiles.update(id=uid, deleted_at="2026-01-01T00:00:00+00:00")
    claim()
    assert "Already claimed" not in admin.get("/admin/claims").text
    assert approve(admin, the_claim().id).status_code == 303


def test_an_ein_no_org_has_cannot_be_approved(admin):
    claim(ein="12-3456789")
    cid = the_claim().id
    page = admin.get("/admin/claims").text
    assert (
        "No org here has EIN 123456789" in page
        and f"/admin/claims/{cid}/approve" not in page
    )
    r = approve(admin, cid)
    assert r.status_code == 200 and "no org has that EIN" in r.text
    assert the_claim().status == "pending" and not data.coordinator_by_email(
        the_claim().email
    )
    assert not org_members.count


def test_a_deactivated_org_cannot_be_claimed(admin):
    org = imported()
    orgs.update(id=org.id, deactivated_at="2026-01-01T00:00:00+00:00")
    claim()
    assert "DEACTIVATED" in admin.get("/admin/claims").text
    r = approve(admin, the_claim().id)
    assert r.status_code == 200 and "deactivated" in r.text and not org_members.count


def test_an_existing_coordinator_is_linked_not_duplicated(admin):
    imported()
    _, email = coordinator_for(0, "claimant@foodbank.example")
    before = profiles.count
    claim(name="A Different Name")
    approve(admin, the_claim().id)
    assert profiles.count == before
    uid = data.coordinator_by_email(email)
    assert profiles[uid].display_name == "Pat Doe, coordinator"
    assert len(org_members(where="user_id = ?", where_args=[uid])) == 2


def test_an_admins_address_cannot_be_claimed_into_a_coordinator(admin):
    imported()
    claim(email="boss@example.org")
    r = approve(admin, the_claim().id)
    assert r.status_code == 200 and "that address is an admin" in r.text
    assert the_claim().status == "pending" and not org_members.count


def test_a_failing_email_does_not_undo_the_approval(admin, monkeypatch):
    imported()
    claim()

    def boom(*a):
        raise RuntimeError("mail down")

    monkeypatch.setattr(notify, "send_claim_approved", boom)
    r = approve(admin, the_claim().id)
    assert r.status_code == 303 and the_claim().status == "approved"
    # the claimant can still ask for a link themselves
    monkeypatch.undo()
    TestClient(app).post("/org/login/send", data={"email": the_claim().email})
    assert notify.get_emailer().sent[-1][0] == "claimant@foodbank.example"


def test_deciding_twice_does_nothing_the_second_time(admin):
    imported()
    claim()
    cid = the_claim().id
    approve(admin, cid)
    sent = len(notify.get_emailer().sent)
    r = approve(admin, cid)
    assert r.status_code == 303 and len(notify.get_emailer().sent) == sent
    admin.post(f"/admin/claims/{cid}/reject")
    assert the_claim().status == "approved"


# --- who may decide -------------------------------------------------------------------------


def outsiders():
    vol = TestClient(app)
    sign_in(vol)
    profiles.update(id=uid_of(vol), onboarded_at="2026-01-01T00:00:00")
    _, email = coordinator_for(0)
    coord = TestClient(app)
    org_sign_in(coord, email)
    return {"volunteer": vol, "coordinator": coord, "signed out": TestClient(app)}


@pytest.mark.parametrize("who", ["volunteer", "coordinator", "signed out"])
def test_nobody_but_an_admin_reaches_the_claims(who):
    imported()
    claim()
    cid = the_claim().id
    caller = outsiders()[who]
    members = org_members.count
    sent = len(notify.get_emailer().sent)
    for method, path in (
        ("GET", "/admin/claims"),
        ("POST", f"/admin/claims/{cid}/approve"),
        ("POST", f"/admin/claims/{cid}/reject"),
    ):
        r = caller.request(
            method,
            path,
            data={"confirm": "1"} if method == "POST" else None,
            follow_redirects=False,
        )
        assert r.status_code == 403 or (
            r.status_code in (302, 303)
            and r.headers["location"] in ("/org", "/org/login", "/login")
        ), (who, path, r.status_code)
        assert "Claimant" not in r.text
    assert the_claim().status == "pending" and org_members.count == members
    assert len(notify.get_emailer().sent) == sent


def test_a_coordinator_cannot_add_themselves_to_another_org():
    # the claim page is the only way in and it needs an admin: a signed-in coordinator who
    # claims another org's EIN changes nothing until an admin approves
    other = imported("530196605", "Other Org")
    coord = outsiders()["coordinator"]
    claim(coord, ein="53-0196605", email="coord0@example.org")
    assert not org_members(where="org_id = ?", where_args=[other.id])
    assert the_claim().status == "pending"


# --- the form -----------------------------------------------------------------------------


def test_mass_assignment_of_status_or_ids_is_ignored():
    org = imported()
    r = claim(
        status="approved",
        org_id=str(org.id),
        id="999",
        decided_by="1",
        decided_at="2026-01-01",
        verified_at="2026-01-01",
        role="admin",
    )
    assert RECEIVED in r.text
    c = the_claim()
    assert (c.status, c.org_id, c.decided_by, c.decided_at) == (
        "pending",
        None,
        None,
        None,
    )
    assert c.id != 999
    assert not org_members.count and not orgs[org.id].verified_at


@pytest.mark.parametrize(
    "over,message",
    [
        ({"authorized": ""}, "authorized to represent"),
        ({"ein": "12345"}, "nine digits"),
        ({"ein": ""}, "nine digits"),
        ({"org_name": " "}, "organization's name"),
        ({"org_name": "x" * 201}, "organization's name"),
        ({"name": ""}, "Enter your name"),
        ({"name": "n" * 101}, "Enter your name"),
        ({"email": "not-an-email"}, "email address"),
        ({"email": "a@b.co" + "x" * 260}, "email address"),
        ({"phone": "123"}, "US mobile number"),
    ],
)
def test_a_bad_form_asks_again_and_stores_nothing(over, message):
    r = claim(**over)
    assert r.status_code == 200 and message in r.text and RECEIVED not in r.text
    assert claim_requests.count == 0


def test_a_checkbox_with_another_value_is_not_consent():
    assert claim(authorized="on").status_code == 200 and claim_requests.count == 0


def test_non_text_form_parts_are_a_bad_request():
    r = TestClient(app).post(
        "/org/claim",
        files={"name": ("x.txt", b"x")},
        data={k: v for k, v in FORM.items() if k != "name"},
        follow_redirects=False,
    )
    assert r.status_code == 400 and claim_requests.count == 0


def test_the_phone_is_optional():
    claim(phone="")
    assert the_claim().phone == ""


def test_html_in_a_request_is_escaped_on_the_admin_page(admin):
    imported()
    claim(name="<script>alert(1)</script>", org_name="<img src=x onerror=1>")
    page = admin.get("/admin/claims").text
    assert "<script>alert(1)</script>" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page


def test_no_inline_event_handlers_on_the_claim_pages(admin):
    imported()
    claim()
    for client, path in ((TestClient(app), "/org/claim"), (admin, "/admin/claims")):
        html = client.get(path).text
        assert not re.search(r"\son[a-z]+\s*=", html), path
        assert "javascript:" not in html, path


# --- who a claim confirms and lists -------------------------------------------------------


def test_an_org_run_by_coordinators_only_still_needs_the_confirmation(admin):
    """B1: scripts/add_coordinator.py defaults to the coordinator role."""
    org = imported()
    data.add_coordinator("rita@example.org", org.id, name="Rita Run")  # coordinator
    claim(email="mallory@example.org")
    cid = the_claim().id
    assert (
        "Already claimed by Rita Run (coordinator)" in admin.get("/admin/claims").text
    )
    r = approve(admin, cid)
    assert (
        r.status_code == 200 and "already claimed by Rita Run (coordinator)" in r.text
    )
    assert the_claim().status == "pending" and not data.coordinator_by_email(
        "mallory@example.org"
    )
    assert approve(admin, cid, confirm="1").status_code == 303
    roles = {
        profiles[m.user_id].email: m.role
        for m in org_members(where="org_id = ?", where_args=[org.id])
    }
    # nobody owned it, so the approved claimant is its owner; Rita is not demoted
    assert roles == {"rita@example.org": "coordinator", "mallory@example.org": "owner"}


def test_the_claim_row_lists_everyone_who_can_sign_in(admin):
    org = imported()
    data.add_coordinator("a@example.org", org.id, "owner", name="Ann Owner")
    data.add_coordinator("b@example.org", org.id, name="Bo Coord")
    gone = data.add_coordinator("c@example.org", org.id, name="Cy Gone")
    profiles.update(id=gone, deleted_at="2026-01-01T00:00:00+00:00")
    claim()
    page = admin.get("/admin/claims").text
    assert "Ann Owner (owner), Bo Coord (coordinator)" in page and "Cy Gone" not in page


def test_a_suspended_coordinators_address_is_refused(admin):
    imported()
    uid, email = coordinator_for(0, "sus@example.org")
    profiles.update(id=uid, suspended_at="2026-01-01T00:00:00+00:00")
    claim(email=email)
    r = approve(admin, the_claim().id)
    assert r.status_code == 200 and "suspended coordinator" in r.text
    assert the_claim().status == "pending"


# import the EIN a claim names


def serve_org(request):
    ein = request.url.path.rsplit("/", 1)[1].removesuffix(".json")
    if ein == EIN:
        return httpx.Response(200, json=ORG_FIXTURE)
    return httpx.Response(404, json={"error": "Organization not found"})


ORG_FIXTURE = json.loads((FIXTURES / "organization_861620575.json").read_text())


def propublica_says(monkeypatch, handler):
    monkeypatch.setattr(
        propublica,
        "make_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=10),
    )


def test_a_claim_for_an_org_not_yet_here_can_import_it_then_be_approved(
    admin, monkeypatch
):
    propublica_says(monkeypatch, serve_org)
    claim()
    cid = the_claim().id
    page = admin.get("/admin/claims").text
    assert (
        f"/admin/claims/{cid}/import" in page
        and f"/admin/claims/{cid}/approve" not in page
    )
    r = admin.post(f"/admin/claims/{cid}/import", follow_redirects=False)
    assert r.status_code == 303
    org = orgs(where="ein = ?", where_args=[EIN])[0]
    assert (org.name, org.zip, org.intro) == ("Los Angeles Food Bank", "90019", "cold")
    assert not org.verified_at and the_claim().status == "pending"
    page = admin.get("/admin/claims").text
    assert (
        f"/admin/claims/{cid}/import" not in page
        and f"/admin/claims/{cid}/approve" in page
    )
    assert approve(admin, cid).status_code == 303 and orgs[org.id].verified_at


@pytest.mark.parametrize(
    "handler,message",
    [
        (lambda r: httpx.Response(404, json={}), "no nonprofit with that EIN"),
        (lambda r: httpx.Response(500), "did not answer"),
        (lambda r: httpx.Response(200, json={"organization": {"name": "X", "zipcode": "94103", "ntee_code": "K31"}}), "not a Los Angeles County ZIP"),
        (lambda r: httpx.Response(200, json={"organization": {"name": "X", "zipcode": "90019", "ntee_code": "K03"}}), "maps to no cause"),
    ],
)  # fmt: skip
def test_an_import_that_cannot_happen_says_why_and_changes_nothing(
    admin, monkeypatch, handler, message
):
    propublica_says(monkeypatch, handler)
    claim()
    before = orgs.count
    r = admin.post(f"/admin/claims/{the_claim().id}/import", follow_redirects=False)
    assert r.status_code == 200 and "Not imported" in r.text and message in r.text
    assert orgs.count == before and the_claim().status == "pending"


def test_only_an_admin_can_import_and_nothing_is_fetched(monkeypatch):
    def boom(request):
        raise AssertionError("no request expected")

    propublica_says(monkeypatch, boom)
    claim()
    for who, caller in outsiders().items():
        r = caller.post(
            f"/admin/claims/{the_claim().id}/import", follow_redirects=False
        )
        assert r.status_code in (302, 303, 403), who
        assert r.headers.get("location") != "/admin/claims"
    assert not orgs(where="ein = ?", where_args=[EIN])


# how long a claim keeps its contact details


def decided(days_ago, status="approved"):
    when = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    claim_requests.update(id=the_claim().id, status=status, decided_at=when)


def test_decided_claims_lose_their_contact_details_after_90_days():
    imported()
    claim()
    cid = the_claim().id
    assert data.scrub_decided_claims() == 0  # pending: kept
    decided(89)
    assert data.scrub_decided_claims() == 0 and claim_requests[cid].email
    decided(91, "rejected")
    assert data.scrub_decided_claims() == 1
    row = claim_requests[cid]
    assert (row.name, row.email, row.phone) == (None, None, None)
    assert (row.ein, row.status) == (
        EIN,
        "rejected",
    )  # the record of the decision stays
    assert data.scrub_decided_claims() == 0


def test_the_hourly_sweep_scrubs_old_claims(monkeypatch):
    monkeypatch.setenv("TASKS_TOKEN", "t")
    imported()
    claim()
    decided(120)
    r = TestClient(app).post("/tasks/reminders", headers={"Authorization": "Bearer t"})
    assert r.status_code == 200 and claim_requests()[0].email is None


def test_removing_a_coordinator_scrubs_their_claims(admin):
    imported()
    _, email = coordinator_for(0, "claimant@foodbank.example")
    claim()  # pending, from the same address
    data.remove_coordinator(email)
    row = the_claim()
    assert (row.name, row.email, row.phone) == (None, None, None)
    claim(email="other@example.org")
    assert claim_requests.count == 2 and any(
        c.email == "other@example.org" for c in claim_requests()
    )


def test_the_privacy_page_says_what_a_claim_keeps():
    text = TestClient(app).get("/privacy").text.replace("&#x27;", "'")
    assert "asks to manage a nonprofit" in text and "90 days" in text


# the data layer checks the caller itself


def test_the_data_layer_refuses_a_caller_who_is_not_an_admin():
    imported()
    claim()
    cid = the_claim().id
    coord_uid, _ = coordinator_for(0)
    vol = data.get_or_create_volunteer("+13105550177")
    members = org_members.count
    sent = len(notify.get_emailer().sent)
    for uid in (coord_uid, vol, 10**6):
        for fn, args in (
            (data.admin_claims, ()),
            (data.approve_claim, (cid, True)),
            (data.reject_claim, (cid,)),
            (data.pending_claim_ein, (cid,)),
        ):
            with pytest.raises(PermissionError):
                fn(uid, *args)
    assert the_claim().status == "pending" and org_members.count == members
    assert len(notify.get_emailer().sent) == sent


@pytest.mark.parametrize(
    "name,kept",
    [
        ("Test Claimant", True),
        ("n" * 81, False),  # over the 80 a coordinator's name may hold
        ("Test\u202e Claimant", False),  # a bidi control
    ],
)
def test_an_older_claims_unfit_name_is_not_given_to_the_coordinator(admin, name, kept):
    """Claims from before names were checked: approve_claim creates the coordinator with
    no name when the stored one is unfit (the dashboard then asks for theirs)."""
    imported()
    claim()
    c = the_claim()
    claim_requests.update(id=c.id, name=name)  # as an older, unchecked claim held it
    assert approve(admin, c.id).status_code == 303
    p = data.get_profile(data.coordinator_by_email(c.email))
    assert (p.display_name or "") == (name if kept else "")
