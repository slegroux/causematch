"""Notifications: texts to volunteers, email to coordinators; once per event, throttled."""

import asyncio
import logging
import smtplib
import socket
import ssl
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import pytest
from conftest import (
    PANTRY,
    add_number,
    adder,
    coordinator,
    coordinator_for,
    pantry_opening,
    volunteer,
)
from test_oauth import EMAIL, google_sign_in

from app import config, data, notify, security
from app.db import (
    matches,
    notifications,
    openings,
    orgs,
    profiles,
    rate_events,
    shifts,
)

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


def opening():
    return pantry_opening()


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
    w = SimpleNamespace()
    w.vol, w.coord = volunteer("Zelda"), coordinator()
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
            f"{orgs()[0].name} said yes to {PANTRY}! Say hi: {BASE}/chat/{world.match.id}",
        )
    ]


def test_a_volunteer_without_a_phone_is_emailed_once_when_matched(world, anon):
    accept(world.coord, world.match)  # a volunteer with a number still gets a text
    assert [to for to, _ in texts()] == [world.vol.phone]
    texts().clear()
    google_sign_in(anon)
    (p,) = profiles(where="email = ?", where_args=[EMAIL])
    profiles.update(id=p.id, onboarded_at="2026-01-01T00:00:00", heard_from="skipped")
    anon.uid = p.id
    m = like(anon)
    notify.get_emailer().sent.clear()
    accept(world.coord, m)
    notify.on_accepted(m.id)  # a repeat sends nothing more
    org = orgs()[0].name
    assert notify.get_emailer().sent == [
        (
            EMAIL,
            f"You're matched with {org}",
            f"{org} said yes to {PANTRY}!\n\nSay hi: {BASE}/chat/{m.id}",
        )
    ]
    assert texts() == []


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
    vol = volunteer("Zelda")
    vol.post("/swipe", data={"opening_id": str(opening().id), "liked": "0"})
    assert emails() == []


