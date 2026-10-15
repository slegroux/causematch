"""A cookie named like a form field must never override what the user submitted.

FastHTML binds a handler parameter from the path, then cookies, then headers and the
query, and only then the body, so user input is read explicitly (app/forms.py).
"""

import ast
import json
import re
from pathlib import Path

import pytest
from conftest import FORM, coordinator_for, org_sign_in, sign_in
from starlette.testclient import TestClient
from test_access import opening_form

from app import data, notify
from app.db import matches, messages, openings, orgs, profiles
from main import app

HOSTILE = "hostile-cookie-value"
ROOT = Path(__file__).resolve().parent.parent
# What a handler parameter may be without coming from the request's data: the request, the
# session, the socket, and FastHTML's other injected names.
INJECTED = {"req", "request", "sess", "session", "ws", "scope", "auth", "app", "state"}


def with_cookie(client, name, value=HOSTILE):
    client.cookies.set(name, value)
    return client


def test_no_handler_takes_user_input_through_a_parameter():
    """Only path ids (path values win over cookies) and injected names may be parameters."""
    offenders = []
    for f in sorted((ROOT / "app" / "routes").glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            paths = [
                d.args[0].value
                for d in node.decorator_list
                if isinstance(d, ast.Call)
                and d.args
                and isinstance(d.args[0], ast.Constant)
            ]
            if not paths:
                continue
            in_path = {p for path in paths for p in re.findall(r"{(\w+)", path)}
            for a in node.args.args:
                if a.arg not in INJECTED | in_path:
                    offenders.append(f"{f.name}:{node.name}({a.arg})")
    assert offenders == []


# --- the sign-in forms -------------------------------------------------------------------


@pytest.mark.parametrize("name", ["phone", "code"])
def test_login_form_beats_a_cookie(name):
    c = with_cookie(
        TestClient(app), name, "3105559999" if name == "phone" else "111111"
    )
    c.post("/login/send", data={"phone": "(310) 555-0111"})
    assert notify.get_verifier().sent[-1][0] == "+13105550111"
    r = c.post("/login/verify", data={"code": "000000"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"


def test_org_token_in_the_link_beats_a_cookie():
    _, email = coordinator_for(0)
    c = with_cookie(TestClient(app), "token")
    c.post("/org/login/send", data={"email": email})
    token = re.search(r"token=([\w-]+)", notify.get_emailer().sent[-1][2]).group(1)
    html = c.get(f"/org/login/verify?token={token}").text  # kept in the session
    assert HOSTILE not in html
    r = c.post("/org/login/verify", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/org"


# --- the volunteer and coordinator forms ---------------------------------------------------


@pytest.mark.parametrize("name", sorted(FORM))
def test_onboarding_field_beats_a_cookie(new_user, name):
    with_cookie(new_user, name)
    r = new_user.post("/onboarding", data=FORM, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/deck"
    (p,) = profiles(where="phone = ?", where_args=[new_user.phone])
    assert (p.display_name, p.neighborhood, p.heard_from) == (
        "Sam",
        "Chinatown",
        "public",
    )
    assert json.loads(p.causes) == ["food-insecurity"]


@pytest.mark.parametrize("name", sorted(opening_form(1)))
def test_opening_field_beats_a_cookie(coord, name):
    with_cookie(coord, name)
    n = openings.count
    r = coord.post(
        "/org/openings/new", data=opening_form(coord.org_id), follow_redirects=False
    )
    assert r.status_code == 303 and openings.count == n + 1
    (o,) = openings(where="title = 'Probe opening'")
    assert (o.org_id, o.neighborhood, o.slots) == (coord.org_id, "Chinatown", 5)


def test_swipe_fields_beat_cookies(user):
    uid = data.get_or_create_volunteer(user.phone)
    ids = [c["id"] for c in data.deck(uid, 5)]
    mine, other = ids[0], ids[1]
    with_cookie(user, "opening_id", str(other))
    with_cookie(user, "liked", "0")
    user.post("/swipe", data={"opening_id": str(mine), "liked": "1"})
    assert [m.opening_id for m in matches()] == [mine]


def test_decide_field_beats_a_cookie(coord):
    vol = TestClient(app)
    sign_in(vol)
    uid = data.get_or_create_volunteer(vol.phone)
    profiles.update(id=uid, display_name="Sam", onboarded_at="2026-01-01T00:00:00")
    opening = openings(where="org_id = ?", where_args=[coord.org_id])[0]
    m = data.swipe(uid, opening.id, True)
    with_cookie(coord, "decision", "decline")
    coord.post(f"/org/matches/{m.id}/decide", data={"decision": "accept"})
    assert matches[m.id].status == "accepted"


@pytest.mark.parametrize("name", ["notice", "error"])
def test_messages_come_from_the_server_not_a_cookie(coord, name):
    for path in ("/org", "/org/inbox"):
        html = with_cookie(coord, name, "empty").get(path).text
        assert 'role="alert"' not in html and "Write a message first" not in html
        html = coord.get(path, params={"error": "Injected text"}).text
        assert "Injected text" not in html


# --- chat ----------------------------------------------------------------------------------


@pytest.fixture
def accepted():
    vol, coord = TestClient(app), TestClient(app)
    sign_in(vol)
    vol.uid = data.get_or_create_volunteer(vol.phone)
    profiles.update(id=vol.uid, display_name="Sam", onboarded_at="2026-01-01T00:00:00")
    cuid, email = coordinator_for(0)
    org_sign_in(coord, email)
    opening = openings(where="org_id = ?", where_args=[orgs()[0].id])[0]
    m = data.swipe(vol.uid, opening.id, True)
    data.decide(cuid, m.id, True)
    return vol, coord, m.id


def test_chat_post_beats_a_msg_cookie(accepted):
    vol, _, mid = accepted
    with_cookie(vol, "msg")
    vol.post(f"/chat/{mid}/send", data={"msg": "what I typed"})
    assert [m.body for m in messages()] == ["what I typed"]


def test_chat_ws_frame_beats_a_msg_cookie(accepted):
    vol, _, mid = accepted
    with_cookie(vol, "msg")
    with vol.websocket_connect(f"/chat/{mid}/ws") as ws:
        ws.send_text(json.dumps({"msg": "what I typed"}))
        assert "what I typed" in ws.receive_text()
    assert [m.body for m in messages()] == ["what I typed"]


def test_chat_notice_is_not_taken_from_a_cookie(accepted):
    vol, _, mid = accepted
    with_cookie(vol, "notice", "empty")
    assert "Write a message first" not in vol.get(f"/chat/{mid}").text
