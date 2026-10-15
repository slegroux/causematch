"""Urgent calls: a nonprofit short of people for a shift in the next 48 hours."""

import itertools
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import coordinator_for, org_sign_in
from starlette.testclient import TestClient

from app import data, notify
from app.db import (
    Profile,
    matches,
    notifications,
    openings,
    orgs,
    profiles,
    shifts,
    urgent_calls,
)
from main import app

LA = ZoneInfo("America/Los_Angeles")
DOGS = "Walk dogs at the shelter"  # org 3's opening, 9 days out, 5 spots


def local(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def tomorrow(hhmm="09:00"):
    return f"{(datetime.now(LA) + timedelta(days=1)).date()}T{hhmm}"


def in_hours(h):
    return local(datetime.now(LA) + timedelta(hours=h))


@pytest.fixture
def shelter():
    """Signed-in coordinator of org 3 (Eastside Paws Rescue), with its dog-walking opening."""
    uid, email = coordinator_for(3)
    c = TestClient(app)
    org_sign_in(c, email)
    c.uid = uid
    c.opening = openings(where="title = ?", where_args=[DOGS])[0]
    assert c.opening.org_id == orgs()[3].id
    return c


def post_call(c, starts_at=None, needed="2", auto=False, opening=None):
    body = {"starts_at": starts_at or tomorrow(), "needed": needed}
    if auto:
        body["auto_accept"] = "1"
    oid = (opening or c.opening).id
    return c.post(f"/org/openings/{oid}/urgent", data=body, follow_redirects=False)


def uid_of(user):
    return data.get_or_create_volunteer(user.phone)


def test_post_puts_the_call_first_in_the_deck_with_its_badge(shelter, user):
    before = data.deck(uid_of(user), 50)
    assert before[0]["title"] != DOGS and DOGS in [c["title"] for c in before]
    assert not any(c["urgent"] for c in before)
    r = post_call(shelter)
    assert r.status_code == 303 and r.headers["location"] == "/org"
    (call,) = urgent_calls()
    assert (call.opening_id, call.starts_at, call.needed, call.auto_accept) == (
        shelter.opening.id,
        tomorrow(),
        2,
        False,
    )
    top = data.deck(uid_of(user), 50)[0]
    assert top["title"] == DOGS and top["starts_at"] == tomorrow()
    assert top["urgent"] == "Urgent · tomorrow 9 am · needs 2"
    page = user.get("/deck")
    assert DOGS in page.text and "Urgent · tomorrow 9 am · needs 2" in page.text


def test_deck_today_also_shows_the_call_first(shelter, user):
    post_call(shelter, starts_at=in_hours(30))
    cards = data.deck(uid_of(user), 50, today=True)
    assert cards[0]["title"] == DOGS and cards[0]["urgent"].startswith("Urgent · ")


def test_deck_label_says_today_and_minutes():
    assert data.urgent_label(local(datetime.now(LA)), 1).startswith("Urgent · today ")
    soon = datetime.now(LA) + timedelta(days=1)
    assert (
        data.urgent_label(f"{soon.date()}T14:30", 3)
        == "Urgent · tomorrow 2:30 pm · needs 3"
    )


def test_deck_shows_a_call_on_an_opening_whose_first_shift_passed(shelter, user):
    """A weekly opening started last week: its call brings it back, at the call's time."""
    openings.update(
        id=shelter.opening.id, starts_at=local(datetime.now(LA) - timedelta(days=3))
    )
    assert DOGS not in [c["title"] for c in data.deck(uid_of(user), 50)]
    assert post_call(shelter).status_code == 303
    top = data.deck(uid_of(user), 50)[0]
    assert top["title"] == DOGS and top["starts_at"] == tomorrow()


def test_deck_like_on_an_urgent_card_counts_toward_the_call(shelter, user):
    post_call(shelter)
    r = user.post("/swipe", data={"opening_id": str(shelter.opening.id), "liked": "1"})
    assert r.status_code == 200
    (m,) = matches(where="opening_id = ?", where_args=[shelter.opening.id])
    assert m.urgent_call_id == urgent_calls()[0].id and m.status == "pending"


def test_deck_like_without_a_call_carries_none(shelter, user):
    user.post("/swipe", data={"opening_id": str(shelter.opening.id), "liked": "1"})
    (m,) = matches(where="opening_id = ?", where_args=[shelter.opening.id])
    assert m.urgent_call_id is None


def test_deck_hides_a_call_from_a_blocked_org(shelter, user):
    """The deck's usual rules hold for an urgent card: a blocked org stays hidden."""
    post_call(shelter)
    uid = uid_of(user)
    data._add_block(blocker_id=uid, blocked_org_id=shelter.opening.org_id)
    assert DOGS not in [c["title"] for c in data.deck(uid, 50)]


@pytest.mark.parametrize("hours", [72, 49, -1])
def test_post_refuses_a_time_outside_the_next_48_hours(shelter, hours):
    r = post_call(shelter, starts_at=in_hours(hours))
    assert r.status_code == 200 and "Pick a time in the next 48 hours." in r.text
    assert not urgent_calls()


def test_post_refuses_a_garbled_time_or_number(shelter):
    for body in (
        {"starts_at": "tomorrow", "needed": "2"},
        {"starts_at": tomorrow(), "needed": "two"},
        {"starts_at": tomorrow(), "needed": "0"},
        {"starts_at": tomorrow(), "needed": "2", "auto_accept": "yes"},
    ):
        r = shelter.post(
            f"/org/openings/{shelter.opening.id}/urgent", data=body,
            follow_redirects=False,
        )  # fmt: skip
        assert r.status_code == 200, body
    assert not urgent_calls()


def test_post_refuses_more_than_the_open_spots(shelter):
    r = post_call(shelter, needed="6")  # 5 spots
    assert "How many more people: a number from 1 to 5." in r.text
    assert not urgent_calls()
    assert post_call(shelter, needed="5").status_code == 303


def test_post_refuses_a_fourth_call_in_a_day(shelter):
    oid = shelter.opening.id
    for _ in range(data.URGENT_PER_DAY):
        assert post_call(shelter).status_code == 303
        stop = shelter.post(f"/org/openings/{oid}/urgent/close", follow_redirects=False)
        assert stop.status_code == 303
    r = post_call(shelter)
    assert r.status_code == 200 and "You can post 3 urgent calls a day." in r.text
    assert len(urgent_calls()) == 3
    # a day later the org may post again
    urgent_calls.update(id=urgent_calls()[0].id, created_at="2020-01-01T00:00:00+00:00")
    assert post_call(shelter).status_code == 303


def test_post_refuses_a_second_open_call_on_one_opening(shelter):
    assert post_call(shelter).status_code == 303
    r = post_call(shelter, starts_at=tomorrow("15:00"))
    assert "already has an urgent call" in r.text
    assert len(urgent_calls()) == 1


def test_post_refuses_a_closed_opening(shelter):
    shelter.post(f"/org/openings/{shelter.opening.id}/close")
    r = post_call(shelter)
    assert r.status_code == 200 and "filled or closed" in r.text
    assert not urgent_calls()


def test_post_page_prefills_a_soon_opening_start(shelter):
    page = shelter.get(f"/org/openings/{shelter.opening.id}/urgent")
    assert page.status_code == 200 and "Urgent call" in page.text
    assert 'value=""' in page.text  # 9 days out: no default time
    openings.update(id=shelter.opening.id, starts_at=tomorrow("10:00"))
    page = shelter.get(f"/org/openings/{shelter.opening.id}/urgent")
    assert f'value="{tomorrow("10:00")}"' in page.text


def test_close_by_the_org_takes_it_out_of_the_deck(shelter, user):
    post_call(shelter)
    dash = shelter.get("/org")
    assert "Urgent · tomorrow 9 am · needs 2" in dash.text
    assert "0 of 2 found" in dash.text and "Stop urgent call" in dash.text
    r = shelter.post(
        f"/org/openings/{shelter.opening.id}/urgent/close", follow_redirects=False
    )
    assert r.status_code == 303
    (call,) = urgent_calls()
    assert call.status == "closed" and call.ended_at
    top = data.deck(uid_of(user), 50)
    assert not any(c["urgent"] for c in top)
    assert "Post an urgent call" in shelter.get("/org").text


def test_close_when_its_time_passes(shelter, user):
    post_call(shelter)
    urgent_calls.update(id=urgent_calls()[0].id, starts_at=in_hours(-1))
    assert not any(c["urgent"] for c in data.deck(uid_of(user), 50))
    assert urgent_calls()[0].status == "closed"
    assert "Stop urgent call" not in shelter.get("/org").text


def test_close_when_filled_books_the_call_time(shelter, user):
    post_call(shelter, needed="1")
    uid = uid_of(user)
    m = data.swipe(uid, shelter.opening.id, True)
    data.decide(shelter.uid, m.id, True)
    call = urgent_calls()[0]
    assert call.status == "filled" and call.ended_at
    (shift,) = shifts(where="match_id = ?", where_args=[m.id])
    assert shift.starts_at == tomorrow()  # the call's shift, not the opening's
    assert not any(c["urgent"] for c in data.deck(uid, 50))


def test_close_when_its_opening_closes(shelter):
    post_call(shelter)
    shelter.post(f"/org/openings/{shelter.opening.id}/close")
    assert urgent_calls()[0].status == "closed"


def test_close_when_its_opening_fills(shelter):
    """Normal likes can fill the opening before the call: the call is filled too."""
    post_call(shelter, needed="1")
    openings.update(id=shelter.opening.id, status="filled")
    assert data.org_urgent_calls(shelter.uid) == {}
    assert urgent_calls()[0].status == "filled"


def test_deck_demo_seeds_an_urgent_call_first_in_the_deck(user):
    """scripts/demo.py's call: on the invented seed it adds the org's second opening."""
    from test_ui import _demo

    coord, _ = coordinator_for(0)
    org = orgs()[0]
    first = openings(where="org_id = ? AND status = 'open'", where_args=[org.id])[0]
    oid = _demo()._urgent_call(coord, org, first)
    assert oid != first.id
    (call,) = urgent_calls()
    assert (call.opening_id, call.needed, call.auto_accept) == (oid, 2, True)
    top = data.deck(uid_of(user), 50)[0]
    assert top["id"] == oid and top["urgent"] == "Urgent · tomorrow 9 am · needs 2"


# --- the volunteer's opt-in (/me/preferences) -----------------------------------------

PREFS = {
    "cause_food-insecurity": "love",
    "availability": ["sat_morning"],
    "frequency": "weekly",
    "place1_label": "Home",
    "place1_where": "Chinatown",
    "place1_mode": "car",
    "place1_minutes": "30",
}
OPT_IN = {
    "sections": "urgent",
    "urgent_on": "1",
    "urgent_window": "today",
    "urgent_quiet_from": "22",
    "urgent_quiet_to": "7",
    "urgent_per_week": "2",
}


def save_prefs(user, **over):
    body = {**PREFS, **OPT_IN, **over}
    return user.post("/me/preferences", data=body, follow_redirects=False)


def test_optin_is_off_by_default(user):
    assert data.preferences(uid_of(user))["urgent"] == {
        "on": False,
        "window": "48h",
        "quiet_from": 21,
        "quiet_to": 8,
        "per_week": 3,
    }
    page = user.get("/me/preferences").text
    assert "Last-minute calls" in page and "Tell me about last-minute calls" in page
    assert 'name="urgent_on" value="1" class' in page  # unchecked
    assert '<option value="21" selected>9 pm</option>' in page
    assert '<option value="8" selected>8 am</option>' in page


def test_optin_saves_and_shows(user):
    r = save_prefs(user)
    assert r.status_code == 303 and r.headers["location"] == "/me/preferences?saved=1"
    assert data.preferences(uid_of(user))["urgent"] == {
        "on": True,
        "window": "today",
        "quiet_from": 22,
        "quiet_to": 7,
        "per_week": 2,
    }
    page = user.get("/me/preferences").text
    assert 'name="urgent_on" value="1" checked' in page
    assert '<option value="22" selected>10 pm</option>' in page
    # turned off again
    body = {**PREFS, **OPT_IN}
    del body["urgent_on"]
    user.post("/me/preferences", data=body)
    assert data.preferences(uid_of(user))["urgent"]["on"] is False


def test_optin_a_page_without_the_section_keeps_it(user):
    save_prefs(user)
    r = user.post("/me/preferences", data=PREFS, follow_redirects=False)
    assert r.status_code == 303
    assert data.preferences(uid_of(user))["urgent"]["on"] is True


@pytest.mark.parametrize(
    "over",
    [
        {"urgent_window": "week"},
        {"urgent_per_week": "4"},
        {"urgent_per_week": "0"},
        {"urgent_quiet_from": "24"},
        {"urgent_quiet_to": "-1"},
        {"urgent_quiet_to": ""},
        {"urgent_on": "yes"},
    ],
)
def test_optin_refuses_values_off_the_page(user, over):
    r = save_prefs(user, **over)
    assert r.status_code == 200 and "wasn't recognized" in r.text
    assert data.preferences(uid_of(user))["urgent"]["on"] is False


def test_optin_refuses_quiet_hours_that_start_when_they_end(user):
    r = save_prefs(user, urgent_quiet_from="9", urgent_quiet_to="9")
    assert "Quiet hours need a start and an end that differ." in r.text
    assert data.preferences(uid_of(user))["urgent"]["on"] is False


def test_optin_quiet_hours_wrap_past_midnight():
    from app.prefs import quiet_now

    night = {"quiet_from": 21, "quiet_to": 8}
    assert [h for h in range(24) if quiet_now(night, h)] == [
        0, 1, 2, 3, 4, 5, 6, 7, 21, 22, 23
    ]  # fmt: skip
    day = {"quiet_from": 9, "quiet_to": 17}
    assert [h for h in range(24) if quiet_now(day, h)] == list(range(9, 17))


def test_optin_is_private(shelter, user):
    """A nonprofit the volunteer liked sees nothing of it."""
    save_prefs(user)
    user.post("/swipe", data={"opening_id": str(shelter.opening.id), "liked": "1"})
    inbox = shelter.get("/org/inbox").text
    assert "Sam" in inbox  # the like is there
    for word in ("Last-minute", "last-minute", "quiet", "10 pm"):
        assert word not in inbox


# --- notification waves (/tasks/urgent) -----------------------------------------------

TOKEN = "scheduler-secret"
SHELTER = (34.0335, -118.2087)  # the dog-walking opening, Boyle Heights
_phones = itertools.count(1)


def call_day():
    return (datetime.now(LA) + timedelta(days=1)).date()


def call_start():
    """The calls below: tomorrow 1 pm."""
    return f"{call_day()}T13:00"


def at(hour=11, day=None):
    """A sweep's time: `day` (default the call's) at `hour`, Los Angeles."""
    d = day or call_day()
    return datetime(d.year, d.month, d.day, hour, tzinfo=LA).astimezone(UTC)


@pytest.fixture
def sweep(monkeypatch):
    """Runs /tasks/urgent as Cloud Scheduler does, at a set time (default 11 am the day
    of the call). sweep.at moves the clock."""
    monkeypatch.setenv("TASKS_TOKEN", TOKEN)
    notify.get_sms().sent.clear()

    class Sweep:
        when = at()

        def __call__(self, **headers):
            r = TestClient(app).post(
                "/tasks/urgent",
                headers=headers or {"Authorization": f"Bearer {TOKEN}"},
            )
            return r

    s = Sweep()
    monkeypatch.setattr(notify, "now", lambda: s.when)
    return s


def opted_in(lat=SHELTER[0], lon=SHELTER[1], causes=None, **kw):
    """An onboarded volunteer near the shelter, opted in to last-minute calls, free on the
    call's day; kw overrides any Profile field."""
    n = next(_phones)
    fields = {
        "role": "volunteer",
        "phone": f"+1213555{n:04d}",
        "display_name": f"Vol{n}",
        "session_version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "onboarded_at": "2026-01-01T00:00:00+00:00",
        "lat": lat,
        "lon": lon,
        "radius_km": 16,  # 30 minutes by car
        "cause_prefs": json.dumps(causes or {"animal-shelters": "love"}),
        "availability": "[]",
        "free_dates": json.dumps([call_day().isoformat()]),
        "urgent_on": True,
        **kw,
    }
    return profiles.insert(Profile(**fields)).id


def notified():
    """The volunteers an urgent notice was sent to."""
    rows = notifications(where="kind = 'urgent' AND status = 'sent'")
    return {r.recipient_id for r in rows}


def texted():
    return {p for p, _ in notify.get_sms().sent}


def phone(uid):
    return profiles[uid].phone


def call_on(shelter, needed=1, starts_at=None):
    assert post_call(shelter, starts_at=starts_at or call_start(),
                     needed=str(needed)).status_code == 303  # fmt: skip
    return urgent_calls()[-1]


def test_wave_one_texts_a_link_to_the_card(shelter, sweep):
    vol = opted_in()
    call_on(shelter)
    r = sweep()
    assert r.status_code == 200 and r.text == "sent 1"
    ((to, text),) = notify.get_sms().sent
    assert to == phone(vol)
    assert text == (
        "Causematch: Eastside Paws Rescue needs 1 more person today 1 pm for Walk "
        f"dogs at the shelter. Interested? https://causematch.test/deck?card="
        f"{shelter.opening.id} (Turn these off in Preferences. Reply STOP to end all "
        "texts.)"
    )
    (n,) = notifications(where="kind = 'urgent'")
    assert (n.key, n.recipient_id, n.status) == (
        f"urgent:{urgent_calls()[0].id}:{vol}",
        vol,
        "sent",
    )
    assert urgent_calls()[0].waves == 1


def test_wave_one_is_three_times_the_people_needed_best_fit_first(shelter, sweep):
    loved = [opted_in() for _ in range(4)]
    open_ = [opted_in(causes={"animal-shelters": "open"}) for _ in range(4)]
    call_on(shelter, needed=1)
    assert sweep().text == "sent 3"
    assert notified() < set(loved) and not notified() & set(open_)


def test_wave_one_is_capped_at_25(shelter, sweep):
    vols = [opted_in() for _ in range(30)]
    call_on(shelter, needed=5)  # 5 open spots: 15
    assert sweep().text == "sent 15"
    urgent_calls.update(id=urgent_calls()[0].id, needed=10)  # 30, capped
    notifications.delete_where()
    urgent_calls.update(id=urgent_calls()[0].id, waves=0)
    assert sweep().text == "sent 25"
    assert len(notified()) == 25 and notified() <= set(vols)


def test_wave_two_after_30_minutes_reaches_new_people_only(shelter, sweep):
    vols = [opted_in() for _ in range(5)]
    call_on(shelter, needed=1)
    assert sweep().text == "sent 3"
    first = notified()
    sweep.when += timedelta(minutes=20)
    assert sweep().text == "sent 0"  # too soon
    sweep.when += timedelta(minutes=10)
    assert sweep().text == "sent 2"  # the 2 not texted yet
    assert notified() == set(vols) and len(notify.get_sms().sent) == 5
    assert len(texted()) == 5 and first < notified()  # no one twice
    assert urgent_calls()[0].waves == 2
    opted_in()
    sweep.when += timedelta(hours=1)
    assert sweep().text == "sent 0"  # no third wave


def test_wave_two_skipped_once_filled(shelter, sweep):
    vols = [opted_in() for _ in range(5)]
    call_on(shelter, needed=1)
    sweep()
    m = data.swipe(vols[0], shelter.opening.id, True)
    data.decide(shelter.uid, m.id, True)
    sweep.when += timedelta(minutes=40)
    assert sweep().text == "sent 0" and urgent_calls()[0].status == "filled"


def test_wave_due_is_claimed_once(shelter, sweep):
    """Two sweeps at once (a scheduler retry) never send the same wave twice."""
    call_on(shelter)
    assert len(data.urgent_waves_due(sweep.when)) == 1
    assert data.urgent_waves_due(sweep.when) == []


def block_org(uid, org_id):
    data._add_block(blocker_id=uid, blocked_org_id=org_id)


def block_volunteer(uid, org_id):
    data._add_block(blocker_id=uid, blocked_user_id=uid, blocked_org_id=org_id)


EXCLUDED = {
    "not opted in": lambda s: opted_in(urgent_on=False),
    "quiet hours": lambda s: opted_in(urgent_quiet_from=10, urgent_quiet_to=12),
    "quiet hours past midnight": lambda s: opted_in(
        urgent_quiet_from=9, urgent_quiet_to=8
    ),
    "same day only": lambda s: opted_in(urgent_window="today"),
    "far away": lambda s: opted_in(lat=34.25, lon=-118.60),  # Chatsworth, 16 km reach
    "cause not for me": lambda s: opted_in(causes={"animal-shelters": "no"}),
    "not free": lambda s: opted_in(free_dates="[]"),
    "blocked the org": lambda s: (v := opted_in(), block_org(v, s.opening.org_id))[0],
    "blocked by the org": lambda s: (
        v := opted_in(),
        block_volunteer(v, s.opening.org_id),
    )[0],
    "replied STOP, no email": lambda s: opted_in(sms_opted_out=True),
    "no phone, no email": lambda s: opted_in(phone=None),
    "already liked it": lambda s: (
        v := opted_in(),
        data.swipe(v, s.opening.id, True),
    )[0],
    "passed on it": lambda s: (v := opted_in(), data.swipe(v, s.opening.id, False))[0],
    "suspended": lambda s: opted_in(suspended_at="2026-01-01T00:00:00+00:00"),
    "not onboarded": lambda s: opted_in(onboarded_at=None),
}


@pytest.mark.parametrize("why", sorted(EXCLUDED))
def test_wave_excludes(shelter, sweep, why):
    ok = opted_in()  # the control: notified
    out = EXCLUDED[why](shelter)
    call_on(shelter, needed=2)
    sweep.when = at(11, call_day() - timedelta(days=1))  # the day before the call
    sweep()
    assert notified() == {ok}, why
    assert out not in {n.recipient_id for n in notifications()}


def test_wave_same_day_only_is_notified_on_the_day(shelter, sweep):
    vol = opted_in(urgent_window="today")
    call_on(shelter)
    sweep.when = at(11)  # the call's own day
    sweep()
    assert notified() == {vol}


def test_wave_weekly_cap(shelter, sweep):
    """At most what they picked, and never more than 3 in 7 days."""
    one = opted_in(urgent_per_week=1)
    three = opted_in(urgent_per_week=3)
    for uid, n in ((one, 1), (three, 3)):
        for i in range(n):
            notifications.insert(
                kind="urgent",
                key=f"urgent:0:{uid}:{i}",
                recipient_id=uid,
                sent_at=(sweep.when - timedelta(days=6)).isoformat(timespec="seconds"),
                status="sent",
                attempts=1,
            )
    fresh = opted_in()
    call_on(shelter, needed=2)
    sweep()
    assert notified() - {one, three} == {fresh}


def test_wave_weekly_cap_frees_up_after_7_days(shelter, sweep):
    vol = opted_in(urgent_per_week=1)
    notifications.insert(
        kind="urgent",
        key=f"urgent:0:{vol}",
        recipient_id=vol,
        sent_at=(sweep.when - timedelta(days=7, minutes=1)).isoformat(),
        status="sent",
        attempts=1,
    )
    call_on(shelter)
    sweep()
    assert vol in notified()


def test_wave_after_stop_goes_by_email(shelter, sweep):
    vol = opted_in(sms_opted_out=True, email="vol@example.org")
    call_on(shelter)
    notify.get_emailer().sent.clear()
    assert sweep().text == "sent 1" and not notify.get_sms().sent
    ((to, subject, body),) = notify.get_emailer().sent
    assert to == "vol@example.org" and vol in notified()
    assert subject == "Last minute: Eastside Paws Rescue needs 1 more person today 1 pm"
    assert f"/deck?card={shelter.opening.id}" in body and "Preferences" in body


def test_wave_twilio_stop_reply_marks_the_number(shelter, sweep, monkeypatch):
    vol = opted_in()
    call_on(shelter)

    def stopped(phone, text):
        raise notify.SMSOptedOut

    monkeypatch.setattr(notify.get_sms(), "send", stopped)
    assert sweep().text == "sent 0"
    assert profiles[vol].sms_opted_out
    (n,) = notifications(where="kind = 'urgent'")
    assert n.status == "skipped"


def test_wave_texts_count_against_the_sms_ceiling(shelter, sweep, monkeypatch):
    monkeypatch.setenv("SMS_MAX_PER_HOUR", "1")
    a, b = opted_in(), opted_in()
    call_on(shelter, needed=1)
    assert sweep().text == "sent 1"
    statuses = sorted(n.status for n in notifications(where="kind = 'urgent'"))
    assert statuses == ["deferred", "sent"]
    assert notified() < {a, b}


def test_wave_task_needs_the_token(shelter, sweep):
    opted_in()
    call_on(shelter)
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
        r = TestClient(app).post("/tasks/urgent", headers=headers)
        assert r.status_code == 401
    assert not notify.get_sms().sent and urgent_calls()[0].waves is None


def test_wave_link_puts_the_card_on_top_or_says_it_ended(shelter, user):
    """/deck?card=<id>: that opening first while the deck holds it."""
    uid = uid_of(user)
    top = data.deck(uid, 50)
    other = top[3]["id"]
    page = user.get(f"/deck?card={other}")
    assert top[3]["title"] in page.text.split("Pass")[0]
    assert "has filled or ended" not in page.text
    closed = openings(where="status = 'closed'")[0].id
    for card in (closed, "abc", "²", "9" * 30):
        page = user.get(f"/deck?card={card}")
        assert page.status_code == 200
    assert (
        "That last-minute call has filled or ended."
        in user.get(f"/deck?card={closed}").text
    )
