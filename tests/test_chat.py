import json
import re
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import anyio.from_thread
import pytest
from conftest import (
    coordinator,
    png,
    volunteer,
)
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import data, notify, security
from app.db import matches, messages, openings, orgs
from main import app

XSS = ["<script>alert(1)</script>", '"><img src=x onerror=alert(1)>']


@pytest.fixture(autouse=True)
def one_loop(monkeypatch):
    """Every TestClient runs on one event loop, as the server does. Each client has a loop
    of its own by default, and a socket cannot be written to from another client's loop."""
    with anyio.from_thread.start_blocking_portal() as portal:

        @contextmanager
        def factory(self):
            yield portal

        monkeypatch.setattr(TestClient, "_portal_factory", factory)
        yield


class Chat:
    """A volunteer and org 0's coordinator, with an accepted match between them."""

    def __init__(self):
        self.vol = volunteer("Zelda Quinn")
        self.coord = coordinator(0)
        self.opening = openings(where="org_id = ?", where_args=[orgs()[0].id])[0]
        self.match = data.swipe(self.vol.uid, self.opening.id, True)
        data.decide(self.coord.uid, self.match.id, True)
        self.id = self.match.id
        self.vol_ws = f"/chat/{self.id}/ws"
        self.coord_ws = f"/org/chat/{self.id}/ws"


@pytest.fixture
def chat():
    return Chat()


def say(ws, text):
    ws.send_text(json.dumps({"msg": text}))


def bubble(ws):
    """The next message frame, skipping the empty chat-error frames that follow a send."""
    while 'id="messages"' not in (frame := ws.receive_text()):
        assert 'id="chat-error"' in frame
    return frame


def refused(client, path):
    """True when the socket is closed on the caller with 1008. Sends a frame first, so a
    handler that wrongly lets the caller in answers instead of leaving the test hanging."""
    try:
        with client.websocket_connect(path) as ws:
            try:
                say(ws, "")
            except (anyio.ClosedResourceError, RuntimeError):
                return True  # already closed on us
            ws.receive_text()
    except WebSocketDisconnect as e:
        return e.code == 1008
    return False


def registered(match_id, n=1):
    """Wait until the server has registered n sockets on the match (it accepts first)."""
    from app.routes import chat as chat_routes

    for _ in range(200):
        if len(chat_routes._sockets.get(match_id, {})) >= n:
            return
        time.sleep(0.01)
    raise AssertionError("socket never registered")


def test_pending_match_has_no_chat():
    vol, coord = volunteer("Zelda Quinn"), coordinator(0)
    opening = openings(where="org_id = ?", where_args=[orgs()[0].id])[0]
    m = data.swipe(vol.uid, opening.id, True)
    assert m.status == "pending"
    for client, page in ((vol, f"/chat/{m.id}"), (coord, f"/org/chat/{m.id}")):
        r = client.get(page, follow_redirects=False)
        assert r.status_code == 403
        r = client.post(page + "/send", data={"msg": "hi"}, follow_redirects=False)
        assert r.status_code == 403
    assert refused(vol, f"/chat/{m.id}/ws")
    assert refused(coord, f"/org/chat/{m.id}/ws")
    assert messages.count == 0


def test_declined_match_has_no_chat():
    vol, coord = volunteer("Zelda Quinn"), coordinator(0)
    opening = openings(where="org_id = ?", where_args=[orgs()[0].id])[0]
    m = data.swipe(vol.uid, opening.id, True)
    data.decide(coord.uid, m.id, False)
    assert vol.get(f"/chat/{m.id}").status_code == 403
    assert coord.get(f"/org/chat/{m.id}").status_code == 403


def test_message_reaches_other_side(chat):
    with (
        chat.vol.websocket_connect(chat.vol_ws) as wv,
        chat.coord.websocket_connect(chat.coord_ws) as wc,
    ):
        say(wv, "I can come at nine")
        mine = bubble(wv)
        theirs = bubble(wc)
        assert "I can come at nine" in mine and "You" in mine
        assert "I can come at nine" in theirs and "Zelda" in theirs
        assert 'id="messages"' in theirs and 'hx-swap-oob="beforeend"' in theirs
        assert "Quinn" not in theirs  # the coordinator sees a first name only
        say(wc, "Great, see you then")
        assert "Great, see you then" in bubble(wc)
        assert "Pat Doe" in bubble(wv)
    assert [m.body for m in messages()] == ["I can come at nine", "Great, see you then"]