def test_a_repeat_like_sends_no_second_email(clock):
    coordinator()
    vol = volunteer("Zelda")
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
    vol = volunteer("Zelda")
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
    # "Still coming?": each links to its own shift's confirm page, with no login token
    assert sorted(t.rsplit(" ", 1)[1] for _, t in texts()) == sorted(
        f"{BASE}/shifts/{s.id}/confirm"
        for s in shifts(where="status = 'scheduled'")
        if local(clock, hours=23) <= s.starts_at <= local(clock, hours=25)
    )
    assert all(
        t.startswith(f"Reminder: {PANTRY} with {orgs()[0].name} is on ")
        and "Still coming? " in t
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


def test_production_refuses_to_start_with_half_a_twilio_no_email_or_tasks_token():
    prod = {"CAUSEMATCH_ENV": "production"}
    assert notify.make_sms(prod) is None  # no Twilio at all: volunteers get email
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


# --- send windows, presence and retries -------------------------------------------------


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


# --- the SMS ceiling and the reminder cap -------------------------------------------------


def status_of(key):
    return notifications(where="key = ?", where_args=[key])[0].status


def at_the_ceiling(monkeypatch, anon):
    """A ceiling of one text an hour, already used by a code from Me's add-a-number."""
    rate_events.delete_where()
    monkeypatch.setenv("SMS_MAX_PER_HOUR", "1")
    assert add_number(adder(), "3105550140") == 303


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
    assert add_number(adder(), "3105550141") == 429


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


def test_accept_notice_links_to_the_chat(world):
    r = world.coord.post(
        f"/org/matches/{world.match.id}/decide",
        data={"decision": "accept"},
        follow_redirects=True,
    )
    assert "Accepted · " in r.text
    assert f'href="/org/chat/{world.match.id}"' in r.text


# --- email over SMTP (a Gmail account and its app password, for the beta) ---------------

APP_PASSWORD = "abcdefghijklmnop"  # 16 letters, as Gmail's
SMTP_ENV = {
    "SMTP_USERNAME": "causematch.example@gmail.com",
    "SMTP_PASSWORD": APP_PASSWORD,
}
RESEND = {"EMAIL_API_KEY": "k", "EMAIL_FROM": "hello@causematch.example"}


CONNECTIONS = []  # every FakeSMTP made, in order


class FakeSMTP:
    """smtplib.SMTP stand-in: records what the emailer does, no network. Its socket is
    one end of a local socketpair (see smtp_server). broken: the next send_message fails
    as a dropped connection would. Tests set hang (send_message blocks on the socket
    until it is shut) and greeting on the class."""

    hang, greeting = False, 220

    def __init__(self, host="", port=0, timeout=None):
        self.host, self.port, self.timeout, self.sock = None, None, timeout, None
        self.calls, self.messages = [], []
        self.broken = False
        CONNECTIONS.append(self)

    def getreply(self):
        self.calls.append("connect")
        return self.greeting, b"smtp.example ready"

    def starttls(self, context=None):
        self.calls.append(("starttls", context))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        if self.hang:
            self.sock.recv(1)  # until the deadline shuts the socket
            raise smtplib.SMTPServerDisconnected
        if self.broken:
            raise smtplib.SMTPServerDisconnected
        self.calls.append("send")
        self.messages.append(message)

    def quit(self):
        self.calls.append("quit")

    def close(self):
        self.calls.append("close")


def steps(server):
    return [c if isinstance(c, str) else c[0] for c in server.calls]


@pytest.fixture
def smtp_server(monkeypatch):
    """Every SMTPEmailer connects to a FakeSMTP over a local socketpair."""
    CONNECTIONS.clear()
    pairs = []

    def fake_open(self, deadline):
        CONNECTIONS[-1].host, CONNECTIONS[-1].port = self.host, self.port
        pairs.append(socket.socketpair())
        deadline.watch(pairs[-1][0])
        return pairs[-1][0]

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.SMTPEmailer, "_open", fake_open)
    yield CONNECTIONS
    for a, b in pairs:
        a.close()
        b.close()


def test_email_picks_the_fake_then_resend_then_smtp_and_production_needs_one():
    prod = {"CAUSEMATCH_ENV": "production"}
    dev_fake = {"CAUSEMATCH_ENV": "development", "CAUSEMATCH_FAKE_EMAIL": "1"}
    assert isinstance(notify.make_emailer({**dev_fake, **SMTP_ENV}), notify.FakeEmailer)
    both = notify.make_emailer({**prod, **RESEND, **SMTP_ENV})
    assert isinstance(both, notify.ResendEmailer)  # Resend wins: switching is config
    e = notify.make_emailer({**prod, **SMTP_ENV})
    assert isinstance(e, notify.SMTPEmailer)
    assert (e.host, e.port, e.sender) == (
        "smtp.gmail.com",
        587,
        SMTP_ENV["SMTP_USERNAME"],
    )
    named = "Causematch <causematch.example@gmail.com>"
    assert (
        notify.make_emailer({**prod, **SMTP_ENV, "EMAIL_FROM": named}).sender == named
    )
    for half in (
        {"SMTP_USERNAME": "causematch.example@gmail.com"},
        {"SMTP_PASSWORD": APP_PASSWORD},
        {"SMTP_HOST": "smtp.gmail.com"},
    ):
        for env in ({**prod, **half}, {**prod, **RESEND, **half}):
            with pytest.raises(RuntimeError, match="SMTP is half configured"):
                notify.make_emailer(env)
        with pytest.raises(RuntimeError, match="SMTP is half configured"):
            config.require_volunteer_sign_in({**prod, **half})
    with pytest.raises(RuntimeError, match="at least 16"):
        notify.make_emailer({**prod, **SMTP_ENV, "SMTP_PASSWORD": "short"})
    grouped = {**prod, **SMTP_ENV, "SMTP_PASSWORD": " abcd efgh\tijkl mnop\n"}
    assert notify.make_emailer(grouped).password == APP_PASSWORD  # as Google shows it
    with pytest.raises(RuntimeError, match="SMTP_PORT"):
        notify.make_emailer({**prod, **SMTP_ENV, "SMTP_PORT": "smtp"})
    with pytest.raises(RuntimeError, match="SMTP_MAX_PER_DAY"):
        notify.make_emailer({**prod, **SMTP_ENV, "SMTP_MAX_PER_DAY": "0"})
    with pytest.raises(RuntimeError, match="EMAIL_FROM"):
        notify.make_emailer({**prod, "EMAIL_API_KEY": "k"})
    with pytest.raises(RuntimeError, match="Email is not configured"):
        notify.make_emailer(prod)
    with pytest.raises(RuntimeError, match="CAUSEMATCH_ENV"):
        notify.make_emailer({**prod, **SMTP_ENV, "CAUSEMATCH_FAKE_EMAIL": "1"})
    config.require_volunteer_sign_in({**prod, **SMTP_ENV})  # SMTP alone is a way in
    with pytest.raises(RuntimeError, match="No volunteer sign-in"):
        config.require_volunteer_sign_in(prod)


