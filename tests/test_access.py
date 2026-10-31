"""Every route is classified, and checked for sign-in, role and ownership."""

import ast
import os
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import (
    PANTRY,
    ROOT,
    coordinator_for,
    jpeg,
    org_sign_in,
    pantry_opening,
    sign_in,
    volunteer,
)
from starlette.routing import Mount, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import data, notify
from app.auth import (
    PUBLIC,
    is_admin_path,
    is_org_path,
    session_cookie,
    session_secret,
    ws_session_user,
)
from app.db import (
    blocks,
    claim_requests,
    matches,
    messages,
    openings,
    org_members,
    orgs,
    profiles,
    reports,
    shifts,
    swipes,
    urgent_calls,
)
from main import app

NAME = "Zelda"  # volunteer A, whose data nobody else may see
# What nobody but the owner may see; the world fixture adds A's phone and org 0's coordinator.
SECRETS = [NAME, PANTRY]

# (method, path) -> (who may use it, how the route is scoped)
#   who: public | volunteer | coordinator | admin | signed-in (every role; the handler decides)
#   scope: None | own-record (the signed-in volunteer's) | own-org (the coordinator's org's)
ROUTES = {
    ("GET", "/login"): ("public", None),
    ("POST", "/login/send"): ("public", None),
    ("POST", "/login/verify"): ("public", None),
    ("POST", "/logout"): ("volunteer", None),  # any signed-in role may sign out
    ("GET", "/org/login"): ("public", None),
    ("POST", "/login/demo"): ("public", None),  # a demo's one tap; 403 elsewhere
    ("POST", "/org/login/demo"): ("public", None),  # the same
    ("POST", "/org/login/send"): ("public", None),
    ("GET", "/org/login/verify"): ("public", None),
    ("POST", "/org/login/verify"): ("public", None),
    ("GET", "/org/claim"): ("public", None),
    ("POST", "/org/claim"): ("public", None),
    ("GET", "/org/claim/search"): ("public", None),  # IRS records only, rate-limited
    ("GET", "/onboarding"): ("volunteer", "own-record"),
    ("POST", "/onboarding"): ("volunteer", "own-record"),
    ("GET", "/onboarding/more"): ("volunteer", "own-record"),
    ("POST", "/onboarding/more"): ("volunteer", "own-record"),
    ("GET", "/"): ("volunteer", None),
    ("POST", "/"): ("volunteer", None),
    ("GET", "/deck"): ("volunteer", "own-record"),
    ("POST", "/deck"): ("volunteer", "own-record"),
    ("POST", "/deck/filters"): ("volunteer", "own-record"),
    ("POST", "/deck/relax"): ("volunteer", "own-record"),
    ("POST", "/swipe"): ("volunteer", "own-record"),
    ("GET", "/matches"): ("volunteer", "own-record"),
    ("GET", "/matches/{match_id}/withdraw"): ("volunteer", "own-record"),
    ("POST", "/matches/{match_id}/withdraw"): ("volunteer", "own-record"),
    ("GET", "/matches/{match_id}/report"): ("volunteer", "own-record"),
    ("POST", "/matches/{match_id}/report"): ("volunteer", "own-record"),
    ("GET", "/matches/{match_id}/block"): ("volunteer", "own-record"),
    ("POST", "/matches/{match_id}/block"): ("volunteer", "own-record"),
    ("GET", "/profile"): ("volunteer", "own-record"),
    ("GET", "/me/preferences"): ("volunteer", "own-record"),
    ("POST", "/me/preferences"): ("volunteer", "own-record"),
    ("GET", "/profile/delete"): ("volunteer", "own-record"),
    ("POST", "/profile/delete"): ("volunteer", "own-record"),
    ("GET", "/privacy"): ("public", None),
    ("GET", "/org/matches/{match_id}/report"): ("coordinator", "own-org"),
    ("POST", "/org/matches/{match_id}/report"): ("coordinator", "own-org"),
    ("GET", "/org/matches/{match_id}/block"): ("coordinator", "own-org"),
    ("POST", "/org/matches/{match_id}/block"): ("coordinator", "own-org"),
    ("GET", "/org"): ("coordinator", "own-org"),
    ("POST", "/org/details"): ("coordinator", "own-org"),
    ("GET", "/org/openings/new"): ("coordinator", "own-org"),
    ("GET", "/org/zip"): ("coordinator", None),  # a public table lookup
    ("POST", "/org/openings/new"): ("coordinator", "own-org"),
    ("POST", "/org/openings/{opening_id}/close"): ("coordinator", "own-org"),
    ("GET", "/org/openings/{opening_id}/urgent"): ("coordinator", "own-org"),
    ("POST", "/org/openings/{opening_id}/urgent"): ("coordinator", "own-org"),
    ("POST", "/org/openings/{opening_id}/urgent/close"): ("coordinator", "own-org"),
    ("GET", "/org/inbox"): ("coordinator", "own-org"),
    ("POST", "/org/matches/{match_id}/decide"): ("coordinator", "own-org"),
    ("GET", "/org/shifts"): ("coordinator", "own-org"),
    ("POST", "/org/shifts/{shift_id}/mark"): ("coordinator", "own-org"),
    ("POST", "/org/shifts/{shift_id}/cancel"): ("coordinator", "own-org"),
    ("POST", "/org/matches/{match_id}/shifts/repeat"): ("coordinator", "own-org"),
    ("GET", "/org/matches/{match_id}/shifts/new"): ("coordinator", "own-org"),
    ("POST", "/org/matches/{match_id}/shifts/new"): ("coordinator", "own-org"),
    ("GET", "/admin"): ("admin", None),
    ("GET", "/admin/metrics"): ("admin", None),
    ("GET", "/admin/metrics.csv"): ("admin", None),
    ("GET", "/admin/reports"): ("admin", None),
    ("POST", "/admin/reports/{report_id}/dismiss"): ("admin", "own-record"),
    ("POST", "/admin/reports/{report_id}/suspend"): ("admin", "own-record"),
    ("POST", "/admin/reports/{report_id}/deactivate"): ("admin", "own-record"),
    ("GET", "/admin/blocks"): ("admin", None),
    ("POST", "/admin/blocks/{block_id}/unblock"): ("admin", "own-record"),
    ("GET", "/admin/claims"): ("admin", None),
    ("POST", "/admin/claims/{claim_id}/approve"): ("admin", "own-record"),
    ("POST", "/admin/claims/{claim_id}/reject"): ("admin", "own-record"),
    ("GET", "/admin/orgs"): ("admin", None),
    ("POST", "/admin/orgs/{org_id}/lookup"): ("admin", "own-record"),
    ("POST", "/admin/orgs/{org_id}/verify"): ("admin", "own-record"),
    ("GET", "/history"): ("volunteer", "own-record"),
    ("GET", "/hours"): ("volunteer", "own-record"),
    ("GET", "/shifts/{shift_id}.ics"): ("volunteer", "own-record"),
    ("GET", "/shifts/{shift_id}/cancel"): ("volunteer", "own-record"),
    ("POST", "/shifts/{shift_id}/cancel"): ("volunteer", "own-record"),
    ("GET", "/shifts/{shift_id}/confirm"): ("volunteer", "own-record"),
    ("POST", "/shifts/{shift_id}/confirm"): ("volunteer", "own-record"),
    ("GET", "/chat/{match_id}"): ("volunteer", "own-record"),
    ("POST", "/chat/{match_id}/send"): ("volunteer", "own-record"),
    ("WS", "/chat/{match_id}/ws"): ("volunteer", "own-record"),
    ("GET", "/org/chat/{match_id}"): ("coordinator", "own-org"),
    ("POST", "/org/chat/{match_id}/send"): ("coordinator", "own-org"),
    ("WS", "/org/chat/{match_id}/ws"): ("coordinator", "own-org"),
    ("POST", "/tasks/reminders"): (
        "public",
        None,
    ),  # the scheduler's bearer token guards it
    ("POST", "/tasks/urgent"): ("public", None),  # the same
    # an urgent email's unsubscribe link: the signed key is the credential
    ("GET", "/urgent/off/{key}"): ("public", "own-record"),
    ("POST", "/urgent/off/{key}"): ("public", "own-record"),
    ("GET", "/favicon.ico"): ("public", None),
    ("*", "/static"): ("public", None),  # the static mount: manifest, icons, scripts
    ("GET", "/me/profile"): ("volunteer", "own-record"),
    ("POST", "/me/profile/photo"): ("volunteer", "own-record"),
    ("POST", "/me/profile/photo/remove"): ("volunteer", "own-record"),
    ("POST", "/me/profile/details"): ("volunteer", "own-record"),
    ("POST", "/deck/photo-nudge/dismiss"): ("volunteer", "own-record"),
    ("POST", "/admin/reports/{report_id}/remove-photo"): ("admin", "own-record"),
    # data.photo_for decides per photo: a volunteer's own, a matched org's, an admin's
    ("GET", "/photos/{key}.jpg"): ("signed-in", "own-record"),
    ("GET", "/photos/thumb/{key}.jpg"): ("signed-in", "own-record"),
    ("GET", "/org/organization"): ("coordinator", "own-org"),
    ("POST", "/org/organization/{org_id}"): ("coordinator", "own-org"),
    ("POST", "/org/organization/{org_id}/logo"): ("coordinator", "own-org"),
    ("POST", "/org/organization/{org_id}/photo"): ("coordinator", "own-org"),
    ("GET", "/org/openings/{opening_id}/media"): ("coordinator", "own-org"),
    ("POST", "/org/openings/{opening_id}/photo"): ("coordinator", "own-org"),
    ("POST", "/org/openings/{opening_id}/link"): ("coordinator", "own-org"),
}
# Ids that arrive in a form body rather than the path: each needs an OWNERSHIP check too.
BODY_IDS = {
    ("POST", "/swipe"): {"opening_id", "call_id"},
    ("POST", "/org/openings/new"): {"org_id"},
}
PUBLIC_ROUTES = [r for r, (who, _) in ROUTES.items() if who == "public"]
GUARDED = [r for r, (who, _) in ROUTES.items() if who != "public"]
# Sign-out and photos work for every signed-in role, so they are not role-wrong cases.
ROLE_ROUTES = [
    r for r in GUARDED if r != ("POST", "/logout") and ROUTES[r][0] != "signed-in"
]