def test_saved_before_broadcast_and_history_shown_on_both_pages(chat):
    data.send_message(chat.vol.uid, chat.id, "alpha-msg")
    data.send_message(chat.coord.uid, chat.id, "omega-msg")
    for client, page in (
        (chat.vol, f"/chat/{chat.id}"),
        (chat.coord, f"/org/chat/{chat.id}"),
    ):
        html = client.get(page).text
        assert html.index("alpha-msg") < html.index("omega-msg")
        assert 'ws-connect="' in html and "ws-send" in html
        assert f'action="{page}/send"' in html


def test_headers_show_only_what_each_side_may_see(chat):
    v = chat.vol.get(f"/chat/{chat.id}").text
    c = chat.coord.get(f"/org/chat/{chat.id}").text
    assert chat.opening.title in v and orgs()[0].name in v and "Pat Doe" in v
    assert chat.opening.title in c and "Zelda" in c
    for secret in ("Quinn", chat.vol.phone, "coord0@example.org"):
        assert secret not in c and secret not in v


def test_third_parties_are_refused_on_http_and_ws(chat):
    other_vol, other_coord = volunteer("Bianca"), coordinator(1)
    data.send_message(chat.vol.uid, chat.id, "private words")
    for client, base in ((other_vol, "/chat"), (other_coord, "/org/chat")):
        r = client.get(f"{base}/{chat.id}", follow_redirects=False)
        assert r.status_code == 403 and "private words" not in r.text
        r = client.post(
            f"{base}/{chat.id}/send", data={"msg": "intrude"}, follow_redirects=False
        )
        assert r.status_code == 403
        assert refused(client, f"{base}/{chat.id}/ws")
    # a volunteer on the coordinator's door, and the reverse
    assert (
        chat.vol.get(f"/org/chat/{chat.id}", follow_redirects=False).status_code == 403
    )
    assert chat.coord.get(f"/chat/{chat.id}", follow_redirects=False).status_code in (
        303,
        403,
    )
    assert refused(chat.vol, chat.coord_ws) and refused(chat.coord, chat.vol_ws)
    assert [m.body for m in messages()] == ["private words"]


def test_signed_out_is_refused(chat):
    anon = TestClient(app)
    assert refused(anon, chat.vol_ws) and refused(anon, chat.coord_ws)
    r = anon.post(f"/chat/{chat.id}/send", data={"msg": "x"}, follow_redirects=False)
    assert r.status_code in (303, 401, 403) and messages.count == 0


def test_withdrawn_match_is_read_only(chat):
    data.send_message(chat.coord.uid, chat.id, "see you Saturday")
    data.withdraw(chat.vol.uid, chat.id)
    assert matches[chat.id].status == "withdrawn"
    for client, base in ((chat.vol, "/chat"), (chat.coord, "/org/chat")):
        page = client.get(f"{base}/{chat.id}")
        assert page.status_code == 200 and "see you Saturday" in page.text
        assert (
            "read-only" in page.text
            and 'name="msg"' not in page.text
            and "ws-connect" not in page.text
        )
        r = client.post(
            f"{base}/{chat.id}/send", data={"msg": "hello?"}, follow_redirects=False
        )
        assert r.status_code == 403
        assert refused(client, f"{base}/{chat.id}/ws")
    with pytest.raises(PermissionError):
        data.send_message(chat.coord.uid, chat.id, "hello?")
    assert [m.body for m in messages()] == ["see you Saturday"]


def test_withdrawn_before_acceptance_has_no_history_page():
    vol, coord = volunteer("Zelda Quinn"), coordinator(0)
    opening = openings(where="org_id = ?", where_args=[orgs()[0].id])[0]
    m = data.swipe(vol.uid, opening.id, True)
    data.withdraw(vol.uid, m.id)
    assert vol.get(f"/chat/{m.id}").status_code == 403
    assert coord.get(f"/org/chat/{m.id}").status_code == 403