def test_an_smtp_send_is_the_same_message_over_verified_starttls(
    smtp_server, monkeypatch
):
    e = notify.make_emailer({"CAUSEMATCH_ENV": "production", **SMTP_ENV})
    e.send("vol@example.org", "Subject", "Body", {"List-Unsubscribe": "<https://x>"})
    (server,) = smtp_server
    assert (server.host, server.port, server.timeout) == ("smtp.gmail.com", 587, 10)
    assert steps(server) == ["connect", "starttls", "login", "send", "quit", "close"]
    tls = server.calls[1]
    assert tls[1].verify_mode == ssl.CERT_REQUIRED and tls[1].check_hostname
    assert server._host == "smtp.gmail.com"  # the name the certificate must carry
    login = server.calls[2]
    assert login == ("login", "causematch.example@gmail.com", APP_PASSWORD)
    (m,) = server.messages
    assert (m["To"], m["From"], m["Subject"]) == (
        "vol@example.org",
        "causematch.example@gmail.com",
        "Subject",
    )
    assert m["List-Unsubscribe"] == "<https://x>"
    assert m.get_content() == "Body\n"
    # the sign-in link goes out with Resend's subject and text
    resend = notify.ResendEmailer("k", "hello@causematch.example")
    posted = []
    resend.client.post = lambda path, json: (
        posted.append(json) or SimpleNamespace(raise_for_status=lambda: None)
    )
    for emailer in (e, resend):
        monkeypatch.setattr(notify, "_emailer", emailer)
        link = notify.send_volunteer_link("vol@example.org", "tok", new=True)
    m = smtp_server[-1].messages[0]
    assert m["Subject"] == posted[0]["subject"] == "Finish signing up for Causematch"
    assert (
        m.get_content().rstrip("\n") == posted[0]["text"] and link in posted[0]["text"]
    )
    # port 465: TLS from the start, with the same verified context, and no STARTTLS
    smtp_server.clear()
    tls_e = notify.make_emailer(
        {"CAUSEMATCH_ENV": "production", **SMTP_ENV, "SMTP_PORT": "465"}
    )
    tls_e.send("vol@example.org", "Subject", "Body")
    (server,) = smtp_server
    assert tls_e.context.verify_mode == ssl.CERT_REQUIRED and server.port == 465
    assert steps(server) == ["connect", "login", "send", "quit", "close"]
    # a greeting that isn't 220 is refused before anything else
    smtp_server.clear()
    monkeypatch.setattr(FakeSMTP, "greeting", 554)
    with pytest.raises(smtplib.SMTPConnectError):
        e.send("vol@example.org", "Subject", "Body")
    assert steps(smtp_server[0]) == ["connect", "close"]