def app_routes(routes, prefix=""):
    """Every (method, path) the router serves, mounts included, found from the app itself."""
    for r in routes:
        path = prefix + r.path
        if isinstance(r, Mount) and getattr(r, "routes", None):
            yield from app_routes(r.routes, path)
        elif isinstance(r, WebSocketRoute):
            yield ("WS", path)
        elif isinstance(r, Mount) or not getattr(r, "methods", None):
            yield ("*", path)
        else:
            methods = set(r.methods)
            if "GET" in methods:  # Starlette adds HEAD to every GET
                methods.discard("HEAD")
            for method in sorted(methods):
                yield (method, path)


def concrete(path, match_id, opening_id):
    return path.format(
        match_id=match_id,
        opening_id=opening_id,
        shift_id=match_id,
        report_id=match_id,
        block_id=match_id,
        claim_id=match_id,
        org_id=match_id,
        key="A" * 22,
    )


def is_public_path(path):
    probe = path + "/x" if path == "/static" else path
    return any(re.fullmatch(p, probe) for p in PUBLIC)


def home_of(role):
    return {"coordinator": "/org", "admin": "/admin"}.get(role, "/deck")


def login_of(path):
    return "/org/login" if is_org_path(path) or is_admin_path(path) else "/login"


def send(client, method, path):
    """The route's own method, with a body shaped like what its form posts."""
    if method == "GET":
        return client.get(path, follow_redirects=False)
    body = {"opening_id": "1", "liked": "1", "decision": "accept", "first_name": "x"}
    return client.request(method, path, data=body, follow_redirects=False)


