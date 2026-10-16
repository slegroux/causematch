"""Urgent calls: a nonprofit short of people for a shift in the next 48 hours."""

import itertools
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import coordinator_for, org_sign_in, uid_of
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
    swipes,
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


def like_call(uid, shelter):
    """A like on the urgent card (drawn while the call was open): the form carries the
    call's id."""
    call = urgent_calls(
        where="opening_id = ?", where_args=[shelter.opening.id], order_by="id DESC"
    )[0]
    return data.swipe(uid, shelter.opening.id, True, call_id=call.id)


def card_like(shelter, **extra):
    """/swipe's form for a like on the shelter's urgent card."""
    call = data._open_calls()[shelter.opening.id]
    return {"opening_id": str(shelter.opening.id), "liked": "1",
            "call_id": str(call.id), **extra}  # fmt: skip


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
    r = user.post("/swipe", data=card_like(shelter))
    assert r.status_code == 200
    (m,) = matches(where="opening_id = ?", where_args=[shelter.opening.id])
    assert m.urgent_call_id == urgent_calls()[0].id and m.status == "pending"


@pytest.mark.parametrize("auto", [False, True])
def test_deck_like_on_a_card_drawn_before_the_call_shows_the_call_first(
    shelter, user, auto
):
    """The card said the opening's own date; the call is for tomorrow. Nothing is
    recorded: the deck shows the call's card, with a notice, before a yes counts."""
    post_call(shelter, auto=auto)
    form = {"opening_id": str(shelter.opening.id), "liked": "1"}  # no call_id
    r = user.post("/swipe", data=form, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "This opening now needs people tomorrow 9 am." in r.text
    assert DOGS in r.text.split("Pass")[0] and "Urgent · tomorrow 9 am" in r.text
    assert not matches(where="opening_id = ?", where_args=[shelter.opening.id])
    assert not swipes(where="opening_id = ?", where_args=[shelter.opening.id])
    assert not shifts()
    r = user.post("/swipe", data=form, follow_redirects=False)  # without script
    assert r.headers["location"] == f"/deck?card={shelter.opening.id}"
    stale = {**form, "call_id": str(urgent_calls()[0].id + 7)}  # another call's id
    user.post("/swipe", data=stale)
    assert not matches(where="opening_id = ?", where_args=[shelter.opening.id])
    user.post("/swipe", data=card_like(shelter))  # now from the call's card
    (m,) = matches(where="opening_id = ?", where_args=[shelter.opening.id])
    assert m.urgent_call_id == urgent_calls()[0].id
    assert m.status == ("accepted" if auto else "pending")


def test_deck_a_pass_on_an_older_card_still_counts(shelter, user):
    post_call(shelter)
    form = {"opening_id": str(shelter.opening.id), "liked": "0"}
    assert user.post("/swipe", data=form).status_code == 200
    assert swipes(where="opening_id = ?", where_args=[shelter.opening.id])


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
    assert "Stop urgent call" not in shelter.get("/org").text
    assert urgent_calls()[0].status == "open"  # reads write nothing
    data.urgent_waves_due(datetime.now(UTC))  # the task's sweep stamps it
    call = urgent_calls()[0]
    assert call.status == "closed" and call.ended_at


def test_close_when_filled_books_the_call_time(shelter, user):
    post_call(shelter, needed="1")
    uid = uid_of(user)
    m = like_call(uid, shelter)
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
    data._sync_urgent(None)  # what decide runs after an accept fills the opening
    assert urgent_calls()[0].status == "filled" and urgent_calls()[0].ended_at


def test_deck_demo_seeds_an_urgent_call_first_in_the_deck(user):
    """scripts/demo.py's call: on the invented seed it adds the org's second opening."""
    from app import demo_seed

    coord, _ = coordinator_for(0)
    org = orgs()[0]
    first = openings(where="org_id = ? AND status = 'open'", where_args=[org.id])[0]
    oid = demo_seed._urgent_call(coord, org, first)
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
    m = like_call(vols[0], shelter)
    data.decide(shelter.uid, m.id, True)
    sweep.when += timedelta(minutes=40)
    assert sweep().text == "sent 0" and urgent_calls()[0].status == "filled"


def test_wave_due_is_claimed_once(shelter, sweep):
    """Two sweeps at once (a scheduler retry) never send the same wave twice."""
    call = call_on(shelter)
    ((cid, done, _),) = data.urgent_waves_due(sweep.when)
    assert (cid, done) == (call.id, 0)
    assert data.urgent_claim_wave(cid, done, sweep.when)
    assert not data.urgent_claim_wave(cid, done, sweep.when)
    assert data.urgent_waves_due(sweep.when) == []  # wave 2 waits 30 minutes


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


def this_call(call):
    """The volunteers a notice for this call was sent to."""
    rows = notifications(
        where="key LIKE ? AND status = 'sent'", where_args=[f"urgent:{call.id}:%"]
    )
    return {r.recipient_id for r in rows}


@pytest.mark.parametrize(
    "per_week, before, ago, status, asked",
    [
        (1, 0, None, "sent", True),
        (1, 1, timedelta(days=6), "sent", False),
        (3, 2, timedelta(days=6), "sent", True),
        (3, 3, timedelta(days=6), "sent", False),
        (5, 2, timedelta(days=6), "sent", True),  # what they picked, up to 3
        (5, 3, timedelta(days=6), "sent", False),  # never more than 3
        (1, 1, timedelta(days=6, hours=23), "sent", False),
        (1, 1, timedelta(days=7, minutes=1), "sent", True),  # a week on
        (1, 1, timedelta(days=1), "unknown", False),  # may have been delivered
        (1, 1, timedelta(days=1), "deferred", True),  # never went out
        (1, 1, timedelta(days=1), "failed", True),
    ],
)
def test_wave_weekly_cap(shelter, sweep, per_week, before, ago, status, asked):
    """At most what they picked, never more than 3, in any 7 days."""
    vol = opted_in(urgent_per_week=per_week)
    for i in range(before):
        notifications.insert(
            kind="urgent",
            key=f"urgent:0:{vol}:{i}",
            recipient_id=vol,
            sent_at=(sweep.when - ago).isoformat(timespec="seconds"),
            status=status,
            attempts=1,
        )
    other = opted_in()  # the control: always asked
    call = call_on(shelter, needed=2)
    sweep()
    assert this_call(call) == ({vol, other} if asked else {other})


def test_wave_weekly_cap_counts_only_urgent_notices(shelter, sweep):
    vol = opted_in(urgent_per_week=1)
    for i, kind in enumerate(("reminder", "accepted", "message")):
        notifications.insert(kind=kind, key=f"{kind}:{i}:{vol}", recipient_id=vol,
                             sent_at=sweep.when.isoformat(timespec="seconds"),
                             status="sent", attempts=1)  # fmt: skip
    call = call_on(shelter)
    sweep()
    assert this_call(call) == {vol}


def test_wave_after_stop_goes_by_email(shelter, sweep):
    vol = opted_in(sms_opted_out=True, email="vol@example.org")
    call_on(shelter)
    notify.get_emailer().sent.clear()
    assert sweep().text == "sent 1" and not notify.get_sms().sent
    ((to, subject, body),) = notify.get_emailer().sent
    assert to == "vol@example.org" and vol in notified()
    assert subject == "Last minute: Eastside Paws Rescue needs 1 more person today 1 pm"
    assert f"/deck?card={shelter.opening.id}" in body
    off = f"https://causematch.test/urgent/off/{data.urgent_off_key(vol)}"
    assert f"To stop them: {off}" in body
    assert notify.get_emailer().with_headers[-1] == (
        "vol@example.org",
        subject,
        {"List-Unsubscribe": f"<{off}>"},
    )


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


# --- auto-accept ------------------------------------------------------------------------


def org_emails():
    return [(to, subject) for to, subject, _ in notify.get_emailer().sent]


def test_auto_accept_like_is_in_booked_and_opens_the_chat(shelter, user):
    call = call_on(shelter, needed=2, starts_at=tomorrow())
    urgent_calls.update(id=call.id, auto_accept=True)
    notify.get_emailer().sent.clear()
    r = user.post("/swipe", data=card_like(shelter), headers={"HX-Request": "true"})
    (m,) = matches(where="opening_id = ?", where_args=[shelter.opening.id])
    assert r.headers["HX-Redirect"] == f"/chat/{m.id}?notice=in"
    assert (m.status, m.decided_by, m.urgent_call_id) == (
        "accepted",
        shelter.uid,
        call.id,
    )
    (shift,) = shifts(where="match_id = ?", where_args=[m.id])
    assert (shift.starts_at, shift.status) == (tomorrow(), "scheduled")
    chat = user.get(f"/chat/{m.id}?notice=in").text
    assert "You're in! Your shift is booked." in chat
    assert org_emails() == [
        (
            "coord3@example.org",
            "A volunteer is in for Walk dogs at the shelter, tomorrow 9 am",
        )
    ]
    assert "1 of 2 found" in notify.get_emailer().sent[0][2]
    mine = data.my_matches(uid_of(user))[0]
    assert mine["status"] == "accepted" and mine["starts_at"] == tomorrow()
    assert "1 of 2 found" in shelter.get("/org").text


def test_auto_accept_without_script_redirects_to_the_chat(shelter, user):
    call = call_on(shelter, needed=1)
    urgent_calls.update(id=call.id, auto_accept=True)
    r = user.post("/swipe", data=card_like(shelter), follow_redirects=False)
    (m,) = matches(where="opening_id = ?", where_args=[shelter.opening.id])
    assert r.status_code == 303 and r.headers["location"] == f"/chat/{m.id}?notice=in"
    assert urgent_calls()[0].status == "filled"


def test_auto_accept_six_concurrent_likes_accept_exactly_two(shelter):
    import threading

    call = call_on(shelter, needed=2)
    urgent_calls.update(id=call.id, auto_accept=True)
    vols = [opted_in() for _ in range(6)]
    start = threading.Barrier(len(vols))
    errors = []

    def like(uid):
        try:
            start.wait()
            data.swipe(uid, shelter.opening.id, True, call_id=call.id)  # its card
        except Exception as e:  # noqa: BLE001  reported below
            errors.append(e)

    threads = [threading.Thread(target=like, args=(v,)) for v in vols]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    got = sorted(
        m.status for m in matches(where="urgent_call_id = ?", where_args=[call.id])
    )
    assert got == ["accepted"] * 2 + ["pending"] * 4
    assert len(shifts()) == 2 and urgent_calls()[0].status == "filled"


def test_auto_accept_the_statement_alone_never_overfills(shelter):
    """Even without the lock: a full call's guard turns a late accept into a no-op."""
    call = call_on(shelter, needed=1)
    urgent_calls.update(id=call.id, auto_accept=True)
    a, b = opted_in(), opted_in()
    like_call(a, shelter)
    late = matches.insert(
        opening_id=shelter.opening.id, volunteer_id=b, status="pending",
        created_at=data._now(), urgent_call_id=call.id,
    )  # fmt: skip
    data._auto_accept(late, urgent_calls[call.id])
    assert matches[late.id].status == "pending"
    data._auto_accept(late, urgent_calls[call.id])  # twice changes nothing either
    assert [m.status for m in matches(order_by="id")].count("accepted") == 1


def test_auto_accept_past_n_likes_wait_for_the_org(shelter):
    call = call_on(shelter, needed=2)
    urgent_calls.update(id=call.id, auto_accept=True)
    vols = [opted_in() for _ in range(3)]
    ms = [like_call(v, shelter) for v in vols]
    assert [m.status for m in ms] == ["accepted", "accepted", "pending"]
    assert "Vol" in shelter.get("/org/inbox").text
    data.decide(shelter.uid, ms[2].id, True)  # the org still may take a third
    assert matches[ms[2].id].status == "accepted"


def test_auto_accept_off_leaves_every_like_pending(shelter):
    call_on(shelter, needed=2)
    ms = [like_call(opted_in(), shelter) for _ in range(2)]
    assert [m.status for m in ms] == ["pending", "pending"] and not shifts()


def test_auto_accept_never_takes_a_like_the_org_cannot_see(shelter):
    """A volunteer the org blocked: their like stays blind and pending, and takes no place."""
    call = call_on(shelter, needed=1)
    urgent_calls.update(id=call.id, auto_accept=True)
    blocked, ok = opted_in(), opted_in()
    block_volunteer(blocked, shelter.opening.org_id)
    m = like_call(blocked, shelter)
    assert (m.status, m.org_blind) == ("pending", 1)
    assert like_call(ok, shelter).status == "accepted"


def test_auto_accept_stops_at_the_opening_slots(shelter):
    call = call_on(shelter, needed=3)
    urgent_calls.update(id=call.id, auto_accept=True)
    openings.update(id=shelter.opening.id, slots=1)
    first = like_call(opted_in(), shelter)
    assert first.status == "accepted"
    assert openings[shelter.opening.id].status == "filled"
    assert urgent_calls()[0].status == "filled"
    # the slot guard alone, with the call reopened by hand
    urgent_calls.update(id=call.id, status="open")
    late = matches.insert(
        opening_id=shelter.opening.id, volunteer_id=opted_in(), status="pending",
        created_at=data._now(), urgent_call_id=call.id,
    )  # fmt: skip
    data._auto_accept(late, urgent_calls[call.id])
    assert matches[late.id].status == "pending"


def test_auto_accept_a_like_just_after_it_filled_still_counts_toward_it(shelter):
    """The card showed the call: the like is for the call's time, pending for the org."""
    call = call_on(shelter, needed=1, starts_at=tomorrow())
    urgent_calls.update(id=call.id, auto_accept=True)
    like_call(opted_in(), shelter)
    assert urgent_calls()[0].status == "filled"
    late = data.swipe(opted_in(), shelter.opening.id, True, call_id=call.id)
    assert (late.status, late.urgent_call_id) == ("pending", call.id)
    data.decide(shelter.uid, late.id, True)
    (shift,) = shifts(where="match_id = ?", where_args=[late.id])
    assert shift.starts_at == tomorrow()
    # another opening's call, or one that was stopped, is not taken
    other = opted_in()
    plain = data.swipe(other, shelter.opening.id, True, call_id=call.id + 1)
    assert plain.urgent_call_id is None


def test_auto_accept_card_carries_its_call(shelter, user):
    call = call_on(shelter)
    page = user.get("/deck").text
    assert f'value="{call.id}" name="call_id"' in page
    r = user.post(
        "/swipe",
        data=card_like(shelter, call_id="x" * 30),
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "now needs people" in r.text  # not its card
    assert not matches(where="opening_id = ?", where_args=[shelter.opening.id])
    user.post("/swipe", data=card_like(shelter))
    (m,) = matches(where="opening_id = ?", where_args=[shelter.opening.id])
    assert m.urgent_call_id == call.id


# --- waves and auto-accept: quiet hours, budgets and bookings ---------------------------


def test_wave_waits_out_quiet_hours_instead_of_spending_itself(shelter, sweep):
    """Posted at 10 pm for tomorrow 1 pm: everyone is in their quiet hours (9 pm to
    8 am), so no wave is spent at 10 pm or 10:30 pm; wave 1 goes at 8:10 am."""
    vol = opted_in()  # default quiet hours
    call = call_on(shelter)
    sweep.when = at(22, call_day() - timedelta(days=1))
    assert sweep().text == "sent 0"
    sweep.when += timedelta(minutes=30)
    assert sweep().text == "sent 0"
    assert urgent_calls[call.id].waves is None  # nothing spent
    sweep.when = at(8, call_day()) + timedelta(minutes=10)
    assert sweep().text == "sent 1" and this_call(call) == {vol}
    assert urgent_calls[call.id].waves == 1


def test_wave_urgent_texts_leave_half_the_sms_budget_to_sign_in_codes(
    shelter, sweep, monkeypatch
):
    monkeypatch.setenv("SMS_MAX_PER_HOUR", "2")
    vols = [opted_in() for _ in range(3)]
    call = call_on(shelter, needed=1)
    assert sweep().text == "sent 1"  # urgent texts may take 1 of the 2
    statuses = sorted(n.status for n in notifications(where="kind = 'urgent'"))
    assert statuses == ["deferred", "deferred", "sent"] and this_call(call) < set(vols)
    verifier = notify.get_verifier()
    before = len(verifier.sent)
    r = TestClient(app).post("/login/send", data={"phone": "(213) 555-0177"})
    assert (
        r.status_code == 200 and len(verifier.sent) == before + 1
    )  # a code still goes


def test_wave_unsubscribe_link_turns_calls_off_signed_out(shelter, sweep):
    vol = opted_in(sms_opted_out=True, email="vol@example.org")
    call_on(shelter)
    sweep()
    path = f"/urgent/off/{data.urgent_off_key(vol)}"
    anon = TestClient(app)
    page = anon.get(path)
    assert page.status_code == 200 and "Turn them off" in page.text
    assert profiles[vol].urgent_on  # a GET (a mail scanner) changes nothing
    r = anon.post(path, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and profiles[vol].urgent_on
    r = anon.post(path)
    assert r.status_code == 200 and "no more last-minute calls" in r.text
    assert not profiles[vol].urgent_on


def test_deck_auto_accept_card_says_a_like_books_you(shelter, user):
    call = call_on(shelter, starts_at=tomorrow())
    page = user.get("/deck").text
    assert "Like and you're booked" not in page  # the org decides each one
    urgent_calls.update(id=call.id, auto_accept=True)
    line = "Like and you're booked for tomorrow 9 am; they then see your full name."
    assert line in user.get("/deck").text
    assert data.deck(uid_of(user), 1)[0]["booked"] == line


def test_auto_accept_count_clause_alone_never_overfills(shelter):
    """The call still marked open (no status flag to stop it) with its `needed` already
    accepted: the count clause refuses."""
    call = call_on(shelter, needed=1)
    urgent_calls.update(id=call.id, auto_accept=True)
    assert like_call(opted_in(), shelter).status == "accepted"
    urgent_calls.update(id=call.id, status="open")
    late = matches.insert(
        opening_id=shelter.opening.id, volunteer_id=opted_in(), status="pending",
        created_at=data._now(), urgent_call_id=call.id,
    )  # fmt: skip
    data._auto_accept(late, urgent_calls[call.id])
    assert matches[late.id].status == "pending"


def test_auto_accept_call_id_is_bound_to_its_opening(shelter):
    """A filled call on the shelter's other opening, named on a like for the dogs: the
    like is a plain one."""
    cats = openings(where="org_id = ? AND id != ?",
                    where_args=[shelter.opening.org_id, shelter.opening.id])[0]  # fmt: skip
    r = post_call(shelter, needed="1", opening=cats)
    assert r.status_code == 303
    (cat_call,) = urgent_calls(where="opening_id = ?", where_args=[cats.id])
    urgent_calls.update(id=cat_call.id, auto_accept=True)
    first = data.swipe(opted_in(), cats.id, True, call_id=cat_call.id)
    assert first.status == "accepted" and urgent_calls[cat_call.id].status == "filled"
    plain = data.swipe(opted_in(), shelter.opening.id, True, call_id=cat_call.id)
    assert (plain.urgent_call_id, plain.status) == (None, "pending")
    assert not shifts(where="match_id = ?", where_args=[plain.id])


def booked_at(uid, starts_at):
    o = openings(where="status = 'open'")[0]
    m = matches.insert(
        opening_id=o.id, volunteer_id=uid, status="accepted", created_at=data._now()
    )
    shifts.insert(match_id=m.id, starts_at=starts_at, status="scheduled",
                  created_at=data._now())  # fmt: skip


@pytest.mark.parametrize("hours, asked", [(-1.5, False), (1, False), (3, True)])
def test_wave_skips_volunteers_booked_near_the_call(shelter, sweep, hours, asked):
    vol = opted_in()
    start = datetime.fromisoformat(call_start()) + timedelta(hours=hours)
    booked_at(vol, start.strftime("%Y-%m-%dT%H:%M"))
    control = opted_in()
    call = call_on(shelter, needed=2)
    sweep()
    assert this_call(call) == ({vol, control} if asked else {control})


def test_wave_text_quotes_no_web_address_and_no_emoji(shelter, sweep):
    opted_in()
    openings.update(
        id=shelter.opening.id,
        title="🐶 Walk dogs, sign up at evil.example/x or https://evil.example "
        "www.evil.example, and help us all through the long busy weekend please",
    )
    call_on(shelter)
    sweep()
    ((_, text),) = notify.get_sms().sent
    quoted = text.split(" for ", 1)[1].split(". Interested?")[0]
    assert "evil" not in text and "🐶" not in text
    assert quoted.startswith("Walk dogs, sign up at or") and len(quoted) <= 60
    assert quoted.endswith("...")
    assert notify.sms_safe("🐶🐱") == "an opening"


def test_deck_demo_skips_a_call_on_a_fully_booked_opening():
    from app import demo_seed

    coord, _ = coordinator_for(0)
    org = orgs()[0]
    first, full = openings(where="org_id = ?", where_args=[org.id], order_by="id")[:2]
    openings.update(id=full.id, status="open", slots=0)  # the closed holiday drive
    assert demo_seed._urgent_call(coord, org, first) == full.id
    assert not urgent_calls()