def test_a_failed_smtp_send_logs_its_type_only(anon, smtp_server, monkeypatch, caplog):
    _, email = coordinator_for(0, "known@example.org")

    def refuse(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, f"bad {user} {password}".encode())

    monkeypatch.setattr(FakeSMTP, "login", refuse)
    monkeypatch.setattr(
        notify, "_emailer", notify.make_emailer({"CAUSEMATCH_ENV": "test", **SMTP_ENV})
    )
    with caplog.at_level(logging.DEBUG):
        r = anon.post("/org/login/send", data={"email": email})
    assert r.status_code == 200
    assert "sending a sign-in link failed: SMTPAuthenticationError" in caplog.text
    for secret in (APP_PASSWORD, "token=", "/org/login/verify", email):
        assert secret not in caplog.text


def refused(caplog, send, share=None):
    """send() is refused by the ceiling, logged by its share's name (None: the whole)."""
    caplog.clear()
    with caplog.at_level(logging.WARNING), pytest.raises(notify.EmailCeiling):
        send()
    name = f"email-smtp-{share}-day" if share else "email-smtp-day"
    assert f"rate limit reached: {name}" in caplog.text


def test_smtp_keeps_room_for_sign_in_links_under_its_daily_ceiling(
    smtp_server, monkeypatch, caplog
):
    """Of 8: everything but staff 7 (int of 90%); inside it notices 4 (urgent calls' 2
    among them) and sign-ups 2; staff the whole."""
    env = {"CAUSEMATCH_ENV": "test", **SMTP_ENV, "SMTP_MAX_PER_DAY": "8"}
    e = notify.make_emailer(env)
    monkeypatch.setattr(notify, "_emailer", e)
    for to in ("a", "b"):
        notify.send_volunteer_link(f"{to}@example.org", "tok", new=True)
    signup = lambda: notify.send_volunteer_link("c@example.org", "tok", True)
    refused(caplog, signup, "signups")
    for to in ("a", "b"):
        e.send(f"{to}@example.org", "s", "b", kind="urgent")
    refused(caplog, lambda: e.send("c@example.org", "s", "b", kind="urgent"), "urgent")
    for to in ("a", "b"):
        e.send(f"{to}@example.org", "s", "b")
    refused(caplog, lambda: e.send("c@example.org", "s", "b"), "notices")
    # a volunteer's link to their account: what's left of the public share
    notify.send_volunteer_link("vol@example.org", "tok", new=False)
    signin = lambda: notify.send_volunteer_link("vol@example.org", "tok", False)
    refused(caplog, signin, "public")
    # staff's links still go, up to the whole ceiling
    notify.send_login_link("coord@example.org", "tok")
    refused(caplog, lambda: notify.send_login_link("coord@example.org", "tok"))
    assert len(smtp_server) == 8  # no refusal connected
    assert config.smtp_max_per_day({}) == 400
    for host in ("smtp.gmail.com", "SMTP.Gmail.COM", ""):
        with pytest.raises(RuntimeError, match="at most 450"):
            config.smtp_max_per_day({"SMTP_MAX_PER_DAY": "451", "SMTP_HOST": host})
    other = {"SMTP_MAX_PER_DAY": "501", "SMTP_HOST": "smtp.example.org"}
    assert config.smtp_max_per_day(other) == 501


def test_a_flood_of_volunteer_links_never_blocks_a_staff_link(
    smtp_server, monkeypatch, caplog
):
    e = notify.make_emailer(
        {"CAUSEMATCH_ENV": "test", **SMTP_ENV, "SMTP_MAX_PER_DAY": "10"}
    )
    monkeypatch.setattr(notify, "_emailer", e)
    for i in range(9):  # attacker-owned accounts' links: the public share is 9
        notify.send_volunteer_link(f"v{i}@catchall.example", "tok", new=False)
    signin = lambda: notify.send_volunteer_link("v9@catchall.example", "tok", False)
    refused(caplog, signin, "public")
    notify.send_login_link("coord@example.org", "tok")  # staff's floor
    assert len(smtp_server) == 10


