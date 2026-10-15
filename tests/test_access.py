"""Every route is classified, and checked for sign-in, role and ownership."""

import ast
import re
from pathlib import Path

import pytest
from conftest import coordinator_for, org_sign_in, sign_in
from starlette.routing import Mount
from starlette.testclient import TestClient

from app import data
from app.auth import PUBLIC, is_org_path
from app.db import matches, openings, orgs, profiles
from main import app

ROOT = Path(__file__).resolve().parent.parent
TITLE = "Sort and pack pantry boxes"  # org 0's opening that A likes
NAME = "Zelda"  # volunteer A, whose data nobody else may see
SECRETS = (NAME, TITLE)

# (method, path) -> (who may use it, how the route is scoped)
#   who: public | volunteer | coordinator | admin (no admin route exists yet)
#   scope: None | own-record (the signed-in volunteer's) | own-org (the coordinator's org's)
ROUTES = {
    ("GET", "/login"): ("public", None),
    ("POST", "/login/send"): ("public", None),
    ("POST", "/login/verify"): ("public", None),
    ("POST", "/logout"): ("volunteer", None),  # any signed-in role may sign out
    ("GET", "/org/login"): ("public", None),
    ("POST", "/org/login/send"): ("public", None),
    ("GET", "/org/login/verify"): ("public", None),
    ("POST", "/org/login/verify"): ("public", None),
    ("GET", "/onboarding"): ("volunteer", "own-record"),
    ("POST", "/onboarding"): ("volunteer", "own-record"),
    ("GET", "/"): ("volunteer", None),
    ("POST", "/"): ("volunteer", None),
    ("GET", "/deck"): ("volunteer", "own-record"),
    ("POST", "/deck"): ("volunteer", "own-record"),
    ("POST", "/swipe"): ("volunteer", "own-record"),
    ("GET", "/matches"): ("volunteer", "own-record"),
    ("POST", "/matches/{match_id}/withdraw"): ("volunteer", "own-record"),
    ("GET", "/org"): ("coordinator", "own-org"),
    ("POST", "/org/details"): ("coordinator", "own-org"),
    ("GET", "/org/openings/new"): ("coordinator", "own-org"),
    ("POST", "/org/openings/new"): ("coordinator", "own-org"),
    ("POST", "/org/openings/{opening_id}/close"): ("coordinator", "own-org"),
    ("GET", "/org/inbox"): ("coordinator", "own-org"),
    ("POST", "/org/matches/{match_id}/decide"): ("coordinator", "own-org"),
    ("*", "/static"): ("public", None),  # the static mount: manifest, icons, scripts
}
PUBLIC_ROUTES = [r for r, (who, _) in ROUTES.items() if who == "public"]
GUARDED = [r for r, (who, _) in ROUTES.items() if who != "public"]
# Sign-out works for every signed-in role, so it is not a role-wrong case.
ROLE_ROUTES = [r for r in GUARDED if r != ("POST", "/logout")]


def app_routes(routes, prefix=""):
    """Every (method, path) the router serves, mounts included, found from the app itself."""
    for r in routes:
        path = prefix + r.path
        if isinstance(r, Mount) and getattr(r, "routes", None):
            yield from app_routes(r.routes, path)
        elif isinstance(r, Mount) or not getattr(r, "methods", None):
            yield ("*", path)
        else:
            for method in sorted(set(r.methods) - {"HEAD", "OPTIONS"}):
                yield (method, path)


def concrete(path, match_id, opening_id):
    return path.format(match_id=match_id, opening_id=opening_id)


def is_public_path(path):
    probe = path + "/x" if path == "/static" else path
    return any(re.fullmatch(p, probe) for p in PUBLIC)


def home_of(role):
    return "/org" if role == "coordinator" else "/deck"


def login_of(path):
    return "/org/login" if is_org_path(path) else "/login"


def send(client, method, path):
    """The route's own method, with a body shaped like what its form posts."""
    if method == "GET":
        return client.get(path, follow_redirects=False)
    body = {"opening_id": "1", "liked": "1", "decision": "accept", "first_name": "x"}
    return client.request(method, path, data=body, follow_redirects=False)