def ws_refused(client, path):
    """True when the socket is closed on the caller; False when the handler talks to them."""
    try:
        with client.websocket_connect(path) as ws:
            ws.send_text('{"msg": ""}')  # a handler that let us in answers, so no hang
            ws.receive_text()
    except WebSocketDisconnect:
        return True
    return False


@pytest.fixture
def world():
    """Volunteers A and B, an admin, and a coordinator for org 0 and one for org 1.

    A has liked an opening of org 0, so a pending match of org 0 exists.
    """

    w = SimpleNamespace()
    w.a, w.b, w.admin = volunteer(NAME), volunteer("Bianca"), volunteer("Root")
    profiles.update(id=w.admin.uid, role="admin")
    w.anon = TestClient(app)
    for i in (0, 1):
        uid, email = coordinator_for(i)
        setattr(w, f"org{i}_uid", uid)
        c = TestClient(app)
        org_sign_in(c, email)
        setattr(w, f"org{i}", c)
    w.opening = pantry_opening()
    w.other_opening = openings(where="org_id = ?", where_args=[orgs()[1].id])[0]
    assert w.opening.org_id == orgs()[0].id
    w.match = data.swipe(w.a.uid, w.opening.id, True)
    SECRETS[:] = [NAME, PANTRY, w.a.phone, "coord0@example.org"]
    return w


def no_secrets(r):
    assert not any(s in r.text for s in SECRETS), r.text[:300]


# --- the table covers the router, both ways -------------------------------------------


def test_every_route_has_an_access_classification():
    found = list(app_routes(app.routes))
    assert len(found) == len(set(found))
    assert set(found) - set(ROUTES) == set(), "route(s) added without a classification"
    assert set(ROUTES) - set(found) == set(), "classified route(s) that no longer exist"


def test_classification_is_well_formed():
    for (method, path), (who, scope) in ROUTES.items():
        assert who in ("public", "volunteer", "coordinator", "admin", "signed-in")
        assert scope in (None, "own-record", "own-org")
        if "{" in path:
            assert scope, f"{path} takes an id: say whose record it is"
        if is_org_path(path) and who != "public":
            assert who == "coordinator", path
        if is_admin_path(path):
            assert who == "admin", path
        assert not (method == "WS" and who == "public"), (
            "a websocket must check its caller"
        )
        if path in ("/", "/deck", "/matches", "/swipe", "/onboarding"):
            assert who == "volunteer", path


def test_public_means_the_beforeware_skips_it():
    for method, path in ROUTES:
        who = ROUTES[(method, path)][0]
        assert is_public_path(path) == (who == "public"), (method, path)


# --- signed out ------------------------------------------------------------------------


@pytest.mark.parametrize("route", GUARDED, ids=lambda r: " ".join(r))
def test_signed_out_is_sent_to_the_right_login(world, route):
    method, template = route
    path = concrete(template, world.match.id, world.opening.id)
    if method == "WS":
        assert ws_refused(world.anon, path)
        return
    r = send(world.anon, method, path)
    assert r.status_code in (401, 403) or (
        r.status_code in (302, 303) and r.headers["location"] == login_of(path)
    ), (r.status_code, r.headers.get("location"))
    no_secrets(r)
    assert matches[world.match.id].status == "pending"
    assert openings[world.opening.id].status == world.opening.status


def test_manifest_and_icons_are_served_signed_out(world):
    for path in (
        "/static/manifest.webmanifest",
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/apple-touch-icon.png",
        "/static/a2hs.js",
    ):
        assert world.anon.get(path, follow_redirects=False).status_code == 200, path


@pytest.mark.parametrize("route", PUBLIC_ROUTES, ids=lambda r: " ".join(r))
def test_public_routes_show_no_one_s_data(world, route):
    method, template = route
    if method == "*":
        return
    r = send(world.anon, method, template)
    assert r.status_code != 500
    no_secrets(r)


# --- wrong role ------------------------------------------------------------------------


def assert_turned_away(r, role, path):
    """403, or a redirect to the caller's own home; never the other side's page."""
    assert r.status_code == 403 or (
        r.status_code in (302, 303) and r.headers["location"] == home_of(role)
    ), (role, path, r.status_code, r.headers.get("location"))
    no_secrets(r)


@pytest.mark.parametrize("route", ROLE_ROUTES, ids=lambda r: " ".join(r))
def test_wrong_role_never_gets_the_other_sides_page(world, route):
    method, template = route
    who = ROUTES[route][0]
    path = concrete(template, world.match.id, world.opening.id)
    # Volunteer A's pending match belongs to A; B is a stranger on both sides.
    callers = {
        "volunteer": [("coordinator", world.org1), ("admin", world.admin)],
        "coordinator": [("volunteer", world.b), ("admin", world.admin)],
        "admin": [("volunteer", world.b), ("coordinator", world.org1)],
    }[who]
    for role, client in callers:
        if method == "WS":
            assert ws_refused(client, path), role
            continue
        r = send(client, method, path)
        assert_turned_away(r, role, path)
    assert matches[world.match.id].status == "pending"
    assert openings[world.opening.id].status == world.opening.status