def test_through_gmail_login_allows_a_mailbox_five_links_a_day(
    anon, smtp_server, monkeypatch
):
    clock = [1_900_000_000.0]
    monkeypatch.setattr(
        security,
        "time",
        SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic),
    )

    def ask():
        return anon.post("/login/send", data={"email": "flood@example.org"})

    for _ in range(5):
        assert ask().status_code == 200
    clock[0] += 2 * 3600  # past the hourly per-mailbox-and-address limit
    assert ask().status_code == 200  # 20 a day without Gmail
    monkeypatch.setattr(
        notify, "_emailer", notify.make_emailer({"CAUSEMATCH_ENV": "test", **SMTP_ENV})
    )
    rate_events.delete_where()
    for _ in range(5):
        assert ask().status_code == 200
    clock[0] += 2 * 3600
    assert ask().status_code == 429  # 5 a day through Gmail


def test_a_batch_signs_in_once_and_reconnects_after_a_dropped_send(
    smtp_server, monkeypatch
):
    e = notify.make_emailer({"CAUSEMATCH_ENV": "test", **SMTP_ENV})
    monkeypatch.setattr(notify, "_emailer", e)
    with notify.batch():
        e.send("a@example.org", "s", "b")
        e.send("b@example.org", "s", "b")
        smtp_server[0].broken = True
        with pytest.raises(smtplib.SMTPServerDisconnected):
            e.send("c@example.org", "s", "b")
        e.send("d@example.org", "s", "b")  # a new connection
    first, second = smtp_server
    assert steps(first) == ["connect", "starttls", "login", "send", "send", "close"]
    assert steps(second) == ["connect", "starttls", "login", "send", "quit", "close"]


def test_an_orgs_coordinators_get_a_like_over_one_connection(
    smtp_server, clock, monkeypatch
):
    coordinator_for(0, "one@example.org")
    coordinator_for(0, "two@example.org")
    vol = volunteer("Zelda")
    monkeypatch.setattr(
        notify, "_emailer", notify.make_emailer({"CAUSEMATCH_ENV": "test", **SMTP_ENV})
    )
    like(vol)
    (server,) = smtp_server
    assert sorted(m["To"] for m in server.messages) == [
        "one@example.org",
        "two@example.org",
    ]
    assert steps(server).count("login") == 1


@pytest.mark.parametrize("tls_from_the_start", [False, True])
def test_an_smtp_connect_gives_up_at_its_deadline(tls_from_the_start, monkeypatch):
    """A server that takes the connection and never greets (587) or never finishes the
    TLS handshake (465): the send fails after SMTP_DEADLINE, not SMTP_TIMEOUT per read,
    as SMTPUnavailable (nothing went: retried), and connects stop for SMTP_BREAK.
    Loopback only."""
    monkeypatch.setattr(notify, "SMTP_DEADLINE", 0.3)
    monkeypatch.setattr(socket, "getfqdn", lambda *a: "test.invalid")  # no DNS
    with socket.create_server(("127.0.0.1", 0)) as silent:  # never accepts or speaks
        port = silent.getsockname()[1]
        monkeypatch.setattr(
            notify, "SMTP_TLS_PORT", port if tls_from_the_start else 465
        )
        monkeypatch.setattr(security, "allow", lambda *a: True)
        e = notify.SMTPEmailer("127.0.0.1", port, "u", APP_PASSWORD, "u", 10)
        began = time.monotonic()
        with pytest.raises(notify.SMTPUnavailable):
            e.send("a@example.org", "s", "b")
        assert time.monotonic() - began < 2
        assert e._no_connect_until > time.monotonic() + 50