def test_closing_a_chat_mid_conversation_stops_the_open_socket(chat):
    with chat.coord.websocket_connect(chat.coord_ws) as wc:
        data.withdraw(chat.vol.uid, chat.id)
        say(wc, "still there?")
        with pytest.raises(WebSocketDisconnect) as e:
            wc.receive_text()
        assert e.value.code == 1008
    assert messages.count == 0


@pytest.mark.parametrize("bad", ["", "   \n", "x" * 2001])
def test_empty_and_too_long_bodies_are_rejected(chat, bad):
    with pytest.raises(ValueError):
        data.send_message(chat.vol.uid, chat.id, bad)
    r = chat.vol.post(
        f"/chat/{chat.id}/send", data={"msg": bad}, follow_redirects=False
    )
    assert r.status_code == 303 and "notice=" in r.headers["location"]
    page = chat.vol.get(r.headers["location"])
    assert 'role="alert"' in page.text
    with chat.vol.websocket_connect(chat.vol_ws) as wv:
        say(wv, bad)
        assert 'id="chat-error"' in wv.receive_text()
    assert messages.count == 0


def test_body_limits_and_types(chat):
    assert data.send_message(chat.vol.uid, chat.id, "  hi  ")["body"] == "hi"
    assert data.send_message(chat.vol.uid, chat.id, "x" * 2000)["body"] == "x" * 2000
    for bad in (None, 5, ["a"], b"hi"):
        with pytest.raises(ValueError):
            data.send_message(chat.vol.uid, chat.id, bad)
    assert messages.count == 2


@pytest.mark.parametrize("payload", XSS)
def test_message_bodies_are_text_never_html(chat, payload):
    with (
        chat.vol.websocket_connect(chat.vol_ws) as wv,
        chat.coord.websocket_connect(chat.coord_ws) as wc,
    ):
        say(wv, payload)
        for frame in (bubble(wv), bubble(wc)):
            assert (
                payload not in frame and "<script" not in frame and "<img" not in frame
            )
            assert "&lt;" in frame or "&quot;" in frame or "&gt;" in frame
    for client, page in (
        (chat.vol, f"/chat/{chat.id}"),
        (chat.coord, f"/org/chat/{chat.id}"),
    ):
        html = client.get(page).text
        assert (
            payload not in html
            and "<img src=x" not in html
            and "<script>alert" not in html
        )
    assert messages()[0].body == payload  # stored as typed


def test_a_revoked_session_is_closed_on_the_next_message(chat):
    with chat.vol.websocket_connect(chat.vol_ws) as wv:
        data.sign_out_everywhere(chat.vol.uid)
        say(wv, "after sign out")
        with pytest.raises(WebSocketDisconnect) as e:
            wv.receive_text()
        assert e.value.code == 1008
    assert messages.count == 0


def test_a_revoked_listener_gets_nothing_and_is_closed(chat):
    with (
        chat.vol.websocket_connect(chat.vol_ws) as wv,
        chat.coord.websocket_connect(chat.coord_ws) as wc,
    ):
        data.sign_out_everywhere(chat.vol.uid)
        say(wc, "anyone there?")
        assert "anyone there?" in bubble(wc)
        with pytest.raises(WebSocketDisconnect) as e:
            wv.receive_text()
        assert e.value.code == 1008


def test_removed_coordinator_is_closed(chat):
    from app.db import org_members

    with chat.coord.websocket_connect(chat.coord_ws) as wc:
        org_members.delete_where("user_id = ?", [chat.coord.uid])
        say(wc, "hello")
        with pytest.raises(WebSocketDisconnect) as e:
            wc.receive_text()
        assert e.value.code == 1008
    assert messages.count == 0


def test_sockets_are_forgotten_on_disconnect(chat):
    from app.routes import chat as chat_routes

    with chat.vol.websocket_connect(chat.vol_ws):
        registered(chat.id)
        assert len(chat_routes._sockets[chat.id]) == 1
    assert chat.id not in chat_routes._sockets