def test_signed_in_volunteer_who_is_not_onboarded_cannot_use_the_deck(world):
    c = TestClient(app)
    sign_in(c)
    for path in ("/deck", "/matches", "/"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/onboarding"


# --- another user's or another org's id ------------------------------------------------

GONE = (2**63 - 1, 2**63, 2**70, 0, -1)


def assert_refused(r):
    assert r.status_code in (403, 404), (r.status_code, r.text[:200])
    no_secrets(r)


def path_id_check(call, record_id, intact):
    """Ownership check for a path id: a stranger's call, with the real id and bad ids."""

    def check(w):
        assert_refused(call(w, record_id(w)))
        assert intact(w)
        for gone in GONE:
            assert_refused(call(w, gone))
        assert intact(w)

    return check


def withdraw_page_other(w, mid):
    return w.b.get(f"/matches/{mid}/withdraw", follow_redirects=False)


def withdraw_other(w, mid):
    return w.b.post(f"/matches/{mid}/withdraw", follow_redirects=False)


def decide_other(w, mid):
    return w.org1.post(
        f"/org/matches/{mid}/decide",
        data={"decision": "accept"},
        follow_redirects=False,
    )


def close_other(w, oid):
    return w.org1.post(f"/org/openings/{oid}/close", follow_redirects=False)


LA = ZoneInfo("America/Los_Angeles")


def urgent_check(method, suffix):
    """org 1's coordinator, on org 0's opening: refused; no call posted, and org 0's own
    call (posted first) stays open."""

    def check(w):
        soon = (datetime.now(LA) + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M")
        body = {"starts_at": soon, "needed": "2", "auto_accept": "1"}
        r = w.org0.post(
            f"/org/openings/{w.opening.id}/urgent", data=body, follow_redirects=False
        )
        assert r.status_code == 303  # control: the owner may post one
        (call,) = urgent_calls()

        def call_as_org1(oid):
            path = f"/org/openings/{oid}/urgent{suffix}"
            if method == "GET":
                return w.org1.get(path, follow_redirects=False)
            return w.org1.post(path, data=body, follow_redirects=False)

        for oid in (w.opening.id, *GONE):
            assert_refused(call_as_org1(oid))
        assert [(c.id, c.status) for c in urgent_calls()] == [(call.id, "open")]

    return check


def urgent_off_check(method):
    """A key signed for B, with A's id in it, or a garbled one: refused, and A's
    last-minute calls stay on; B's own key works (the control)."""

    def check(w):
        profiles.update(id=w.a.uid, urgent_on=True)
        profiles.update(id=w.b.uid, urgent_on=True)
        b_key = data.urgent_off_key(w.b.uid)
        forged = f"{w.a.uid}-{b_key.split('-')[1]}"
        for key in (forged, f"{w.a.uid}-", "A" * 22, f"{w.a.uid}-{'0' * 32}", "-"):
            for who in (w.anon, w.b):
                r = (who.get if method == "GET" else who.post)(
                    f"/urgent/off/{key}", follow_redirects=False
                )
                assert r.status_code == 404, (key, r.status_code)
                no_secrets(r)
        assert profiles[w.a.uid].urgent_on
        r = (w.anon.get if method == "GET" else w.anon.post)(f"/urgent/off/{b_key}")
        assert r.status_code == 200
        assert bool(profiles[w.b.uid].urgent_on) == (method == "GET")

    return check


def opening_form(org_id, **over):
    f = {
        "org_id": str(org_id),
        "title": "Probe opening",
        "about": "Unload and shelve donated produce.",
        "cause": "food-insecurity",
        "zip": "90012",
        "neighborhood": "Chinatown",
        "starts_at": (datetime.now(LA) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M"),
        "recurrence": "weekly",
        "duration_minutes": "180",
        "slots": "5",
        "orientation_required": "no",
        "notes": "",
        "adult_only_ok": "1",
        "reply_channel": "email",
    }
    return {**f, **over}


def check_create_opening(w):
    """A coordinator names an org in the body; only their own org's id is accepted."""
    org0, org1 = orgs()[0].id, orgs()[1].id
    count = openings.count
    assert_refused(
        w.org1.post(
            "/org/openings/new", data=opening_form(org0), follow_redirects=False
        )
    )
    assert openings.count == count and not openings(where="title = 'Probe opening'")
    for gone in ("0", str(2**63), str(2**70), "999999999999999999", "-1", "x"):
        r = w.org1.post(
            "/org/openings/new",
            data=opening_form(gone),
            follow_redirects=False,
        )
        assert r.status_code in (200, 403, 404), (gone, r.status_code)
        no_secrets(r)
    assert openings.count == count
    # control: the same form for their own org goes through, so the refusals were about ownership
    r = w.org1.post(
        "/org/openings/new", data=opening_form(org1), follow_redirects=False
    )
    assert r.status_code == 303 and openings.count == count + 1
    assert openings(where="title = 'Probe opening'")[0].org_id == org1


def check_swipe(w):
    """A swipe names an opening; it must be one in the caller's deck, for the caller only."""
    b = w.b.uid

    def swipe(oid, **extra):
        return w.b.post(
            "/swipe", data={"opening_id": str(oid), "liked": "1", **extra},
            follow_redirects=False,
        )  # fmt: skip

    for gone in (0, -1, 2**63 - 1, 2**63, 2**70, "x", "1e3"):
        assert swipe(gone).status_code in (303, 404), gone
    data.close_opening(w.org0_uid, w.opening.id)  # closed since the card was drawn
    assert swipe(w.opening.id).status_code == 303
    assert not swipes(where="volunteer_id = ?", where_args=[b])
    assert [m.volunteer_id for m in matches()] == [w.a.uid]
    # a body cannot swipe for someone else
    openings.update(id=w.opening.id, status="open")
    swipe(w.opening.id, volunteer_id=str(w.a.uid))
    assert sorted(m.volunteer_id for m in matches()) == sorted([w.a.uid, b])


def accepted_match(w):
    """The world's match, accepted: a stranger is then refused for being a stranger."""
    data.decide(w.org0_uid, w.match.id, True)
    return w.match.id


def chat_id_check(stranger, owner, base, kind):
    """A stranger's call on an accepted match's chat, with the real id and bad ids."""

    def call(w, mid):
        c = getattr(w, stranger)
        if kind == "page":
            return c.get(f"{base}/{mid}", follow_redirects=False)
        return c.post(
            f"{base}/{mid}/send", data={"msg": "intrude"}, follow_redirects=False
        )

    def check(w):
        mid = accepted_match(w)
        for i in (mid, *GONE):
            if kind == "ws":
                assert ws_refused(getattr(w, stranger), f"{base}/{i}/ws"), i
            else:
                assert_refused(call(w, i))
        assert messages.count == 0
        # control: the owner is let in, so the refusals were about ownership
        o = getattr(w, owner)
        if kind == "ws":
            with o.websocket_connect(f"{base}/{mid}/ws") as ws:
                ws.send_text('{"msg": "hi"}')
                assert "hi" in ws.receive_text()  # the owner's socket really answers
            assert messages.count == 1
        elif kind == "page":
            assert o.get(f"{base}/{mid}").status_code == 200
        else:
            r = o.post(f"{base}/{mid}/send", data={"msg": "hi"}, follow_redirects=False)
            assert r.status_code == 303 and messages.count == 1

    return check


def shift_match_check(kind):
    """org1 is a stranger to org 0's accepted match: it can't see or add a shift to it."""

    def check(w):
        mid = accepted_match(w)
        path = "/org/matches/{}/shifts/new"
        when = (datetime.now(LA) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        for i in (mid, *GONE):
            if kind == "page":
                r = w.org1.get(path.format(i), follow_redirects=False)
            else:
                r = w.org1.post(
                    path.format(i), data={"starts_at": when}, follow_redirects=False
                )
            assert_refused(r)
        assert shifts.count == 1  # only the one made on accept
        # control: the owner's org is let in
        o = w.org0
        if kind == "page":
            assert o.get(path.format(mid)).status_code == 200
        else:
            r = o.post(
                path.format(mid), data={"starts_at": when}, follow_redirects=False
            )
            assert r.status_code == 303 and shifts.count == 2

    return check


def check_repeat_shift(w):
    """org1 can't add a weekly repeat to org 0's match, whatever the id; org 0 can."""
    openings.update(id=w.opening.id, recurrence="weekly")
    mid = accepted_match(w)
    for i in (mid, *GONE):
        assert_refused(
            w.org1.post(f"/org/matches/{i}/shifts/repeat", follow_redirects=False)
        )
    assert shifts.count == 1
    r = w.org0.post(f"/org/matches/{mid}/shifts/repeat", follow_redirects=False)
    assert r.status_code == 303 and shifts.count == 2


def check_cancel_shift(w):
    """org1 can't cancel org 0's shift, whatever the id; org 0 can."""
    sid = shifts(where="match_id = ?", where_args=[accepted_match(w)])[0].id
    for i in (sid, *GONE):
        assert_refused(w.org1.post(f"/org/shifts/{i}/cancel", follow_redirects=False))
    assert shifts[sid].status == "scheduled"
    r = w.org0.post(f"/org/shifts/{sid}/cancel", follow_redirects=False)
    assert r.status_code == 303 and shifts[sid].status == "cancelled"


def check_mark_shift(w):
    """org1 can't mark org 0's shift, whatever the id; org 0 can."""
    mid = accepted_match(w)
    sid = shifts(where="match_id = ?", where_args=[mid])[0].id
    shifts.update(id=sid, starts_at="2020-01-01T09:00")  # long past: markable
    for i in (sid, *GONE):
        assert_refused(
            w.org1.post(
                f"/org/shifts/{i}/mark",
                data={"result": "attended"},
                follow_redirects=False,
            )
        )
    assert shifts[sid].status == "scheduled"
    r = w.org0.post(
        f"/org/shifts/{sid}/mark", data={"result": "attended"}, follow_redirects=False
    )
    assert r.status_code == 303 and shifts[sid].status == "completed"


def check_shift_ics(w):
    """B gets 404 for A's shift's calendar file, whatever the id; A gets it."""
    sid = shifts(where="match_id = ?", where_args=[accepted_match(w)])[0].id
    for i in (sid, *GONE):
        r = w.b.get(f"/shifts/{i}.ics", follow_redirects=False)
        assert r.status_code == 404
        no_secrets(r)
    r = w.a.get(f"/shifts/{sid}.ics")
    assert r.status_code == 200 and "BEGIN:VEVENT" in r.text


def own_shift_check(method, action):
    """B, and A's own org's coordinator, get 404 or their home for A's shift, whatever the
    id: no page, the shift stays scheduled and unconfirmed, the org gets no email. Then A
    gets the page, or the change."""

    def check(w):
        sid = shifts(where="match_id = ?", where_args=[accepted_match(w)])[0].id
        sent = len(notify.get_emailer().sent)

        def call(client, i):
            path = f"/shifts/{i}/{action}"
            if method == "GET":
                return client.get(path, follow_redirects=False)
            return client.post(path, data={"reason": "x"}, follow_redirects=False)

        for i in (sid, *GONE):
            r = call(w.b, i)
            assert r.status_code == 404, (i, r.status_code)
            no_secrets(r)
        assert_turned_away(call(w.org0, sid), "coordinator", f"/shifts/{sid}")
        assert_turned_away(call(w.admin, sid), "admin", f"/shifts/{sid}")
        assert (shifts[sid].status, shifts[sid].confirmed_at) == ("scheduled", None)
        assert len(notify.get_emailer().sent) == sent
        r = call(w.a, sid)
        if method == "GET":
            assert r.status_code == 200 and PANTRY in r.text
        elif action == "cancel":
            assert r.status_code == 303 and shifts[sid].status == "cancelled"
            assert matches[w.match.id].status == "accepted"
        else:
            assert r.status_code == 303 and shifts[sid].confirmed_at

    return check


def stranger_post(client, template, **body):
    def call(w, rid):
        return client(w).post(
            template.format(rid),
            data=body or {"reason": "spam"},
            follow_redirects=False,
        )

    return call


def stranger_get(client, template):
    return lambda w, rid: client(w).get(template.format(rid), follow_redirects=False)


def untouched(w):
    return (
        not blocks.count
        and not reports.count
        and (matches[w.match.id].status == "pending")
    )


def safety_check(call):
    """A stranger's report or block on A's match: refused, nothing recorded."""
    return path_id_check(call, lambda w: w.match.id, untouched)


def _reported(w):
    data.report_org(w.a.uid, w.match.id, "spam")
    return reports()[0].id


def check_admin_report_route(action):
    def check(w):
        rid = _reported(w)
        for i in (rid, *GONE):
            assert_refused(
                w.b.post(f"/admin/reports/{i}/{action}", follow_redirects=False)
            )
        assert reports[rid].status == "open"
        r = w.admin.post(f"/admin/reports/{rid}/dismiss", follow_redirects=False)
        assert r.status_code == 303 and reports[rid].status == "dismissed"

    return check


def check_admin_unblock(w):
    data.block_org(w.a.uid, w.match.id)
    bid = blocks()[0].id
    for i in (bid, *GONE):
        assert_refused(w.b.post(f"/admin/blocks/{i}/unblock", follow_redirects=False))
    assert blocks.count == 1
    r = w.admin.post(f"/admin/blocks/{bid}/unblock", follow_redirects=False)
    assert r.status_code == 303 and blocks.count == 0


def check_admin_org(method, tail):
    def check(w):
        org = orgs()[0].id
        for i in (org, *GONE):
            r = w.b.request(
                method,
                f"/admin/orgs/{i}/{tail}"
                + ("?ein=530196605" if method == "GET" else ""),
                data={"ein": "530196605"} if method == "POST" else None,
                follow_redirects=False,
            )
            assert_refused(r)
        assert not orgs[org].ein

    return check


def check_admin_claim(action):
    def check(w):
        data.upsert_imported_org(
            "861620575",
            "Food Bank",
            "Los Angeles",
            "90019",
            "K31",
            "food-insecurity",
            34.0,
            -118.3,
        )
        data.submit_claim(
            "861620575", "Food Bank", "Test Claimant", "claimant@example.org"
        )
        cid = claim_requests()[0].id
        members = org_members.count
        for who in (w.b, w.org1, w.anon):
            for i in (cid, *GONE):
                r = who.post(f"/admin/claims/{i}/{action}", follow_redirects=False)
                assert r.status_code == 403 or (
                    r.status_code == 303 and r.headers["location"] != "/admin/claims"
                ), (r.status_code, r.headers.get("location"))
                no_secrets(r)
        assert claim_requests[cid].status == "pending" and org_members.count == members
        r = w.admin.post(f"/admin/claims/{cid}/{action}", follow_redirects=False)
        assert r.status_code == 303 and claim_requests[cid].status != "pending"

    return check


def check_photo(template):
    """Volunteer A's photo: 404 for B and for an org A never liked, as for a made-up key."""

    def check(w):
        r = w.a.post(
            "/me/profile/photo",
            files={"photo": ("a.jpg", jpeg(), "image/jpeg")},
            follow_redirects=False,
        )
        assert r.status_code == 303
        key = profiles[w.a.uid].photo_key
        for who in (w.b, w.org1):
            for k in (key, "A" * 22, "nope", key[:-1] + "x"):
                r = who.get(template.format(key=k), follow_redirects=False)
                assert r.status_code == 404 and r.content == b"Not found"
        assert w.a.get(template.format(key=key)).status_code == 200

    return check


def org_media_check(method, template, record):
    """org 1's coordinator, with org 0's id (record "org") or its opening's: refused, and
    org 0's logo, photo, about, website, opening photo and link unchanged."""

    def snapshot(w):
        o, org = openings[w.opening.id], orgs[w.opening.org_id]
        return (
            org.logo_key,
            org.photo_key,
            org.about,
            org.website,
            o.photo_key,
            o.event_url,
        )

    def call(w, i):
        path = template.format(org_id=i, opening_id=i)
        if method == "GET":
            return w.org1.get(path, follow_redirects=False)
        if path.endswith(("/logo", "/photo")):
            files = {"photo": ("p.jpg", jpeg(), "image/jpeg")}
            return w.org1.post(path, files=files, follow_redirects=False)
        body = {
            "about": "Hijacked",
            "website": "https://evil.example",
            "event_url": "https://evil.example",
        }
        return w.org1.post(path, data=body, follow_redirects=False)

    def check(w):
        before = snapshot(w)
        mine = w.opening.org_id if record == "org" else w.opening.id
        assert_refused(call(w, mine))
        for gone in GONE:
            assert_refused(call(w, gone))
        assert snapshot(w) == before

    return check


# route -> check(world), for every route that takes an id from the path or the body
OWNERSHIP = {
    **{
        (method, path): org_media_check(
            method, path, "org" if "{org_id}" in path else "opening"
        )
        for method, path in (
            ("POST", "/org/organization/{org_id}"),
            ("POST", "/org/organization/{org_id}/logo"),
            ("POST", "/org/organization/{org_id}/photo"),
            ("GET", "/org/openings/{opening_id}/media"),
            ("POST", "/org/openings/{opening_id}/photo"),
            ("POST", "/org/openings/{opening_id}/link"),
        )
    },
    ("GET", "/photos/{key}.jpg"): check_photo("/photos/{key}.jpg"),
    ("GET", "/photos/thumb/{key}.jpg"): check_photo("/photos/thumb/{key}.jpg"),
    ("GET", "/matches/{match_id}/withdraw"): path_id_check(
        withdraw_page_other,
        lambda w: w.match.id,
        lambda w: matches[w.match.id].status == "pending",
    ),
    ("POST", "/matches/{match_id}/withdraw"): path_id_check(
        withdraw_other,
        lambda w: w.match.id,
        lambda w: matches[w.match.id].status == "pending",
    ),
    ("POST", "/org/matches/{match_id}/decide"): path_id_check(
        decide_other,
        lambda w: w.match.id,
        lambda w: matches[w.match.id].status == "pending",
    ),
    ("POST", "/org/openings/{opening_id}/close"): path_id_check(
        close_other,
        lambda w: w.opening.id,
        lambda w: openings[w.opening.id].status == w.opening.status,
    ),
    ("GET", "/org/openings/{opening_id}/urgent"): urgent_check("GET", ""),
    ("POST", "/org/openings/{opening_id}/urgent"): urgent_check("POST", ""),
    ("POST", "/org/openings/{opening_id}/urgent/close"): urgent_check("POST", "/close"),
    ("POST", "/org/openings/new"): check_create_opening,
    ("GET", "/urgent/off/{key}"): urgent_off_check("GET"),
    ("POST", "/urgent/off/{key}"): urgent_off_check("POST"),
    ("POST", "/swipe"): check_swipe,
    ("GET", "/org/matches/{match_id}/shifts/new"): shift_match_check("page"),
    ("POST", "/org/matches/{match_id}/shifts/new"): shift_match_check("save"),
    ("POST", "/org/shifts/{shift_id}/mark"): check_mark_shift,
    ("POST", "/org/shifts/{shift_id}/cancel"): check_cancel_shift,
    ("POST", "/org/matches/{match_id}/shifts/repeat"): check_repeat_shift,
    ("GET", "/shifts/{shift_id}.ics"): check_shift_ics,
    **{
        (method, f"/shifts/{{shift_id}}/{action}"): own_shift_check(method, action)
        for method in ("GET", "POST")
        for action in ("cancel", "confirm")
    },
    ("GET", "/chat/{match_id}"): chat_id_check("b", "a", "/chat", "page"),
    ("POST", "/chat/{match_id}/send"): chat_id_check("b", "a", "/chat", "send"),
    ("WS", "/chat/{match_id}/ws"): chat_id_check("b", "a", "/chat", "ws"),
    ("GET", "/org/chat/{match_id}"): chat_id_check("org1", "org0", "/org/chat", "page"),
    ("POST", "/org/chat/{match_id}/send"): chat_id_check(
        "org1", "org0", "/org/chat", "send"
    ),
    ("WS", "/org/chat/{match_id}/ws"): chat_id_check("org1", "org0", "/org/chat", "ws"),
    ("GET", "/matches/{match_id}/report"): safety_check(
        stranger_get(lambda w: w.b, "/matches/{}/report")
    ),
    ("POST", "/matches/{match_id}/report"): safety_check(
        stranger_post(lambda w: w.b, "/matches/{}/report")
    ),
    ("GET", "/matches/{match_id}/block"): safety_check(
        stranger_get(lambda w: w.b, "/matches/{}/block")
    ),
    ("POST", "/matches/{match_id}/block"): safety_check(
        stranger_post(lambda w: w.b, "/matches/{}/block")
    ),
    ("GET", "/org/matches/{match_id}/report"): safety_check(
        stranger_get(lambda w: w.org1, "/org/matches/{}/report")
    ),
    ("POST", "/org/matches/{match_id}/report"): safety_check(
        stranger_post(lambda w: w.org1, "/org/matches/{}/report")
    ),
    ("GET", "/org/matches/{match_id}/block"): safety_check(
        stranger_get(lambda w: w.org1, "/org/matches/{}/block")
    ),
    ("POST", "/org/matches/{match_id}/block"): safety_check(
        stranger_post(lambda w: w.org1, "/org/matches/{}/block")
    ),
    ("POST", "/admin/reports/{report_id}/dismiss"): check_admin_report_route("dismiss"),
    ("POST", "/admin/reports/{report_id}/suspend"): check_admin_report_route("suspend"),
    ("POST", "/admin/reports/{report_id}/deactivate"): check_admin_report_route(
        "deactivate"
    ),
    ("POST", "/admin/reports/{report_id}/remove-photo"): check_admin_report_route(
        "remove-photo"
    ),
    ("POST", "/admin/blocks/{block_id}/unblock"): check_admin_unblock,
    ("POST", "/admin/claims/{claim_id}/approve"): check_admin_claim("approve"),
    ("POST", "/admin/claims/{claim_id}/reject"): check_admin_claim("reject"),
    ("POST", "/admin/orgs/{org_id}/lookup"): check_admin_org("POST", "lookup"),
    ("POST", "/admin/orgs/{org_id}/verify"): check_admin_org("POST", "verify"),
}


def handler_id_names():
    """Every parameter or form key named *_id in the route modules, found from the source."""
    names = set()
    for f in (ROOT / "app" / "routes").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.arg) and node.arg.endswith("_id"):
                names.add(node.arg)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.fullmatch(r"[a-z]+_id", node.value)
            ):
                names.add(node.value)
    return names


def test_every_route_with_an_id_has_an_ownership_check():
    path_ids = {r for r in ROUTES if "{" in r[1]}
    assert path_ids | set(BODY_IDS) == set(OWNERSHIP)
    assert set(BODY_IDS) <= set(ROUTES)
    declared = set().union(*BODY_IDS.values()) | {
        n for _, path in path_ids for n in re.findall(r"{(\w+)}", path)
    }
    assert handler_id_names() <= declared, (
        "an id the route modules read is not classified"
    )


@pytest.mark.parametrize("route", sorted(OWNERSHIP), ids=lambda r: " ".join(r))
def test_another_users_or_orgs_id_is_refused_and_unchanged(world, route):
    OWNERSHIP[route](world)


def test_the_owner_still_can_use_their_own_id(world):
    page = world.a.get(f"/matches/{world.match.id}/withdraw")
    assert page.status_code == 200 and PANTRY in page.text
    assert matches[world.match.id].status == "pending"  # a GET never withdraws
    r = world.org0.post(
        f"/org/matches/{world.match.id}/decide",
        data={"decision": "accept"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and matches[world.match.id].status == "accepted"
    r = world.a.post(f"/matches/{world.match.id}/withdraw", follow_redirects=False)
    assert r.status_code == 303 and matches[world.match.id].status == "withdrawn"
    r = world.org0.post(
        f"/org/openings/{world.opening.id}/close", follow_redirects=False
    )
    assert r.status_code == 303 and openings[world.opening.id].status != "open"


@pytest.mark.parametrize("path", ["/org", "/org/inbox"])
def test_another_orgs_coordinator_sees_none_of_this_orgs_data(world, path):
    assert NAME in world.org0.get("/org/inbox").text  # the data really is there
    r = world.org1.get(path)
    assert r.status_code == 200
    no_secrets(r)


def past_shift_world(w):
    """A's match accepted and its shift moved into the past, so every shift list shows it."""
    sid = shifts(where="match_id = ?", where_args=[accepted_match(w)])[0].id
    shifts.update(id=sid, starts_at="2020-01-01T09:00")


def test_another_orgs_coordinator_sees_none_of_this_orgs_shifts(world):
    past_shift_world(world)
    mine = world.org0.get("/org/shifts")
    assert NAME in mine.text and PANTRY in mine.text  # the data really is there
    r = world.org1.get("/org/shifts")
    assert r.status_code == 200
    no_secrets(r)


def test_a_volunteer_sees_none_of_another_volunteers_shifts(world):
    past_shift_world(world)
    mine = world.a.get("/history")
    assert PANTRY in mine.text  # the data really is there
    r = world.b.get("/history")
    assert r.status_code == 200
    no_secrets(r)


def test_a_volunteer_sees_none_of_another_volunteers_matches(world):
    assert PANTRY in world.a.get("/matches").text
    r = world.b.get("/matches")
    assert r.status_code == 200
    no_secrets(r)
    deck = world.b.get("/deck")
    assert NAME not in deck.text


# --- data.py is the only door to the database -----------------------------------------


TABLES = {
    "db", "_db", "causes", "login_tokens", "matches", "openings", "org_members",
    "messages", "orgs", "profiles", "swipes", "shifts", "notifications",
    "blocks", "reports",
}  # fmt: skip


def imports_db(source):
    """True when the module reaches the database: imports app.db however it is spelled,
    names it as a string (importlib), or takes a table or the db from app.data."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(
                a.name == "app.db" or a.name.startswith("app.db.") for a in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "app.db" or mod.startswith("app.db."):
                return True
            if mod in ("app", "") and any(a.name == "db" for a in node.names):
                return True
            if node.level and (mod == "db" or mod.startswith("db.")):
                return True
            if mod == "app.data" and any(a.name in TABLES for a in node.names):
                return True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == "app.db" or node.value.startswith("app.db."):
                return True
        elif (  # data.profiles, data._db
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "data"
            and node.attr in TABLES
        ):
            return True
    return False


def test_the_import_check_catches_every_spelling():
    for src in (
        "import app.db",
        "from app.db import profiles",
        "from app import db",
        "from . import db",
        "from .db import profiles",
        "from app.db.x import y",
        "from app.data import db",
        "from app.data import profiles as p",
        "import importlib; importlib.import_module('app.db')",
        "from app import data\ndata.profiles()",
    ):
        assert imports_db(src), src
    for src in ("from app import data", "import apsw", "from app import notify"):
        assert not imports_db(src), src


def test_only_data_imports_db():
    files = [ROOT / "main.py"] + sorted((ROOT / "app").rglob("*.py"))
    # demo_seed.py writes the demo's rows directly, as scripts/ do: it runs at startup and
    # in scripts, never on a request
    files = [f for f in files if f.name not in ("data.py", "db.py", "demo_seed.py")]
    assert any(f.parent.name == "routes" for f in files)  # the glob found the routes
    offenders = [str(f.relative_to(ROOT)) for f in files if imports_db(f.read_text())]
    assert offenders == []


def test_data_exposes_functions_only_no_tables_or_db():
    from apswutils.db import Database, Table

    for name, value in vars(data).items():
        assert not isinstance(value, (Table, Database)), name
        assert getattr(value, "__module__", None) != "app.db", name
        assert name == "_db" or name not in TABLES, name  # _db is the private import


# --- websockets: Beforeware does not run, so the handler checks the caller itself ------


def ws_app(role):
    """A throwaway app, built here and never shipped: one ws route guarded by the helper."""

    from fasthtml.common import fast_app

    wsapp, _ = fast_app(
        secret_key=session_secret(os.environ),
        session_cookie=session_cookie(),
        exts="ws",
    )

    async def conn(ws):
        if ws_session_user(ws, role) is None:
            await ws.close(code=1008)
            return
        await ws.send_text("welcome")

    async def on_msg(ws, msg: str = ""):
        if (
            ws_session_user(ws, role) is None
            and ws.application_state.name != "DISCONNECTED"
        ):
            await ws.close(code=1008)

    wsapp.ws("/ws-probe", conn=conn)(on_msg)
    return wsapp


def ws_client(role, signed_in_as=None):
    c = TestClient(ws_app(role))
    if signed_in_as is not None:
        c.cookies.update(signed_in_as.cookies)  # same secret, so the same session
    return c


def test_ws_helper_lets_the_right_role_in(world):
    assert not ws_refused(ws_client("volunteer", world.a), "/ws-probe")
    assert not ws_refused(ws_client("coordinator", world.org0), "/ws-probe")


def test_ws_helper_refuses_signed_out_and_wrong_role(world):
    assert ws_refused(ws_client("volunteer"), "/ws-probe")
    assert ws_refused(ws_client("volunteer", world.org0), "/ws-probe")
    assert ws_refused(ws_client("volunteer", world.admin), "/ws-probe")
    assert ws_refused(ws_client("coordinator", world.a), "/ws-probe")
    assert ws_refused(ws_client("coordinator", world.admin), "/ws-probe")


def test_ws_helper_refuses_unonboarded_removed_and_signed_out_everywhere(world):
    new = TestClient(app)
    sign_in(new)
    assert ws_refused(ws_client("volunteer", new), "/ws-probe")  # not onboarded
    org_members.delete_where("user_id = ?", [world.org0_uid])  # membership removed
    assert ws_refused(ws_client("coordinator", world.org0), "/ws-probe")
    data.sign_out_everywhere(world.a.uid)  # session version bumped
    assert ws_refused(ws_client("volunteer", world.a), "/ws-probe")
