"""Notifications: texts to volunteers, email to coordinators; once per event, throttled."""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from conftest import coordinator_for, org_sign_in, sign_in
from starlette.testclient import TestClient

from app import config, data, notify
from app.db import (
    matches,
    notifications,
    openings,
    orgs,
    profiles,
    rate_events,
    shifts,
)
from main import app

TITLE = "Sort and pack pantry boxes"
BASE = "https://causematch.test"
T0 = datetime(2027, 3, 1, 18, 0, tzinfo=UTC)  # 10:00 in Los Angeles
FMT = "%Y-%m-%dT%H:%M"
TOKEN = "scheduler-secret"


class Clock:
    def __init__(self, monkeypatch):
        self.at = T0
        monkeypatch.setattr(notify, "now", lambda: self.at)

    def advance(self, **delta):
        self.at += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Rows from other test files must not suppress sends here (ids are reused)."""
    notifications.delete_where()
    shifts.delete_where()
    notify.get_sms().sent.clear()
    monkeypatch.setenv("TASKS_TOKEN", TOKEN)


def texts():
    return notify.get_sms().sent


def emails():
    """Notification emails only (not sign-in links)."""
    return [m for m in notify.get_emailer().sent if "volunteer interest" in m[1]]


def volunteer(name="Zelda"):
    c = TestClient(app)
    sign_in(c)
    c.uid = data.get_or_create_volunteer(c.phone)
    profiles.update(id=c.uid, display_name=name, onboarded_at="2026-01-01T00:00:00")
    return c


def coordinator(org_index=0):
    uid, email = coordinator_for(org_index)
    c = TestClient(app)
    org_sign_in(c, email)
    c.uid = uid
    return c


def opening():
    return openings(where="title = ?", where_args=[TITLE])[0]


def like(vol, o=None):
    r = vol.post("/swipe", data={"opening_id": str((o or opening()).id), "liked": "1"})
    assert r.status_code == 200
    return matches(where="volunteer_id = ?", where_args=[vol.uid])[-1]


def accept(coord, m):
    r = coord.post(
        f"/org/matches/{m.id}/decide",
        data={"decision": "accept"},
        follow_redirects=False,
    )
    assert r.status_code == 303


@pytest.fixture
def world(clock):
    class W:
        pass

    w = W()
    w.vol, w.coord = volunteer(), coordinator()
    w.match = like(w.vol)
    notify.get_emailer().sent.clear()
    return w


@pytest.fixture
def chat(world):
    accept(world.coord, world.match)
    texts().clear()
    return world


def registered(match_id):
    """Wait until the server has registered a socket on the match (it accepts first)."""
    import time

    from app.routes import chat as chat_routes

    for _ in range(200):
        if chat_routes._sockets.get(match_id):
            return
        time.sleep(0.05)
    raise AssertionError("socket never registered")


def post_reminders(client, **headers):
    return client.post("/tasks/reminders", headers=headers)


# --- accepted ---------------------------------------------------------------------------


def test_accept_sends_one_text_with_the_expected_words(world):
    accept(world.coord, world.match)
    assert texts() == [
        (
            world.vol.phone,
            f"{orgs()[0].name} said yes to {TITLE}! Chat and details: {BASE}/matches",
        )
    ]


def test_a_second_accept_attempt_sends_nothing_more(world):
    accept(world.coord, world.match)
    accept(world.coord, world.match)
    notify.on_accepted(world.match.id)
    assert len(texts()) == 1


def test_decline_sends_nothing(world):
    world.coord.post(
        f"/org/matches/{world.match.id}/decide", data={"decision": "decline"}
    )
    assert texts() == []


# --- messages ---------------------------------------------------------------------------


def test_coordinator_message_texts_the_volunteer_without_the_message(chat, clock):
    chat.coord.post(
        f"/org/chat/{chat.match.id}/send", data={"msg": "secret shift plan"}
    )
    [(phone, body)] = texts()
    assert phone == chat.vol.phone
    assert (
        body
        == f"New message from {orgs()[0].name} on Causematch: {BASE}/chat/{chat.match.id}"
    )
    assert "secret" not in body


def test_volunteers_own_messages_send_nothing(chat):
    chat.vol.post(f"/chat/{chat.match.id}/send", data={"msg": "hello"})
    assert texts() == []


def test_message_texts_are_throttled_per_match_for_30_minutes(chat, clock):
    send = lambda: chat.coord.post(
        f"/org/chat/{chat.match.id}/send", data={"msg": "hi"}
    )
    send()
    clock.advance(minutes=10)
    send()
    assert len(texts()) == 1
    clock.advance(minutes=30)
    send()
    assert len(texts()) == 2


# --- likes ------------------------------------------------------------------------------


def test_a_like_emails_the_coordinators_without_volunteer_details(clock):
    coordinator()
    vol = volunteer("Zelda")
    like(vol)
    [(to, subject, body)] = emails()
    assert to == "coord0@example.org"
    assert subject == "New volunteer interest on Causematch"
    assert f"{BASE}/org/inbox" in body and "1 volunteer is waiting" in body
    assert "Zelda" not in body and vol.phone not in body


def test_like_emails_are_throttled_per_org_and_count_the_waiting(clock):
    coordinator()
    a, b = volunteer("A"), volunteer("B")
    like(a)
    like(b)
    assert len(emails()) == 1
    clock.advance(minutes=16)
    c = volunteer("C")
    like(c)
    assert len(emails()) == 2
    assert "3 volunteers are waiting" in emails()[-1][2]


def test_a_pass_sends_no_email(clock):
    coordinator()
    vol = volunteer()
    vol.post("/swipe", data={"opening_id": str(opening().id), "liked": "0"})
    assert emails() == []


def test_a_repeat_like_sends_no_second_email(clock):
    coordinator()
    vol = volunteer()
    like(vol)
    clock.advance(minutes=16)
    like(vol)
    assert len(emails()) == 1


# --- opt-out and deleted ----------------------------------------------------------------


def test_opted_out_and_deleted_volunteers_get_no_text(world):
    profiles.update(id=world.vol.uid, sms_opted_out=1)
    accept(world.coord, world.match)
    assert texts() == []
    other = volunteer("Bea")
    m = like(other)
    profiles.update(id=other.uid, deleted_at="2027-01-01T00:00:00")
    accept(world.coord, m)
    assert texts() == []
    assert [n.kind for n in notifications()] == ["likes"]  # only the org's email


class StoppedSMS:
    def send(self, phone, text):
        raise notify.SMSOptedOut


def test_twilio_stop_reply_sets_opted_out_and_is_not_retried(world, monkeypatch):
    monkeypatch.setattr(notify, "_sms", StoppedSMS())
    accept(world.coord, world.match)
    assert profiles[world.vol.uid].sms_opted_out
    assert [n.status for n in notifications() if n.kind == "accepted"] == ["skipped"]
    monkeypatch.setattr(notify, "_sms", notify.FakeSMS())
    notify.on_accepted(world.match.id)
    assert notify.get_sms().sent == []


def twilio(handler):
    """The real TwilioSMS with only its transport replaced, so its own URL is exercised."""
    sms = notify.TwilioSMS("ACx", "tok", "+13105550000")
    sms.client = httpx.Client(
        base_url=sms.client.base_url,
        auth=sms.client.auth,
        transport=httpx.MockTransport(handler),
    )
    return sms


def test_twilio_sender_maps_error_21610_to_opt_out():
    def handler(request):
        return httpx.Response(400, json={"code": 21610, "message": "unsubscribed"})

    sms = twilio(handler)
    with pytest.raises(notify.SMSOptedOut):
        sms.send("+13105550123", "hi")


def test_twilio_sender_posts_to_and_from_and_body():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={})

    sms = twilio(handler)
    sms.send("+13105550123", "hi")
    assert b"To=%2B13105550123" in seen[0].content and b"Body=hi" in seen[0].content
    assert b"From=%2B13105550000" in seen[0].content
    assert (
        str(seen[0].url)
        == "https://api.twilio.com/2010-04-01/Accounts/ACx/Messages.json"
    )
    assert seen[0].method == "POST"
    assert seen[0].headers["authorization"].startswith("Basic ")


def test_twilio_verifier_url_is_the_documented_one():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"status": "approved"})

    v = notify.TwilioVerifier("ACx", "tok", "VAy")
    v.client = httpx.Client(
        base_url=v.client.base_url, transport=httpx.MockTransport(handler)
    )
    v.send("+13105550123")
    assert v.check("+13105550123", "123456")
    assert seen == [
        "https://verify.twilio.com/v2/Services/VAy/Verifications",
        "https://verify.twilio.com/v2/Services/VAy/VerificationCheck",
    ]


# --- failures ---------------------------------------------------------------------------


class BrokenSMS:
    def send(self, phone, text):
        raise httpx.ConnectError(f"boom {phone}")


def test_failing_sms_never_breaks_accept_or_chat_and_is_recorded(
    world, monkeypatch, caplog
):
    monkeypatch.setattr(notify, "_sms", BrokenSMS())
    with caplog.at_level(logging.WARNING):
        accept(world.coord, world.match)
        r = world.coord.post(
            f"/org/chat/{world.match.id}/send",
            data={"msg": "hi"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert matches[world.match.id].status == "accepted"
    assert {n.status for n in notifications() if n.kind != "likes"} == {"failed"}
    assert world.vol.phone not in caplog.text


def test_failing_email_never_breaks_a_swipe(clock, monkeypatch):
    coordinator()
    monkeypatch.setattr(
        notify.get_emailer(), "send", lambda *a: (_ for _ in ()).throw(OSError("down"))
    )
    vol = volunteer()
    like(vol)
    assert [n.status for n in notifications()] == ["failed"]


def test_a_failed_send_can_be_retried(world, monkeypatch):
    monkeypatch.setattr(notify, "_sms", BrokenSMS())
    accept(world.coord, world.match)
    monkeypatch.setattr(notify, "_sms", notify.FakeSMS())
    notify.on_accepted(world.match.id)
    assert len(texts()) == 1


# --- reminders --------------------------------------------------------------------------


def test_reminder_endpoint_refuses_without_the_token(anon):
    assert post_reminders(anon).status_code == 401
    assert post_reminders(anon, Authorization="Bearer nope").status_code == 401
    assert post_reminders(anon, Authorization=TOKEN).status_code == 401
    assert anon.get("/tasks/reminders").status_code in (401, 405)
    assert (
        anon.get(
            "/tasks/reminders", headers={"Authorization": f"Bearer {TOKEN}"}
        ).status_code
        == 405
    )
    assert texts() == []


def test_reminder_endpoint_refuses_everything_when_no_token_is_configured(
    anon, monkeypatch
):
    monkeypatch.delenv("TASKS_TOKEN")
    assert post_reminders(anon, Authorization="Bearer ").status_code == 401
    assert post_reminders(anon).status_code == 401


def local(clock, **delta):
    return (clock.at + timedelta(**delta)).astimezone(notify.LA).strftime(FMT)


def test_reminders_pick_exactly_the_shifts_in_the_window_and_only_once(
    world, clock, anon
):
    accept(world.coord, world.match)
    texts().clear()
    base = shifts()[0]
    mk = lambda start, status="scheduled": shifts.insert(
        shifts.cls(
            match_id=base.match_id, starts_at=start, status=status, created_at="x"
        )
    )
    shifts.update(id=base.id, starts_at=local(clock, hours=24))  # in
    mk(local(clock, hours=23))  # in (edge)
    mk(local(clock, hours=25))  # in (edge)
    mk(local(clock, hours=22, minutes=59))  # too soon
    mk(local(clock, hours=25, minutes=1))  # too far
    mk(local(clock, hours=24, minutes=5), status="cancelled")
    # one reminder per volunteer per sweep: the three go out over three sweeps
    for n in (1, 2, 3, 3):
        r = post_reminders(anon, Authorization=f"Bearer {TOKEN}")
        assert r.status_code == 200
        assert len(texts()) == n
    assert all(
        t.startswith(f"Reminder: {TITLE} with {orgs()[0].name} is on ")
        and f"{BASE}/matches" in t
        for _, t in texts()
    )


def test_reminders_skip_withdrawn_matches_and_opted_out_volunteers(world, clock, anon):
    accept(world.coord, world.match)
    texts().clear()
    shifts.update(id=shifts()[0].id, starts_at=local(clock, hours=24))
    profiles.update(id=world.vol.uid, sms_opted_out=1)
    post_reminders(anon, Authorization=f"Bearer {TOKEN}")
    assert texts() == []


# --- configuration ----------------------------------------------------------------------


def test_production_refuses_to_start_without_twilio_email_or_tasks_token():
    prod = {"CAUSEMATCH_ENV": "production"}
    with pytest.raises(RuntimeError):
        notify.make_sms(prod)
    with pytest.raises(RuntimeError):
        notify.make_sms({**prod, "TWILIO_ACCOUNT_SID": "a", "TWILIO_AUTH_TOKEN": "b"})
    with pytest.raises(RuntimeError):
        notify.make_sms({"CAUSEMATCH_FAKE_SMS": "1", **prod})
    with pytest.raises(RuntimeError):
        notify.make_emailer(prod)
    with pytest.raises(RuntimeError):
        config.tasks_token(prod)
    with pytest.raises(RuntimeError):
        config.tasks_token({"K_SERVICE": "x"})
    with pytest.raises(RuntimeError, match="at least 32"):
        config.tasks_token({**prod, "TASKS_TOKEN": "t" * 31})
    assert config.tasks_token({**prod, "TASKS_TOKEN": "t" * 32}) == "t" * 32
    assert isinstance(
        notify.make_sms(
            {
                **prod,
                "TWILIO_ACCOUNT_SID": "a",
                "TWILIO_AUTH_TOKEN": "b",
                "TWILIO_FROM_NUMBER": "+13105550000",
            }
        ),
        notify.TwilioSMS,
    )


def test_startup_checks_the_sender_and_the_token():
    text = (Path(notify.__file__).parent.parent / "main.py").read_text()
    assert "notify.get_sms()" in text and "tasks_token()" in text


def test_phone_numbers_are_masked_in_logs():
    assert notify.mask("+13105550123") == "***0123"


# --- review fixes -----------------------------------------------------------------------


def test_message_texts_never_come_closer_than_the_window_even_across_a_bucket_edge(
    chat, clock
):
    send = lambda: chat.coord.post(
        f"/org/chat/{chat.match.id}/send", data={"msg": "hi"}
    )
    clock.at = datetime(2027, 3, 1, 18, 29, 59, tzinfo=UTC)
    send()
    clock.advance(seconds=2)  # the old fixed bucket ended here
    send()
    assert len(texts()) == 1
    clock.advance(minutes=30)
    send()
    assert len(texts()) == 2
    assert (
        len([n for n in notifications() if n.kind == "message"]) == 1
    )  # one row per stream


def test_like_emails_never_come_closer_than_the_window_across_a_bucket_edge(clock):
    coordinator()
    clock.at = datetime(2027, 3, 1, 18, 14, 59, tzinfo=UTC)
    like(volunteer("A"))
    clock.advance(seconds=2)
    like(volunteer("B"))
    assert len(emails()) == 1
    clock.advance(minutes=15)
    like(volunteer("C"))
    assert len(emails()) == 2


def test_no_text_while_the_volunteer_has_the_chat_open_and_one_when_not(chat):
    with chat.vol.websocket_connect(f"/chat/{chat.match.id}/ws"):
        registered(chat.match.id)
        chat.coord.post(f"/org/chat/{chat.match.id}/send", data={"msg": "live"})
        assert texts() == []
    # socket closed: offline now (a new window is not needed: nothing was claimed)
    chat.coord.post(f"/org/chat/{chat.match.id}/send", data={"msg": "later"})
    assert len(texts()) == 1


def test_volunteer_online_is_per_match_and_per_volunteer(chat):
    from app.routes import chat as chat_routes

    with chat.vol.websocket_connect(f"/chat/{chat.match.id}/ws"):
        registered(chat.match.id)
        assert chat_routes.volunteer_online(chat.match.id, chat.vol.uid)
        assert not chat_routes.volunteer_online(chat.match.id, chat.vol.uid + 99)
        assert not chat_routes.volunteer_online(chat.match.id + 99, chat.vol.uid)
    assert not chat_routes.volunteer_online(chat.match.id, chat.vol.uid)


def test_a_failed_accept_text_is_retried_by_the_sweep_and_stops_after_five(
    world, monkeypatch, anon
):
    monkeypatch.setattr(notify, "_sms", BrokenSMS())
    accept(world.coord, world.match)  # attempt 1
    row = lambda: next(n for n in notifications() if n.kind == "accepted")
    assert (row().status, row().attempts) == ("failed", 1)
    for expected in (2, 3, 4, 5):
        post_reminders(anon, Authorization=f"Bearer {TOKEN}")
        assert (row().status, row().attempts) == ("failed", expected)
    post_reminders(anon, Authorization=f"Bearer {TOKEN}")  # no sixth try
    assert row().attempts == 5


def test_a_transient_failure_succeeds_on_the_next_sweep(world, monkeypatch, anon):
    monkeypatch.setattr(notify, "_sms", BrokenSMS())
    accept(world.coord, world.match)
    monkeypatch.setattr(notify, "_sms", notify.FakeSMS())
    r = post_reminders(anon, Authorization=f"Bearer {TOKEN}")
    assert r.text == "sent 1" and len(texts()) == 1
    post_reminders(anon, Authorization=f"Bearer {TOKEN}")
    assert len(texts()) == 1


class TimeoutSMS:
    def send(self, phone, text):
        raise httpx.ReadTimeout("slow")


def test_a_timeout_is_unknown_and_never_retried(world, monkeypatch, anon):
    monkeypatch.setattr(notify, "_sms", TimeoutSMS())
    accept(world.coord, world.match)
    assert [n.status for n in notifications() if n.kind == "accepted"] == ["unknown"]
    monkeypatch.setattr(notify, "_sms", notify.FakeSMS())
    post_reminders(anon, Authorization=f"Bearer {TOKEN}")
    assert texts() == []


def test_a_recording_failure_after_a_send_does_not_fail_the_request(world, monkeypatch):
    def boom(*a):
        raise RuntimeError("db down")

    monkeypatch.setattr(data, "notification_done", boom)
    r = world.coord.post(
        f"/org/matches/{world.match.id}/decide",
        data={"decision": "accept"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and len(texts()) == 1


def test_overlapping_reminder_calls_send_once(world, clock, monkeypatch):
    import threading
    import time

    accept(world.coord, world.match)
    texts().clear()
    shifts.update(id=shifts()[0].id, starts_at=local(clock, hours=24))

    class Slow(notify.FakeSMS):
        def send(self, phone, text):
            time.sleep(0.2)
            super().send(phone, text)

    sms = Slow()
    monkeypatch.setattr(notify, "_sms", sms)
    threads = [threading.Thread(target=notify.send_due_reminders) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(sms.sent) == 1


# --- item 17: the SMS ceiling and the reminder cap ----------------------------------------


def status_of(key):
    return notifications(where="key = ?", where_args=[key])[0].status


def at_the_ceiling(monkeypatch, anon):
    """A ceiling of one text an hour, already used by a sign-in code."""
    rate_events.delete_where()
    monkeypatch.setenv("SMS_MAX_PER_HOUR", "1")
    assert anon.post("/login/send", data={"phone": "3105550140"}).status_code == 200


def test_over_the_ceiling_a_text_is_deferred_and_the_sweep_sends_it(
    world, monkeypatch, anon
):
    at_the_ceiling(monkeypatch, anon)
    accept(world.coord, world.match)
    key = f"accepted:{world.match.id}"
    assert texts() == [] and status_of(key) == "deferred"
    post_reminders(anon, Authorization=f"Bearer {TOKEN}")  # still at the ceiling
    assert texts() == [] and status_of(key) == "deferred"
    monkeypatch.setenv("SMS_MAX_PER_HOUR", "10")
    post_reminders(anon, Authorization=f"Bearer {TOKEN}")
    assert len(texts()) == 1 and "said yes" in texts()[0][1]
    assert status_of(key) == "sent"
    post_reminders(anon, Authorization=f"Bearer {TOKEN}")
    assert len(texts()) == 1  # once


def test_a_deferred_message_text_is_sent_by_the_sweep(chat, monkeypatch, anon):
    at_the_ceiling(monkeypatch, anon)
    chat.coord.post(f"/org/chat/{chat.match.id}/send", data={"msg": "See you"})
    assert texts() == [] and status_of(f"msg:{chat.match.id}") == "deferred"
    monkeypatch.setenv("SMS_MAX_PER_HOUR", "10")
    post_reminders(anon, Authorization=f"Bearer {TOKEN}")
    assert len(texts()) == 1 and "New message" in texts()[0][1]


def test_codes_and_notifications_share_the_ceiling(world, monkeypatch, anon):
    rate_events.delete_where()
    monkeypatch.setenv("SMS_MAX_PER_HOUR", "1")
    accept(world.coord, world.match)  # the text uses the hour's one
    assert len(texts()) == 1
    r = anon.post("/login/send", data={"phone": "3105550141"})
    assert r.status_code == 429


def test_reminders_per_sweep_are_capped(world, clock, anon, monkeypatch):
    monkeypatch.setattr(notify, "MAX_REMINDERS_PER_SWEEP", 1)
    other = volunteer("Yuri")
    m2 = like(other)
    accept(world.coord, world.match)
    accept(world.coord, m2)
    texts().clear()
    for s in shifts():
        shifts.update(id=s.id, starts_at=local(clock, hours=24))
    post_reminders(anon, Authorization=f"Bearer {TOKEN}")
    assert len(texts()) == 1
    post_reminders(anon, Authorization=f"Bearer {TOKEN}")
    assert len(texts()) == 2  # the other volunteer, one sweep later