@pytest.fixture
def world():
    """Volunteers A and B, an admin, and a coordinator for org 0 and one for org 1.

    A has liked an opening of org 0, so a pending match of org 0 exists.
    """

    def volunteer(name):
        c = TestClient(app)
        sign_in(c)
        c.uid = data.get_or_create_volunteer(c.phone)
        profiles.update(id=c.uid, display_name=name, onboarded_at="2026-01-01T00:00:00")
        return c

    class W:
        pass

    w = W()
    w.a, w.b, w.admin = volunteer(NAME), volunteer("Bianca"), volunteer("Root")
    profiles.update(id=w.admin.uid, role="admin")
    w.anon = TestClient(app)
    for i in (0, 1):
        _, email = coordinator_for(i)
        c = TestClient(app)
        org_sign_in(c, email)
        setattr(w, f"org{i}", c)
    w.opening = openings(where="title = ?", where_args=[TITLE])[0]
    w.other_opening = openings(where="org_id = ?", where_args=[orgs()[1].id])[0]
    assert w.opening.org_id == orgs()[0].id
    w.match = data.swipe(w.a.uid, w.opening.id, True)
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
        assert who in ("public", "volunteer", "coordinator", "admin")
        assert scope in (None, "own-record", "own-org")
        if "{" in path:
            assert scope, f"{path} takes an id: say whose record it is"
        if is_org_path(path) and who != "public":
            assert who == "coordinator", path
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
    r = send(world.anon, method, path)
    assert r.status_code in (401, 403) or (
        r.status_code in (302, 303) and r.headers["location"] == login_of(path)
    ), (r.status_code, r.headers.get("location"))
    no_secrets(r)
    assert matches[world.match.id].status == "pending"
    assert openings[world.opening.id].status == world.opening.status


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
    }[who]
    for role, client in callers:
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


# route -> (call as a stranger with an id, the record's id in the world, state that must hold)
OWNERSHIP = {
    ("POST", "/matches/{match_id}/withdraw"): (
        withdraw_other,
        lambda w: w.match.id,
        lambda w: matches[w.match.id].status == "pending",
    ),
    ("POST", "/org/matches/{match_id}/decide"): (
        decide_other,
        lambda w: w.match.id,
        lambda w: matches[w.match.id].status == "pending",
    ),
    ("POST", "/org/openings/{opening_id}/close"): (
        close_other,
        lambda w: w.opening.id,
        lambda w: openings[w.opening.id].status == w.opening.status,
    ),
}


def test_every_route_with_an_id_has_an_ownership_check():
    with_ids = {r for r in ROUTES if "{" in r[1]}
    assert with_ids == set(OWNERSHIP)


@pytest.mark.parametrize("route", sorted(OWNERSHIP), ids=lambda r: " ".join(r))
def test_another_users_or_orgs_id_is_refused_and_unchanged(world, route):
    call, record_id, intact = OWNERSHIP[route]
    assert_refused(call(world, record_id(world)))
    assert intact(world)
    for gone in GONE:
        assert_refused(call(world, gone))
    assert intact(world)


def test_the_owner_still_can_use_their_own_id(world):
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


def test_a_volunteer_sees_none_of_another_volunteers_matches(world):
    assert TITLE in world.a.get("/matches").text
    r = world.b.get("/matches")
    assert r.status_code == 200
    no_secrets(r)
    deck = world.b.get("/deck")
    assert NAME not in deck.text


# --- data.py is the only door to the database -----------------------------------------


def imports_db(source):
    """True when the module imports app.db, however it spells it."""
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
    return False


def test_the_import_check_catches_every_spelling():
    for src in (
        "import app.db",
        "from app.db import profiles",
        "from app import db",
        "from . import db",
        "from .db import profiles",
        "from app.db.x import y",
    ):
        assert imports_db(src), src
    for src in ("from app import data", "import apsw", "from app import notify"):
        assert not imports_db(src), src


def test_only_data_imports_db():
    files = [ROOT / "main.py"] + sorted((ROOT / "app").rglob("*.py"))
    files = [f for f in files if f.name not in ("data.py", "db.py")]
    assert any(f.parent.name == "routes" for f in files)  # the glob found the routes
    offenders = [str(f.relative_to(ROOT)) for f in files if imports_db(f.read_text())]
    assert offenders == []