def test_a_send_the_deadline_cuts_is_unknown_and_later_sends_wait(
    smtp_server, monkeypatch
):
    monkeypatch.setattr(notify, "SMTP_DEADLINE", 0.3)
    e = notify.make_emailer({"CAUSEMATCH_ENV": "test", **SMTP_ENV})
    monkeypatch.setattr(notify, "_emailer", e)
    monkeypatch.setattr(FakeSMTP, "hang", True)
    with pytest.raises(TimeoutError):  # it may have gone: "unknown", never retried
        e.send("a@example.org", "s", "b")
    assert len(smtp_server) == 1
    events = len(rate_events())
    with pytest.raises(notify.SMTPUnavailable):  # the break: no connect, no ceiling
        e.send("b@example.org", "s", "b")
    assert len(smtp_server) == 1 and len(rate_events()) == events
    nid = []
    monkeypatch.setattr(data, "notification_done", lambda n, status: nid.append(status))
    monkeypatch.setattr(data, "notification_claim", lambda *a: 1)
    notify._deliver("likes", "likes:x", 1, lambda: e.send("c@example.org", "s", "b"))
    assert nid == ["failed"]  # for the sweep to retry
    e._no_connect_until = 0  # a minute later
    monkeypatch.setattr(FakeSMTP, "hang", False)
    e.send("d@example.org", "s", "b")
    assert steps(smtp_server[-1])[-3:] == ["send", "quit", "close"]


def test_a_batch_drops_its_connection_when_the_deadline_hit_just_after(
    smtp_server, monkeypatch
):
    e = notify.make_emailer({"CAUSEMATCH_ENV": "test", **SMTP_ENV})
    monkeypatch.setattr(notify, "_emailer", e)
    done = notify._Deadline.done

    def late(self):  # the timer fired as the send finished
        done(self)
        self.hit = True

    monkeypatch.setattr(notify._Deadline, "done", late)
    with notify.batch():
        e.send("a@example.org", "s", "b")
        monkeypatch.setattr(notify._Deadline, "done", done)
        e.send("b@example.org", "s", "b")
    first, second = smtp_server
    assert steps(first)[-2:] == ["send", "close"]  # dropped, no QUIT
    assert steps(second)[-3:] == ["send", "quit", "close"]


def run_alone(coro):
    """asyncio.run(coro) in a thread of its own: the e2e tests leave an event loop running
    in this one. Its exception, CancelledError included, is raised here."""
    out = {}

    def run():
        try:
            asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001  handed to the test's thread
            out["error"] = e

    t = threading.Thread(target=run)
    t.start()
    t.join()
    if "error" in out:
        raise out["error"]


def test_notices_a_request_queues_go_out_after_its_response(monkeypatch):
    events = []

    async def app(scope, receive, send):
        notify.after(SimpleNamespace(scope=scope), events.append, "notice")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        events.append("handler done")

    async def send(message):
        events.append(message["type"])

    run_alone(notify.RunAfter(app)({"type": "http"}, None, send))
    assert events == [
        "http.response.start",
        "http.response.body",
        "handler done",
        "notice",
    ]
    notify.after(SimpleNamespace(scope={}), events.append, "at once")  # no RunAfter
    assert events[-1] == "at once"


def test_notices_after_the_response_have_their_own_threads(monkeypatch, caplog):
    monkeypatch.setattr(notify, "AFTER_LIMITER", anyio.CapacityLimiter(1))
    seen = []

    def notice():
        seen.append(
            anyio.from_thread.run_sync(lambda: notify.AFTER_LIMITER.borrowed_tokens)
        )

    async def app(scope, receive, send):
        notify.after(SimpleNamespace(scope=scope), notice)

    run_alone(notify.RunAfter(app)({"type": "http"}, None, None))
    assert seen == [1]  # it held the notices' own token, not the routes' pool
    # the server stopping first: the count is logged and the stop goes through

    async def stopped(*a, **kw):
        raise asyncio.CancelledError

    async def queues_two(scope, receive, send):
        notify.after(SimpleNamespace(scope=scope), notice)
        notify.after(SimpleNamespace(scope=scope), notice)

    monkeypatch.setattr(notify.anyio.to_thread, "run_sync", stopped)
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        run_alone(notify.RunAfter(queues_two)({"type": "http"}, None, None))
    assert "up to 2 notices not sent: the server is stopping" in caplog.text