def test_the_no_js_post_saves_and_redirects_back(chat):
    r = chat.vol.post(
        f"/chat/{chat.id}/send", data={"msg": "from a form"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == f"/chat/{chat.id}"
    r = chat.coord.post(
        f"/org/chat/{chat.id}/send", data={"msg": "reply"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == f"/org/chat/{chat.id}"
    assert [(m.sender_id, m.body) for m in messages()] == [
        (chat.vol.uid, "from a form"),
        (chat.coord.uid, "reply"),
    ]
    assert "reply" in chat.vol.get(f"/chat/{chat.id}").text


def test_http_post_reaches_an_open_socket(chat):
    with chat.coord.websocket_connect(chat.coord_ws) as wc:
        chat.vol.post(f"/chat/{chat.id}/send", data={"msg": "via form"})
        assert "via form" in bubble(wc)


def test_new_message_hook_is_called(chat, monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "on_new_message", lambda *a: calls.append(a))
    chat.vol.post(f"/chat/{chat.id}/send", data={"msg": "ping"})
    assert calls == [(chat.id, chat.vol.uid)]


def test_chat_links_from_matches_and_inbox(chat):
    assert f'href="/chat/{chat.id}"' in chat.vol.get("/matches").text
    assert f'href="/org/chat/{chat.id}"' in chat.coord.get("/org/inbox").text


def test_list_messages_is_the_latest_in_order(chat):
    for i in range(5):
        data.send_message(chat.vol.uid, chat.id, f"m{i}")
    assert [
        m["body"] for m in data.list_messages(chat.coord.uid, chat.id, limit=3)
    ] == ["m2", "m3", "m4"]


def test_vendored_ws_extension_is_served_and_pinned():
    html = TestClient(app).get("/login").text
    assert "/static/vendor/htmx-ext-ws-2.0.4.min.js" in html
    assert "cdn" not in html.split("htmx-ext-ws")[0].rsplit("<script", 1)[-1]
    assert (
        TestClient(app).get("/static/vendor/htmx-ext-ws-2.0.4.min.js").status_code
        == 200
    )
    assert TestClient(app).get("/static/chat.js").status_code == 200


# --- sockets: sign-out, refusals, caps and the new-message hook ---------------------------


def test_logout_ends_open_sockets_and_the_next_frame_is_refused(chat):
    with (
        chat.vol.websocket_connect(chat.vol_ws) as wv,
        chat.coord.websocket_connect(chat.coord_ws) as wc,
    ):
        registered(chat.id, 2)
        assert chat.vol.post("/logout", follow_redirects=False).status_code == 303
        say(wc, "after you left")
        assert "after you left" in bubble(wc)
        with pytest.raises(WebSocketDisconnect) as e:  # nothing arrived; just the close
            wv.receive_text()
        assert e.value.code == 1008
    assert refused(chat.vol, chat.vol_ws)  # the old session is dead everywhere


def test_a_frame_from_a_logged_out_socket_is_refused_and_not_saved(chat):
    with chat.vol.websocket_connect(chat.vol_ws) as wv:
        chat.vol.post("/logout", follow_redirects=False)
        say(wv, "sent after logout")
        with pytest.raises(WebSocketDisconnect) as e:
            wv.receive_text()
        assert e.value.code == 1008
    assert messages.count == 0


@pytest.mark.parametrize("base", ["/chat", "/org/chat"])
def test_a_file_part_as_msg_is_a_400_not_a_500(chat, base):
    client = chat.vol if base == "/chat" else chat.coord
    r = client.post(
        f"{base}/{chat.id}/send",
        files={"msg": ("a.txt", b"text", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 400 and messages.count == 0


def test_a_frame_sent_to_a_refused_socket_is_dropped_not_answered(chat, monkeypatch):
    """conn closes a refused socket, but a frame sent before the close arrived is still
    queued. Answering it wrote to the closed socket and raised in the handler (it surfaced
    as a failure in whichever test happened to refuse a socket)."""
    from app.routes import chat as chat_routes

    checked = threading.Semaphore(0)
    real = chat_routes._authorized

    def spy(*args):
        checked.release()
        return real(*args)

    monkeypatch.setattr(chat_routes, "_authorized", spy)
    with ExitStack() as stack:
        for _ in range(chat_routes.MAX_SOCKETS_PER_MATCH):
            stack.enter_context(chat.vol.websocket_connect(chat.vol_ws))
        registered(chat.id, chat_routes.MAX_SOCKETS_PER_MATCH)
        for _ in range(chat_routes.MAX_SOCKETS_PER_MATCH):
            assert checked.acquire(timeout=5)  # the five connects
        with chat.vol.websocket_connect(chat.vol_ws) as ws:
            say(ws, "")  # queued behind the connect
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_text()
            assert e.value.code == 1008
            assert checked.acquire(timeout=5)  # the sixth connect's own check
            # the frame must not be checked, let alone answered, while the app is still alive
            assert not checked.acquire(timeout=0.5)


def test_the_event_loop_is_not_held_by_the_data_lock(chat):
    held, release = threading.Event(), threading.Event()

    def hold():
        with data._lock:
            held.set()
            release.wait(5)

    with chat.vol.websocket_connect(chat.vol_ws) as wv:
        registered(chat.id)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(5)
        say(wv, "waits for the lock")  # the handler now waits in the threadpool
        time.sleep(0.2)
        start = time.monotonic()
        assert TestClient(app).get("/static/chat.js").status_code == 200
        assert time.monotonic() - start < 0.5  # the loop stayed free
        release.set()
        t.join()
        assert "waits for the lock" in bubble(wv)


def test_the_hook_runs_off_the_loop_after_the_broadcast(chat, monkeypatch):
    import asyncio

    seen = {}
    go = threading.Event()

    def hook(match_id, sender):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        go.wait(5)  # only released once the test has the bubble: broadcast came first

    monkeypatch.setattr(notify, "on_new_message", hook)
    with chat.vol.websocket_connect(chat.vol_ws) as wv:
        say(wv, "ping")
        assert "ping" in bubble(wv)
        go.set()
    assert seen == {"on_loop": False}


def test_a_failing_hook_does_not_lose_the_message(chat, monkeypatch):
    def boom(*a):
        raise RuntimeError("sms down")

    monkeypatch.setattr(notify, "on_new_message", boom)
    with chat.vol.websocket_connect(chat.vol_ws) as wv:
        say(wv, "still delivered")
        assert "still delivered" in bubble(wv)


def test_a_dead_revoked_recipient_does_not_stop_the_others(chat):
    from app.routes import chat as chat_routes

    with (
        chat.vol.websocket_connect(chat.vol_ws),
        chat.coord.websocket_connect(chat.coord_ws) as wc,
    ):
        registered(chat.id, 2)
        (dead,) = [
            w
            for w, (u, _) in chat_routes._sockets[chat.id].items()
            if u == chat.vol.uid
        ]

        async def broken_close(*a, **kw):
            raise RuntimeError("socket is gone")

        dead.close = broken_close
        data.sign_out_everywhere(chat.vol.uid)
        say(wc, "anyone?")
        assert "anyone?" in bubble(wc)  # the sender still gets theirs
    assert [m.body for m in messages()] == ["anyone?"]


def test_socket_caps_per_match_and_per_user(chat):
    from app.db import matches as match_table
    from app.routes.chat import MAX_SOCKETS, MAX_SOCKETS_PER_MATCH

    with ExitStack() as stack:
        for _ in range(MAX_SOCKETS_PER_MATCH):
            stack.enter_context(chat.vol.websocket_connect(chat.vol_ws))
        registered(chat.id, MAX_SOCKETS_PER_MATCH)
        assert refused(chat.vol, chat.vol_ws)  # one too many on this match
        # another user is not affected
        with chat.coord.websocket_connect(chat.coord_ws) as wc:
            say(wc, "fine")
            assert "fine" in bubble(wc)
        # the overall cap, across more matches
        ids = []
        for o in openings()[1:]:
            if len(ids) == MAX_SOCKETS // MAX_SOCKETS_PER_MATCH:
                break
            m = match_table.insert(
                opening_id=o.id,
                volunteer_id=chat.vol.uid,
                status="accepted",
                created_at="2026-01-01T00:00:00+00:00",
                decided_at="2026-01-01T00:00:00+00:00",
            )
            ids.append(m.id)
        for mid in ids[:-1]:
            for _ in range(MAX_SOCKETS_PER_MATCH):
                stack.enter_context(chat.vol.websocket_connect(f"/chat/{mid}/ws"))
            registered(mid, MAX_SOCKETS_PER_MATCH)
        total = MAX_SOCKETS_PER_MATCH * len(ids[:-1]) + MAX_SOCKETS_PER_MATCH
        assert total == MAX_SOCKETS
        assert refused(chat.vol, f"/chat/{ids[-1]}/ws")
        # closing one frees a slot on that match
    from app.routes import chat as chat_routes

    assert not chat_routes._sockets


# --- cross-site sockets and the frame limit --------------------------------------------


@pytest.mark.parametrize("origin", ["https://evil.example", "null", None])
def test_a_foreign_or_missing_origin_is_refused_at_the_handshake(chat, origin):
    for client, path in ((chat.vol, chat.vol_ws), (chat.coord, chat.coord_ws)):
        headers = {"Origin": origin} if origin else {}
        if origin is None:
            del client.headers["origin"]
        with (
            pytest.raises(WebSocketDisconnect) as e,
            client.websocket_connect(path, headers=headers),
        ):
            pass
        assert e.value.code == 1008
    assert not data.list_messages(chat.vol.uid, chat.id)


def test_the_same_origin_socket_is_accepted(chat):
    with chat.vol.websocket_connect(chat.vol_ws) as wv:
        registered(chat.id)
        say(wv, "hello")
        assert "hello" in bubble(wv)


def test_too_many_frames_close_the_socket_with_1013(chat, monkeypatch):
    # a stopped clock for the bucket: on a slow machine a token refilled between frames, the
    # 21st was delivered, and the receive loop below waited for a close that never came
    monkeypatch.setattr(
        security, "time", SimpleNamespace(monotonic=lambda: 1000.0, time=time.time)
    )
    with chat.vol.websocket_connect(chat.vol_ws) as wv:
        registered(chat.id)
        for i in range(20):
            say(wv, f"m{i}")
            assert f"m{i}" in bubble(wv)
        say(wv, "one too many")
        with pytest.raises(WebSocketDisconnect) as e:
            while True:
                wv.receive_text()
        assert e.value.code == 1013
    assert len(data.list_messages(chat.vol.uid, chat.id)) == 20


def test_a_message_with_a_bidi_control_is_refused():
    """A right-to-left override can make a message read as something else (review 4)."""
    with pytest.raises(ValueError, match="can't show"):
        data.send_message(1, 1, "pay at \u202emoc.live")


def test_matches_row_shows_the_org_logo_else_its_initial(chat):
    org = orgs[chat.opening.org_id]
    assert not (org.logo_key or org.photo_key or chat.opening.photo_key)
    page = chat.vol.get("/matches").text
    assert "/photos/" not in page
    assert re.search(rf'aria-hidden="true" class="h-12 w-12[^"]*">{org.name[0]}<', page)
    r = chat.coord.post(
        f"/org/organization/{org.id}/logo",
        files={"photo": ("p", png(), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    src = f"/photos/thumb/{orgs[org.id].logo_key}.jpg"
    for page in (chat.vol.get("/matches").text, chat.vol.get(f"/chat/{chat.id}").text):
        assert f'src="{src}"' in page and 'loading="lazy"' in page
    assert chat.vol.get(src).status_code == 200  # the logo is served to the volunteer


def test_notes_and_calendar_show_once_accepted_only(chat):
    openings.update(id=chat.opening.id, notes="Closed-toe shoes, park on 5th")
    pending = volunteer("Bianca")
    data.swipe(pending.uid, chat.opening.id, True)
    assert "Closed-toe shoes" not in pending.get("/matches").text
    soon = datetime.now(ZoneInfo("America/Los_Angeles")) + timedelta(days=3)
    data.add_shift(chat.coord.uid, chat.id, soon.strftime("%Y-%m-%dT%H:%M"))
    shift = data.my_matches(chat.vol.uid)[0]["shift"]
    for page in (chat.vol.get("/matches").text, chat.vol.get(f"/chat/{chat.id}").text):
        assert "Closed-toe shoes, park on 5th" in page
        assert f'href="/shifts/{shift["id"]}.ics"' in page
        assert "https://calendar.google.com/calendar/render?action=TEMPLATE" in page