def test_a_sockets_notices_run_beside_its_frames_and_its_end_waits(caplog):
    events, go = [], threading.Event()

    def notice():
        go.wait(5)
        events.append("notice")

    async def app(scope, receive, send):
        notify.after(SimpleNamespace(scope=scope), notice)
        events.append("next frame")  # not held by the notice
        go.set()

    run_alone(notify.RunAfter(app)({"type": "websocket"}, None, None))
    assert events == ["next frame", "notice"]  # and the socket's end waited for it

    async def stops(scope, receive, send):  # the server stopping, a notice still queued
        notify.after(SimpleNamespace(scope=scope), notice)
        raise asyncio.CancelledError

    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        run_alone(notify.RunAfter(stops)({"type": "websocket"}, None, None))
    assert "up to 1 notices not sent: the server is stopping" in caplog.text


def test_the_apps_routes_queue_their_notices_after_the_response(world, monkeypatch):
    queued, real = [], notify.after
    monkeypatch.setattr(
        notify,
        "after",
        lambda req, fn, *a: (
            queued.append(notify.AFTER in req.scope),
            real(req, fn, *a),
        ),
    )
    accept(world.coord, world.match)
    assert queued == [True] and len(texts()) == 1  # queued, and sent once out


def test_one_volunteers_likes_email_the_org_up_to_a_daily_cap(clock, monkeypatch):
    monkeypatch.setattr(security, "LIKE_NOTICES", (("like-notice-day", 1, 24 * 3600),))
    coordinator()
    copy = {**pantry_opening().__dict__, "id": None}
    second, third = (
        openings.insert(openings.dataclass()(**{**copy, "title": t}))
        for t in ("Second", "Third")
    )
    vol = volunteer("Zelda")
    like(vol)
    assert len(emails()) == 1
    clock.advance(minutes=16)  # past the org's window
    like(vol, second)
    assert len(emails()) == 1  # past this volunteer's cap: only the inbox shows it
    clock.advance(minutes=16)
    like(volunteer("Yuri"), third)
    assert len(emails()) == 2  # another volunteer's still emails


def test_through_gmail_org_login_allows_a_mailbox_five_links_a_day(
    anon, smtp_server, monkeypatch
):
    clock = [1_900_000_000.0]
    monkeypatch.setattr(
        security,
        "time",
        SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic),
    )
    _, email = coordinator_for(0, "staff@example.org")
    monkeypatch.setattr(
        notify, "_emailer", notify.make_emailer({"CAUSEMATCH_ENV": "test", **SMTP_ENV})
    )
    for _ in range(5):
        assert anon.post("/org/login/send", data={"email": email}).status_code == 200
    clock[0] += 2 * 3600  # past the hourly per-mailbox-and-address limit
    r = anon.post("/org/login/send", data={"email": email})
    assert r.status_code == 429 and len(smtp_server) == 5  # the staff floor is kept


def test_a_refused_password_stops_connects_for_ten_minutes(smtp_server, monkeypatch):
    def refuse(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 not accepted")

    monkeypatch.setattr(FakeSMTP, "login", refuse)
    e = notify.make_emailer({"CAUSEMATCH_ENV": "test", **SMTP_ENV})
    with pytest.raises(smtplib.SMTPAuthenticationError):  # logged by type: the alert
        e.send("a@example.org", "s", "b")
    assert e._no_connect_until > time.monotonic() + 9 * 60
    with pytest.raises(notify.SMTPUnavailable):
        e.send("b@example.org", "s", "b")
    assert len(smtp_server) == 1  # no second sign-in attempt
