import copy
import functools
import hashlib
import hmac
import inspect
import json
import secrets
import threading
from datetime import UTC, date, datetime, timedelta

import apsw
from apswutils.db import NotFoundError

from app import db as _db
from app import match as _match
from app import photos as _photos
from app import prefs as _prefs
from app.choices import (
    ANY_DISTANCE_KM,
    DEFAULT_DEALBREAKERS,
    INTERESTS,
    LANGUAGES,
    MAX_MINUTES,
    SHIFT_HOURS,
    SHIFT_LENGTHS,
)
from app.config import demo_matches, phone_hash_key
from app.forms import BIDI, hidden_chars
from app.match import LA
from app.metrics_rows import fetch_rows


def start(demo):
    """Run once at startup: the cause rows, and the invented demo rows when asked."""
    _db.ensure_causes()
    if demo:
        _db.seed_demo()


def _is_id(n):
    """A row id some table could hold: an int in 1 .. 2**63-1 (SQLite's integer range)."""
    return isinstance(n, int) and not isinstance(n, bool) and 0 < n < 2**63


_LOCAL_FORMAT = "%Y-%m-%dT%H:%M"


def _local_now():
    """Wall clock in Los Angeles, the zone openings' starts_at are stored in."""
    return datetime.now(LA).strftime(_LOCAL_FORMAT)


def local_today():
    """Today's date in Los Angeles, "YYYY-MM-DD"."""
    return datetime.now(LA).date().isoformat()


# The travel minutes a sign-up radius stood for (app/routes/onboarding.py TRAVEL).
RADIUS_MINUTES = {8: 15, 16: 30, 25: 45}
LANGUAGE_NAMES = dict(LANGUAGES)
TRAVEL_PHRASE = {
    "car": "by car",
    "transit": "by transit",
    "bike": "by bike",
    "walk": "on foot",
}


def _with_minutes(place, minutes):
    """The place with this travel time, and the reach and distance scale it gives."""
    reach = _match.reach_km(place["mode"], minutes)
    return {
        **place,
        "max_minutes": minutes,
        "reach_km": reach,
        "scale_km": reach or ANY_DISTANCE_KM,
    }


def _widening(key):
    """(place index, minutes) of a "widen:<i>:<minutes>" relaxation key."""
    return map(int, key.split(":")[1:])


def _places(me):
    """The volunteer's places, as match.best_place takes them; scale_km scales the distance
    score. A profile with no saved place has one, Home: its sign-up ZIP by car, reaching
    exactly its radius, so its deck stays as it was."""
    rows = _db.places(where="user_id = ?", where_args=[me.id], order_by="id")
    if rows:
        return [
            _with_minutes(
                {
                    k: getattr(p, k)
                    for k in ("id", "label", "lat", "lon", "area", "mode")
                },
                p.max_minutes,
            )
            for p in rows
        ]
    if None in (me.lat, me.lon):
        return []
    radius = me.radius_km
    gated = bool(radius) and radius < ANY_DISTANCE_KM
    return [
        {
            "id": None,
            "label": "Home",
            "lat": me.lat,
            "lon": me.lon,
            "area": me.neighborhood or "My sign-up ZIP",
            "mode": "car",
            "max_minutes": RADIUS_MINUTES.get(radius, 0) if gated else 0,
            "reach_km": radius if gated else None,
            "scale_km": radius,
        }
    ]


TODAY_HOURS = 48  # "Free in the next 48h": shifts starting this soon


def _deck_prefs(me):
    """Everything about the volunteer that shapes the deck, read once. relaxations()
    changes one value of a copy and counts what the deck would hold."""
    return {
        "causes": _prefs.cause_prefs(me),
        "places": _places(me),
        "horizon_days": _prefs.horizon_days(me),
        "dealbreakers": _prefs.dealbreakers(me),
        "verified_hours": _prefs.verified_hours(me),
        "availability": json.loads(me.availability or "[]"),
        "free_dates": _prefs.free_dates(me, local_today()),
        "commitment": _prefs.commitment(me),
        "work": _prefs.work(me),
        "orientation_ok": _prefs.orientation_ok(me),
    }


class CallPosted(Exception):
    """A like on a card drawn before the opening's urgent call was posted (_swipe)."""

    def __init__(self, call):
        super().__init__("an urgent call was posted on this opening")
        self.call = call


def _deck_openings(uid, today=False, p=None, calls=None):
    """[(opening, active cause labels, km, (place, km, minutes) or None)] this volunteer
    could still swipe on.

    swipe() checks visibility with this same function, so whatever hides an opening
    from the deck (closed, past, inactive cause, a cause that is "not for me", past the
    volunteer's horizon, a dealbreaker: out of reach of every place, ...) also stops a like
    sent by id. today: only shifts starting in the next TODAY_HOURS, whatever the
    volunteer's times. An opening with an open urgent call comes as a copy starting at
    the call's time, so every check and the card use that time. calls: _open_calls(),
    read here when not given. The place is the one quickest to reach the opening from; it and km
    are None for a remote opening or when the volunteer has no place. p: _deck_prefs(),
    read here when not given.
    """
    labels = {c.slug: c.label for c in _db.causes() if c.active}
    seen = {
        s.opening_id for s in _db.swipes(where="volunteer_id = ?", where_args=[uid])
    }
    p = p or _deck_prefs(_db.profiles[uid])
    only = p["dealbreakers"]
    places = p["places"]
    if "distance" not in only:  # rank by distance, but show every place's reach
        places = [{**pl, "reach_km": None} for pl in places]
    start = datetime.now(LA)
    days = TODAY_HOURS / 24 if today else p["horizon_days"]
    until = (start + timedelta(days=days)).strftime(_LOCAL_FORMAT) if days else None
    # Only verified, active orgs the volunteer has not blocked. An org that blocked the
    # volunteer stays visible: hiding it would tell them.
    shown = {
        r["id"]
        for r in _db.db.query(
            "SELECT id FROM org WHERE verified_at IS NOT NULL AND deactivated_at IS NULL "
            "AND id NOT IN (SELECT blocked_org_id FROM block "
            "WHERE blocker_id = ? AND blocked_user_id IS NULL)",
            [uid],
        )
    }
    calls = _open_calls() if calls is None else calls
    confirming = _confirming_orgs()
    out = []
    for o in _db.openings(
        where="status = ? AND starts_at >= ?", where_args=["open", _local_now()]
    ) + [_db.openings[i] for i in calls if _db.openings[i].starts_at < _local_now()]:
        if o.id in calls:
            o = copy.copy(o)
            o.starts_at = calls[o.id].starts_at
        if o.org_id not in shown or (until and o.starts_at > until):
            continue
        slugs = json.loads(o.causes or "[]")
        cause_labels = [labels[s] for s in slugs if s in labels]
        if not cause_labels or o.id in seen:
            continue
        # a "not for me" of a cause that is inactive now waits for it to come back
        if not _match.causes_allowed(p["causes"], [s for s in slugs if s in labels]):
            continue
        if "causes" in only and not any(
            p["causes"].get(s) in (_prefs.LOVE, _prefs.OPEN)
            for s in slugs
            if s in labels
        ):
            continue  # only loved and open causes
        if o.orientation_required == "separate" and not p["orientation_ok"]:
            continue
        if "when" in only and not today and not _when_ok(p, o):
            continue
        if "work" in only and not _match.work_allowed(p["work"], _work_of(o)):
            continue
        if p["verified_hours"] == "only" and o.org_id not in confirming:
            continue
        near = None
        if not o.remote:
            if None in (o.lat, o.lon):
                continue  # an in-person opening with no place cannot be ranked or gated
            if places:
                near = _match.best_place(places, o.lat, o.lon)
                if near is None:
                    continue  # out of reach of every place
        out.append((o, cause_labels, near and near[1], near))
    return out


def _confirming_orgs():
    """The ids of the orgs that say they can confirm volunteer hours."""
    return {g.id for g in _db.orgs(where="confirms_hours = 1")}


def _when_ok(p, o):
    """The "when" dealbreaker: free then (or a flexible opening), and the commitment fits."""
    free = _match.availability_overlap(
        p["availability"], o.starts_at, o.recurrence, p["free_dates"]
    )
    return free > 0 and _match.commitment_fits(p["commitment"], o.recurrence)


def record_empty_deck(uid, today=False):
    """Log an empty deck, once per volunteer and mode per hour: refreshing doesn't
    inflate it, and the "Free in the next 48h" split stays whole."""
    hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    if not _db.deck_empties.count_where(
        "volunteer_id = ? AND coalesce(today, 0) = ? AND at > ?",
        [uid, int(bool(today)), hour_ago],
    ):
        _db.deck_empties.insert(_db.DeckEmpty(volunteer_id=uid, today=today, at=_now()))


TIERS = [m for m, _ in MAX_MINUTES if m]  # 15, 30, 45, 60; then any distance


def _wider(place):
    """The travel times past this place's reach, shortest first, in minutes (0: any)."""
    if place["reach_km"] is None:
        return []
    return [
        m for m in TIERS if _match.reach_km(place["mode"], m) > place["reach_km"]
    ] + [0]


def _relaxed(p, key):
    """A copy of _deck_prefs() with one preference loosened (keys: _relaxation_labels)."""
    q = {**p, "dealbreakers": [d for d in p["dealbreakers"] if f"only_{d}" != key]}
    if key == "horizon":
        q["horizon_days"] = 0
    elif key == "only_verified":
        q["verified_hours"] = "prefer"
    elif key.startswith("widen:"):
        i, to = _widening(key)
        q["places"] = [
            _with_minutes(pl, to) if j == i else pl for j, pl in enumerate(p["places"])
        ]
    return q


def _relaxation_labels(p):
    """{key: label} of the changes that could add cards. widen:<place>:<minutes> for each
    wider travel time of each place, then the horizon and the dealbreakers."""
    out = {}
    if "distance" in p["dealbreakers"]:
        for i, pl in enumerate(p["places"]):
            who = f" {pl['label']}" if len(p["places"]) > 1 else ""
            for to in _wider(pl):
                reach = f"{to} min" if to else "any distance"
                out[f"widen:{i}:{to}"] = f"Widen{who} to {reach}"
    if p["horizon_days"]:
        out["horizon"] = "Look further ahead"
    if p["verified_hours"] == "only":
        out["only_verified"] = "Show nonprofits that don't confirm hours"
    names = {
        "causes": "Show every cause you haven't ruled out",
        "when": "Show any time",
        "work": "Show any kind of work",
    }
    for d, label in names.items():
        if d in p["dealbreakers"]:
            out[f"only_{d}"] = label
    return out


def relaxations(uid, today=False, view=None):
    """{"key", "label", "more"}: the one change that adds the most cards to the deck as
    shown (the saved preferences under the filter sheet's view), or None. With a view on,
    "Clear filters" comes first; a saved change is offered only when it adds cards even
    under the view, so taking it never changes a preference for nothing. For a place,
    only its shortest wider travel time that adds any is offered."""
    p = _deck_prefs(_db.profiles[uid])
    base = len(_deck_openings(uid, today, _viewed(p, view)))
    options = [("clear_view", "Clear filters", p)] if view else []
    options += [
        (key, label, _viewed(_relaxed(p, key), view))
        for key, label in _relaxation_labels(p).items()
    ]
    best, widened = None, set()
    for key, label, q in options:
        place = key.split(":")[1] if key.startswith("widen:") else None
        if place in widened:
            continue
        more = len(_deck_openings(uid, today, q)) - base
        if more > 0 and place is not None:
            widened.add(place)
        if more > 0 and (best is None or more > best["more"]):
            best = {"key": key, "label": label, "more": more}
    return best


def _saved_places(uid):
    """The volunteer's places as rows, saving the sign-up Home first when there are none."""
    me = _db.profiles[uid]
    places = _places(me)
    if places and places[0]["id"] is None:
        home = places[0]
        _db.places.insert(
            _db.Place(
                user_id=uid,
                label=home["label"],
                lat=home["lat"],
                lon=home["lon"],
                area=home["area"],
                mode=home["mode"],
                # the sign-up travel time, or the shortest that reaches as far
                max_minutes=home["max_minutes"]
                or next(
                    (
                        m
                        for m in TIERS
                        if home["reach_km"] is not None
                        and _match.reach_km(home["mode"], m) >= home["reach_km"]
                    ),
                    0,
                ),
            )
        )
    return _db.places(where="user_id = ?", where_args=[uid], order_by="id")


def apply_relaxation(uid, key):
    """Loosen one preference, as relaxations() offered it. ValueError for an unknown key."""
    me = _db.profiles[uid]
    p = _deck_prefs(me)
    if key not in _relaxation_labels(p):
        raise ValueError("not a relaxation on offer")
    with _db.db.conn:
        if key.startswith("widen:"):
            i, to = _widening(key)
            _db.places.update(id=_saved_places(uid)[i].id, max_minutes=to)
        elif key == "horizon":
            _db.profiles.update(id=uid, horizon_days=0)
        elif key == "only_verified":
            _db.profiles.update(id=uid, verified_hours="prefer")
        else:
            _db.profiles.update(
                id=uid, dealbreakers=json.dumps(_relaxed(p, key)["dealbreakers"])
            )


def _viewed(p, view):
    """_deck_prefs() under the deck's filter sheet, until the volunteer clears it (it lives
    in the session; the saved preferences never change). view: {"max_minutes", "hidden",
    "this_week"}, each only when it differs from the saved value, or None.

    max_minutes sets the main place's travel time (only while distance is "Only show"; it
    may widen as well as narrow), hidden causes are hidden like "not for me", and
    this_week caps the horizon at 7 days. Nothing else can be loosened."""
    if not view:
        return p
    q = {**p, "causes": dict(p["causes"])}
    for slug in view.get("hidden", []):
        q["causes"][slug] = _prefs.NO
    if view.get("this_week") and not 0 < p["horizon_days"] <= 7:
        q["horizon_days"] = 7
    minutes = view.get("max_minutes")
    if minutes is not None and p["places"] and "distance" in p["dealbreakers"]:
        q["places"] = [_with_minutes(p["places"][0], minutes), *p["places"][1:]]
    return q


def _org_reply_rates():
    """{org_id: reply rate} for every org with eligible matches, in one query.

    Eligible: decided, pending after 48 h, or withdrawn after 48 h without a decision
    (counted as not answered in time). Withdrawn within 48 h does not count. Matches of
    deleted volunteers do not count: the org's inbox hides them.
    """
    rows = _db.db.query(
        "SELECT o.org_id AS org_id, "
        "total(m.decided_at IS NOT NULL AND "
        "julianday(m.decided_at) - julianday(m.created_at) <= 2.0) AS fast, "
        "count(*) AS eligible "
        "FROM match m JOIN opening o ON o.id = m.opening_id "
        "JOIN profile p ON p.id = m.volunteer_id "
        "WHERE p.deleted_at IS NULL AND coalesce(m.org_blind, 0) = 0 AND ("
        "m.status IN ('accepted', 'declined') "
        "OR (m.status = 'pending' AND julianday('now') - julianday(m.created_at) > 2.0) "
        "OR (m.status = 'withdrawn' AND (m.decided_at IS NOT NULL "
        "OR julianday(m.withdrawn_at) - julianday(m.created_at) > 2.0))) "
        "GROUP BY o.org_id"
    )
    return {r["org_id"]: _match.reply_rate(r["fast"], r["eligible"]) for r in rows}


KM_PER_MILE = 1.609344


def _distance_label(km):
    """Whole miles for a volunteer to read; the matching works in km."""
    if km is None:
        return None
    miles = km / KM_PER_MILE
    return "< 1 mi" if miles < 1 else f"{round(miles)} mi"


def _work_of(o):
    return {
        "setting": o.setting,
        "effort": o.effort,
        "group_size": o.group_size,
        "first_timer_ok": bool(o.first_timer_ok),
    }


SURPRISE_EVERY = 8  # "Surprise me": about one card in this many is from outside love


def _travel(near):
    """ "25 min by car from Home": the travel estimate to the nearest place."""
    return f"{near[2]} min {TRAVEL_PHRASE[near[0]['mode']]} from {near[0]['label']}"


def _travel_short(near):
    """ "7 min by car": the card's place line, without the place it is from."""
    return f"{near[2]} min {TRAVEL_PHRASE[near[0]['mode']]}"


def _week(starts_at):
    """The Monday (ISO date) of the Los Angeles week, Monday to Sunday, a local start is in."""
    d = date.fromisoformat(starts_at[:10])
    return (d - timedelta(days=d.weekday())).isoformat()


def _full_weeks(me):
    """The weeks (_week) in which the volunteer already has as many shifts booked (not
    cancelled) as their weekly limit; none without a limit."""
    limit = _prefs.weekly_limit(me)
    if not limit:
        return set()
    booked = {}
    for r in _db.db.query(
        "SELECT h.starts_at FROM shift h JOIN match m ON m.id = h.match_id "
        "WHERE m.volunteer_id = ? AND h.status != 'cancelled' AND h.starts_at >= ?",
        [me.id, _week(_local_now())],
    ):
        week = _week(r["starts_at"])
        booked[week] = booked.get(week, 0) + 1
    return {w for w, n in booked.items() if n >= limit}


WEEK_FULL = {"text": "Your week is full", "slug": None}
CONFIRMS_HOURS = {"text": "Confirms your hours", "slug": None}


def deck(uid, limit=20, today=False, view=None, first=None):
    """deck_and_count()'s cards."""
    return deck_and_count(uid, limit, today, view, first)[0]


def deck_and_count(uid, limit=20, today=False, view=None, first=None):
    """Cards for open openings with an active cause, best fit first. today ("Free in the next
    48h"): only shifts starting in the next TODAY_HOURS, soonest first, then nearest.

    With "Surprise me", every SURPRISE_EVERY-th card the volunteer meets (counted by their
    swipes) is the best one with no loved cause. Each card carries 1 to 4 why-chips, the
    distance rounded to whole miles and the travel estimate, never coordinates. Returns
    (cards, how many the deck holds in all). view: the filter sheet's (see _viewed).
    first: an opening id to put on top when the deck holds it (an urgent call's link).
    """
    me = _db.profiles[uid]
    p = _viewed(_deck_prefs(me), view)
    active = {c.slug for c in _db.causes() if c.active}
    rated = {s: v for s, v in p["causes"].items() if s in active}
    loved = {s for s, v in rated.items() if v == _prefs.LOVE}
    skills = json.loads(me.skills or "[]")
    spoken = _prefs.languages(me)
    orgs = {g.id: g for g in _db.orgs()}
    rates = _org_reply_rates()
    confirming = _confirming_orgs()
    ranked = []
    booked = [  # read once: a card within CLASH_HOURS of one of these clashes
        (datetime.fromisoformat(sh["starts_at"]), sh["org"])
        for sh in my_shifts(uid)["upcoming"]
    ]
    calls = _open_calls()
    full = _full_weeks(me)
    drawn = _deck_openings(uid, today, p, calls)
    for o, cause_labels, km, near in drawn:
        slugs = [s for s in json.loads(o.causes or "[]") if s in active]
        fit = _fit(p, rated, skills, spoken, o, slugs, near, rates, confirming)
        if today:  # ignores fit: the soonest, then the nearest; remote after in-person
            key = (o.starts_at, km if km is not None else float("inf"), o.id)
        else:
            key = (-round(fit, 6), o.starts_at, o.id)
        # a week already full last, then urgent calls first
        ranked.append(
            (_week(o.starts_at) in full, o.id not in calls, *key)
            + (o, cause_labels, slugs, near)
        )
    ranked.sort(key=lambda r: r[:5])
    pool = [r[5:] for r in ranked]
    pool.sort(key=lambda r: r[0].id != first)  # stable: the rest keep their order
    seen = _db.swipes.count_where("volunteer_id = ?", [uid])
    surprise = bool(me.surprise) and bool(loved) and not today
    cards = []
    while pool and len(cards) < limit:
        pick, odd = 0, False
        if (
            surprise
            and pool[0][0].id not in calls
            and (seen + len(cards)) % SURPRISE_EVERY == SURPRISE_EVERY - 1
        ):
            outside = [i for i, r in enumerate(pool) if not loved & set(r[2])]
            if outside:
                pick, odd = outside[0], True
        o, cause_labels, slugs, near = pool.pop(pick)
        call = calls.get(o.id)
        # an urgent card says more up top: fewer reasons, so it still fits
        cap = URGENT_WHY if o.id in calls else MAX_WHY
        week_full = _week(o.starts_at) in full
        why, clash = _why(p, rated, spoken, o, cause_labels, slugs, near, odd, booked)
        # the clash and a full week go after the cut: on an urgent card one Like books it
        # (the "confirms your hours" chip is the one that gives way when they don't all fit)
        extra = clash + [CONFIRMS_HOURS] * (o.org_id in confirming)
        extra = extra[: max(len(clash), cap - week_full)]
        why = (
            why[: max(0, cap - week_full - len(extra))]
            + extra
            + [WEEK_FULL] * week_full
        )
        org = orgs.get(o.org_id)
        cards.append(
            {
                "id": o.id,
                "title": o.title,
                "org": org.name if org else "",
                "causes": cause_labels,
                # the same active causes as slugs, in the same order (art and chips)
                "slugs": slugs,
                "starts_at": o.starts_at,
                "duration_minutes": o.duration_minutes,
                "neighborhood": o.neighborhood,
                "slots": o.slots,
                "remote": bool(o.remote),
                "distance": _distance_label(near and near[1]),
                "travel": near and _travel(near),
                "travel_short": near and _travel_short(near),
                # what the volunteer will do (the card's sheet)
                "about": o.about,
                "notes": o.notes,
                "orientation": o.orientation_required,
                "surprise": odd,
                "why": why,
                # the card's picture: the opening's photo, else the org's; its logo
                # as a badge
                "photo": o.photo_key or (org and org.photo_key),
                "logo": org and org.logo_key,
                "event_url": o.event_url,
                "urgent": call
                and urgent_label(call.starts_at, call.needed - _urgent_found(call.id)),
                "call_id": call and call.id,
                # what a like means on an auto-accepting call
                "booked": call
                and call.auto_accept
                and f"Like and you're booked for {urgent_when(call.starts_at)}; "
                "they then see your full name.",
            }
        )
    return cards, len(ranked)


VERIFIED_WEIGHT = 0.15  # "Prefer" nonprofits that confirm hours: this share of the fit


def _fit(p, rated, skills, spoken, o, slugs, near, rates, confirming):
    """match.score of an opening for a volunteer: the deck's order, and who an urgent call
    notifies first. confirming: _confirming_orgs()."""
    when = _match.availability_overlap(
        p["availability"], o.starts_at, o.recurrence, p["free_dates"]
    )
    if not _match.commitment_fits(p["commitment"], o.recurrence):
        when /= 2
    km = near and near[1]
    fit = _match.score(
        causes=(rated, set(slugs)),
        skills=(skills, json.loads(o.skills or "[]")),
        distance=_match.distance_score(km, near and near[0]["scale_km"], o.remote),
        availability=when,
        reply=rates.get(o.org_id, 0.5),
        work=_match.work_fit(p["work"], _work_of(o)),
        language=_match.language_fit(spoken, json.loads(o.languages or "[]")),
    )
    if p["verified_hours"] == "prefer":
        fit = fit * (1 - VERIFIED_WEIGHT) + VERIFIED_WEIGHT * (o.org_id in confirming)
    return fit


MAX_WHY = 4
URGENT_WHY = 2  # an urgent card's reasons: its badge and "booked" line come first
WORK_WORDS = {
    ("setting", "indoor"): "Indoors",
    ("setting", "outdoor"): "Outdoors",
    ("effort", "light"): "Light work",
    ("effort", "moderate"): "Moderate work",
    ("effort", "active"): "Active work",
    ("group_size", "solo"): "On your own",
    ("group_size", "small"): "Small group",
    ("group_size", "big"): "Big group",
}


# Openings have no end time yet: two starts less than this apart clash (a 3 h shift
# plus a 1 h buffer).
CLASH_HOURS = 4


def _why(p, rated, spoken, o, cause_labels, slugs, near, surprise, booked=()):
    """(1 to 4 reasons a card is shown, most telling first: [{"text", "slug"}], slug set on
    a cause chip (drawn in its color); its causes always come first, so there is at least
    one. Then the clash chip in a list, [] when no booked shift is within CLASH_HOURS:
    the caller keeps it past any cut.)
    """
    order = {_prefs.LOVE: 0, _prefs.OPEN: 1}
    why = [
        {
            "text": ("♥ " if rated.get(s) == _prefs.LOVE else "") + label,
            "slug": s,
        }
        for s, label in sorted(
            zip(slugs, cause_labels), key=lambda sl: order.get(rated.get(sl[0]), 2)
        )
    ][:2]
    more = []
    start = datetime.fromisoformat(o.starts_at)
    clash = next(
        (org for at, org in booked if abs(start - at) < timedelta(hours=CLASH_HOURS)),
        None,
    )
    if surprise:
        more.append("Surprise pick")
    if o.remote and not near:
        more.append("Remote")
    if o.starts_at[:10] in p["free_dates"]:
        more.append("You're free that day")
    elif _match.availability_slot(o.starts_at) in _match.day_slots(p["availability"]):
        day, part = _match.availability_slot(o.starts_at).split("_")
        more.append(f"You're free {day.title()} {part}s")
    wanted = p["work"]["first_timer"]
    if o.first_timer_ok and wanted:
        more.append("First-timer friendly")
    for k in _match.work_matches(p["work"], _work_of(o)):
        more.append(WORK_WORDS[(k, getattr(o, k))])
    helps = _match.languages_shared(spoken, json.loads(o.languages or "[]"))
    if helps:
        more.append(f"{LANGUAGE_NAMES[min(helps)]} helps")
    if o.first_timer_ok and not wanted:  # a fact about it, after what matches
        more.append("First-timer friendly")
    chip = [{"text": f"You're booked at {clash} then", "slug": None}] if clash else []
    return (
        why + [{"text": t, "slug": None} for t in more][: MAX_WHY - len(why)],
        chip,
    )


def has_match_news(uid):
    """The Matches tab's dot: since the volunteer last opened Matches, a like of theirs
    is waiting or a nonprofit decided one (never seen: any of those)."""
    seen = _db.profiles[uid].matches_seen_at or ""
    return bool(
        _db.matches.count_where(
            "volunteer_id = ? AND ((status = 'pending' AND created_at > ?) "
            "OR (decided_at IS NOT NULL AND decided_at > ?))",
            [uid, seen, seen],
        )
    )


def saw_matches(uid):
    """They opened Matches: the dot goes until something new happens."""
    _db.profiles.update(id=uid, matches_seen_at=_now())


def new_matches(uid):
    """[{"id", "org", "title", "logo", "photo", "slug", "decided_at"}] of the matches
    accepted since the volunteer last saw a match sheet (never: since their last look at
    Matches), oldest first: the demo's auto-accepts and a coordinator's alike."""
    p = _db.profiles[uid]
    seen = p.match_moment_seen_at or p.matches_seen_at or ""
    out = []
    for m in _db.matches(
        where="volunteer_id = ? AND status = 'accepted' AND decided_at > ?",
        where_args=[uid, seen],
        order_by="decided_at, id",
    ):
        o = _db.openings[m.opening_id]
        org = _db.orgs[o.org_id]
        out.append(
            {
                "id": m.id,
                "org": org.name,
                "title": o.title,
                "decided_at": m.decided_at,
                **_face(org, o),
            }
        )
    return out


def saw_new_matches(uid, rows):
    """The sheet showed these (new_matches' rows): they are not new any more."""
    if rows:
        _db.profiles.update(
            id=uid, match_moment_seen_at=max(r["decided_at"] for r in rows)
        )


def org_name_of(opening_id):
    return _db.orgs[_db.openings[opening_id].org_id].name


def liked_org(uid, opening_id):
    """The org's name when this volunteer liked the opening (a match of theirs is on it,
    blind or not: a block never shows), else None: /onboarding/more names it without
    trusting the id it was given."""
    m = _match_for(uid, opening_id) if _is_id(opening_id) else None
    return org_name_of(opening_id) if m else None


def _filling(match="match"):
    """(SQL, args) for an `AND ...` that keeps the accepted matches that fill an opening
    (and its urgent call): all of them, except in a demo, where a copy's don't count, so
    visitors keep finding the same openings open. match: the alias of the match table.
    Every such count goes through it, so they cannot drift apart."""
    if not demo_matches():
        return "", []
    return (
        f" AND {match}.volunteer_id NOT IN (SELECT id FROM profile WHERE phone LIKE ?)",
        [DEMO_PHONE_PREFIX + "%"],
    )


def _accepted(opening_id):
    skip, args = _filling()
    return _db.matches.count_where(
        f"opening_id = ? AND status = 'accepted'{skip}", [opening_id, *args]
    )


def _sync_fill(opening_id):
    """open <-> filled from accepted count vs slots; a closed opening stays closed."""
    skip, args = _filling()
    _db.db.execute(
        "UPDATE opening SET status = CASE WHEN "
        f"(SELECT count(*) FROM match WHERE opening_id = ? AND status = 'accepted'{skip}) "
        ">= slots THEN 'filled' ELSE 'open' END "
        "WHERE id = ? AND status IN ('open', 'filled')",
        [opening_id, *args, opening_id],
    )


def _match_for(uid, opening_id):
    found = _db.matches(
        where="volunteer_id = ? AND opening_id = ?", where_args=[uid, opening_id]
    )
    return found[0] if found else None


def _swipe(uid, opening_id, liked, today=False, view=None, call_id=None):
    """Record a swipe; a like makes a pending match. (match, created), match None for a pass.

    Repeating a swipe changes nothing. LookupError when the volunteer could not see
    the opening in their deck (closed, inactive cause, unknown id): the deck they swiped
    in (today, view), or their saved deck, since a card drawn under one may be swiped
    after the other changed.

    call_id: the urgent call the card showed. A like counts toward the opening's open
    call only when it is that call (and an auto-accepting call takes it while it has
    places); a like on a card drawn before the call was posted raises CallPosted and
    records nothing, so the volunteer sees the call's card before saying yes to its time.
    A like on the call just after it filled still counts toward it (for its time),
    pending for the org.
    """
    if not _is_id(opening_id):
        raise LookupError("no such opening")
    with _db.db.conn:
        if _db.swipes(
            where="volunteer_id = ? AND opening_id = ?", where_args=[uid, opening_id]
        ):
            return _match_for(uid, opening_id), False
        p = _deck_prefs(_db.profiles[uid])
        drawn = _deck_openings(uid, today, _viewed(p, view))  # the deck it was shown in
        if opening_id not in {o.id for o, *_ in drawn} and (
            not (today or view)
            or opening_id not in {o.id for o, *_ in _deck_openings(uid, False, p)}
        ):
            raise LookupError("no such opening")
        live = _open_calls().get(opening_id)
        if liked and live and call_id != live.id:
            raise CallPosted(live)
        _db.swipes.insert(
            _db.Swipe(
                volunteer_id=uid,
                opening_id=opening_id,
                liked=bool(liked),
                created_at=_now(),
            )
        )
        if not liked:
            return None, False
        call = (
            [live]
            if live
            else (
                _db.urgent_calls(
                    where="id = ? AND opening_id = ? AND status = 'filled' "
                    "AND starts_at > ?",
                    where_args=[call_id, opening_id, _local_now()],
                )
                if _is_id(call_id)
                else []
            )
        )
        m = _db.matches.insert(
            _db.Match(
                opening_id=opening_id,
                volunteer_id=uid,
                status="pending",
                created_at=_now(),
                # a blocked volunteer's like looks normal to them and never reaches the org;
                # in a demo, neither does a real tester's (anyone can be its coordinator)
                org_blind=_org_blocked_volunteer(uid, _db.openings[opening_id].org_id)
                or _hidden_in_demo(uid),
                urgent_call_id=call[0].id if call else None,
            )
        )
        if call and call[0].auto_accept and not m.org_blind:
            _auto_accept(m, call[0])
        return _db.matches[m.id], True


def _auto_accept(m, call):
    """Accept a like on an auto-accepting call while it has places left: the first
    `needed` likes are in, booked at the call's time; the rest stay pending for the org.
    One statement decides, so concurrent likes never take more than `needed` (nor more
    than the opening's slots)."""
    skip, args = _filling()
    _db.db.execute(
        "UPDATE match SET status = 'accepted', decided_at = ?, decided_by = ? "
        "WHERE id = ? AND status = 'pending' "
        f"AND (SELECT count(*) FROM match WHERE urgent_call_id = ? AND status = 'accepted'{skip}) "
        "< (SELECT needed FROM urgent_call WHERE id = ? AND status = 'open' "
        "AND auto_accept = 1) "
        f"AND (SELECT count(*) FROM match WHERE opening_id = ? AND status = 'accepted'{skip}) "
        "< (SELECT slots FROM opening WHERE id = ?)",
        [_now(), call.created_by, m.id, call.id, *args, call.id, m.opening_id, *args,
         m.opening_id],
    )  # fmt: skip
    if _db.db.conn.changes() != 1:
        return
    _sync_fill(m.opening_id)
    _sync_urgent(call.id)
    _new_shift(m.id, call.starts_at, "accept", call.created_by)


def _new_shift(match_id, starts_at, kind, by, minutes=None):
    """A scheduled shift; `minutes` is its length, else the match's opening's (NULL when the
    opening has none)."""
    if minutes is None:
        minutes = _db.openings[_db.matches[match_id].opening_id].duration_minutes
    return _db.shifts.insert(
        _db.Shift(
            match_id=match_id,
            starts_at=starts_at,
            duration_minutes=minutes,
            status="scheduled",
            kind=kind,
            created_by=by,
            created_at=_now(),
        )
    )


def swipe(uid, opening_id, liked, today=False, view=None, call_id=None):
    """Record a swipe; a like makes a pending match. The match, or None for a pass.

    Repeating a swipe changes nothing. LookupError when the volunteer could not see
    the opening in their deck (closed, inactive cause, unknown id).
    """
    return _swipe(uid, opening_id, liked, today, view, call_id)[0]


def swipe_created(uid, opening_id, liked, today=False, view=None, call_id=None):
    """Like `swipe`, but the Match only when this call created it (not a pass or a repeat);
    it is already accepted when an urgent call auto-accepted it. A blind match (the org
    blocked them) comes back too: the volunteer must see what any like shows, and only
    the caller's notice to the org is skipped."""
    match, created = _swipe(uid, opening_id, liked, today, view, call_id)
    return match if created else None


def _like_starts_at(m, o):
    """When match m on opening o is for: a like on an urgent call is for the call's shift."""
    return (
        _db.urgent_calls[m.urgent_call_id].starts_at
        if m.urgent_call_id
        else o.starts_at
    )


def decide(coordinator_uid, match_id, accept):
    """Accept or decline a pending match. PermissionError unless a member of the org.

    ValueError when the match is not pending or no slot is left to accept into.
    """
    m, o = _match_and_opening(match_id)
    _member_of(coordinator_uid, o.org_id)
    if (
        _db.orgs[o.org_id].deactivated_at
        or m.org_blind
        or _org_blocked_volunteer(m.volunteer_id, o.org_id)
    ):
        raise PermissionError("no such match")
    status = "accepted" if accept else "declined"
    skip, args = _filling()
    with _db.db.conn:
        # One statement, so two coordinators can't both take the last slot.
        _db.db.execute(
            "UPDATE match SET status = ?, decided_at = ?, decided_by = ? "
            "WHERE id = ? AND status = 'pending' AND (? = 0 OR "
            f"(SELECT count(*) FROM match WHERE opening_id = ? AND status = 'accepted'{skip}) "
            "< (SELECT slots FROM opening WHERE id = ?))",
            [status, _now(), coordinator_uid, match_id, int(bool(accept)),
             o.id, *args, o.id],
        )  # fmt: skip
        if _db.db.conn.changes() != 1:
            now_status = _db.matches[match_id].status
            raise ValueError(
                "already decided" if now_status != "pending" else "no slots left"
            )
        if accept:
            _sync_fill(o.id)
            _sync_urgent(m.urgent_call_id)
            starts_at = _like_starts_at(m, o)
            # An opening that already started gets no shift: the coordinator schedules one.
            if starts_at > _local_now():
                _new_shift(match_id, starts_at, "accept", coordinator_uid)


def _own_match(uid, match_id):
    """The volunteer's own match row; PermissionError for any other id."""
    m, _ = _match_and_opening(match_id)
    if m.volunteer_id != uid:
        raise PermissionError("not your match")
    return m


def my_match(uid, match_id):
    """{"id", "status", "title"} of the volunteer's own match; PermissionError otherwise."""
    m = _own_match(uid, match_id)
    o = _db.openings[m.opening_id]
    return {
        "id": m.id,
        "status": m.status,
        "title": o.title,
        "org": _db.orgs[o.org_id].name,
    }


def withdraw(uid, match_id):
    """The volunteer's own pending or accepted match becomes withdrawn; the status it had."""
    with _db.db.conn:
        m = _own_match(uid, match_id)
        if m.status not in ("pending", "accepted"):
            raise ValueError("already " + m.status)
        _end_match(m, uid, "withdrawn")
        return m.status


def _face(org, o):
    """The keys ui.OrgFace draws a nonprofit with: its logo, else the opening's photo
    (else the org's), else its first cause's tint. Logos and these photos are public to
    anyone signed in (photo_for)."""
    return {
        "logo": org.logo_key if org else None,
        "photo": o.photo_key or (org.photo_key if org else None),
        "slug": next(iter(json.loads(o.causes or "[]")), None),
    }


# An upcoming shift for a calendar: ui.AddToCalendar and GET /shifts/{id}.ics read it.
_EVENT_SELECT = (
    "SELECT s.id, s.match_id, s.starts_at, s.duration_minutes, s.confirmed_at, o.title, "
    "g.name AS org, "
    "o.neighborhood FROM shift s JOIN match m ON m.id = s.match_id "
    "JOIN opening o ON o.id = m.opening_id JOIN org g ON g.id = o.org_id "
    "WHERE m.status = 'accepted' AND s.status = 'scheduled' AND s.starts_at > ? "
)


def _next_shift(match_id):
    """The match's soonest upcoming shift while it is accepted, as _EVENT_SELECT reads it;
    else None."""
    rows = _db.db.q(
        _EVENT_SELECT + "AND s.match_id = ? ORDER BY s.starts_at LIMIT 1",
        [_local_now(), match_id],
    )
    return rows[0] if rows else None


def shift_event(uid, shift_id):
    """{"id", "match_id", "starts_at", "duration_minutes", "confirmed_at", "title", "org",
    "neighborhood"} of
    this volunteer's own upcoming shift on an accepted match. PermissionError for anything
    else (another volunteer's shift, a cancelled or past one, an unknown id), so the caller
    answers 404."""
    if not _is_id(shift_id):
        raise PermissionError("no such shift")
    rows = _db.db.q(
        _EVENT_SELECT + "AND s.id = ? AND m.volunteer_id = ?",
        [_local_now(), shift_id, uid],
    )
    if not rows:
        raise PermissionError("no such shift")
    return rows[0]


MAX_CANCEL_REASON = 300  # characters of "why", passed on to the org's coordinators


def _gives_up_spot(s):
    """True when cancelling shift s (shift_event's row) ends its match: the only upcoming
    shift of a one-time opening that is open or filled (a closed one has no spot to free),
    so there is nothing left to keep."""
    m = _db.matches[s["match_id"]]
    o = _db.openings[m.opening_id]
    if o.recurrence != "one_time" or o.status not in ("open", "filled"):
        return False
    return not _db.shifts.count_where(
        "match_id = ? AND id != ? AND status = 'scheduled' AND starts_at > ?",
        [m.id, s["id"], _local_now()],
    )


def shift_to_cancel(uid, shift_id):
    """shift_event's row, plus "gives_up_spot" (_gives_up_spot), for the cancel page."""
    s = shift_event(uid, shift_id)
    return {**s, "gives_up_spot": _gives_up_spot(s)}


def cancel_my_shift(uid, shift_id, reason=""):
    """ "Can't make this one": the volunteer's own upcoming shift is cancelled.
    PermissionError as shift_event; ValueError for a reason over MAX_CANCEL_REASON
    characters or holding hidden characters.

    On a recurring or flexible opening the match stays accepted. On a one-time opening
    whose only upcoming shift this is, the match ends as a withdrawal does (_end_match):
    the spot frees up, and when the shift starts within URGENT_HOURS, it refills itself
    (_refill). {"reason": the reason trimmed, "late": whether it was that close,
    "ended": whether the match ended, "call": the UrgentCall posted, or None, "can_call":
    whether, with no call posted, the org could post one for that time now}.
    """
    reason = reason.strip() if isinstance(reason, str) else ""
    if len(reason) > MAX_CANCEL_REASON or hidden_chars(reason, multiline=True):
        raise ValueError("reason")
    with _db.db.conn:
        s = shift_event(uid, shift_id)
        ended = _gives_up_spot(s)
        _db.shifts.update(
            id=s["id"], status="cancelled", cancelled_at=_now(), cancelled_by=uid
        )
        m = _db.matches[s["match_id"]]
        if ended:
            _end_match(m, uid, "withdrawn")
        now = datetime.strptime(_local_now(), _LOCAL_FORMAT)
        soon = (now + timedelta(hours=URGENT_HOURS)).strftime(_LOCAL_FORMAT)
        late = s["starts_at"] <= soon
        call = _refill(m, s["starts_at"]) if late and ended else None
        can_call = (
            late
            and not call
            and not _call_refused(_db.openings[m.opening_id], s["starts_at"], 1)
        )
    return {
        "reason": reason,
        "late": late,
        "ended": ended,
        "call": call,
        "can_call": can_call,
    }


def confirm_my_shift(uid, shift_id):
    """ "Yes, I'll be there": stamps the volunteer's own upcoming shift's confirmed_at,
    once. PermissionError as shift_event."""
    with _db.db.conn:
        s = shift_event(uid, shift_id)
        if not s["confirmed_at"]:
            _db.shifts.update(id=s["id"], confirmed_at=_now())


def my_matches(uid):
    """This volunteer's matches, newest first, with the opening's public details, the
    org's face (_face), and on an accepted match its notes and next shift."""
    org_rows = {o.id: o for o in _db.orgs()}
    out = []
    for m in _db.matches(
        where="volunteer_id = ?", where_args=[uid], order_by="id DESC"
    ):
        o = _db.openings[m.opening_id]
        org = org_rows.get(o.org_id)
        accepted = m.status == "accepted"
        out.append(
            {
                "blocked": _volunteer_blocked_org(uid, o.org_id),
                "id": m.id,
                "status": m.status,
                "title": o.title,
                "org": org.name if org else "",
                "starts_at": _like_starts_at(m, o),
                "duration_minutes": o.duration_minutes,
                "neighborhood": o.neighborhood,
                "event_url": o.event_url,
                "notes": o.notes if accepted else None,
                "shift": _next_shift(m.id) if accepted else None,
                **_face(org, o),
            }
        )
    return out


def org_inbox(coordinator_uid):
    """{"pending": [...], "decided": [...], "past": [...]} for this coordinator's orgs only.

    past: matches that were accepted and then withdrawn or closed; they keep a Report link.

    Each row carries what the volunteer agreed to share (see choices.SHARED_WITH_NONPROFIT):
    their photo (none while a block stands), name ("Sam L.", "Sam Lee" once accepted),
    pronouns, bio, interests, causes, availability (as general buckets), the languages
    they speak, neighborhood, frequency. One query
    selects only those columns, so phone, email and location never leave SQLite.
    """
    skip, args = _filling("a")
    labels = {c.slug: c.label for c in _db.causes()}
    interests = dict(INTERESTS)
    rows = _db.db.query(
        "SELECT m.id, m.status, o.title, o.recurrence, p.display_name, p.causes, "
        "p.availability, "
        "p.neighborhood, p.frequency, p.languages, p.last_name, p.pronouns, p.bio, "
        "p.interests, "
        "p.photo_key, " + _BLOCKED + " AS blocked, "
        "(SELECT count(*) FROM match a WHERE a.opening_id = o.id "
        f"AND a.status = 'accepted'{skip}) >= o.slots AS full "
        "FROM match m JOIN opening o ON o.id = m.opening_id "
        "JOIN profile p ON p.id = m.volunteer_id "
        "WHERE o.org_id IN (SELECT org_id FROM org_member WHERE user_id = ?) "
        "AND p.deleted_at IS NULL AND p.suspended_at IS NULL "
        "AND coalesce(m.org_blind, 0) = 0 "
        "AND (m.status IN ('pending', 'accepted', 'declined') "
        "OR (m.status = 'withdrawn' AND m.decided_at IS NOT NULL)) "
        "AND (m.status = 'withdrawn' OR NOT " + _BLOCKED + ") "
        "ORDER BY m.id DESC",
        [*args, coordinator_uid],
    )
    out = {"pending": [], "decided": [], "past": []}
    for r in rows:
        # After a block either way the row shows BLOCKED_NAME and nothing else of the
        # volunteer: every field is read live, and the volunteer may still edit it.
        blocked = bool(r["blocked"])
        out[
            {"pending": "pending", "withdrawn": "past"}.get(r["status"], "decided")
        ].append(
            {
                "id": r["id"],
                "status": r["status"],
                "opening": r["title"],
                "name": BLOCKED_NAME
                if blocked
                else volunteer_name(
                    r["display_name"], r["last_name"], r["status"] == "accepted"
                ),
                "photo_key": None if blocked else r["photo_key"],
                "pronouns": None if blocked else r["pronouns"],
                "bio": None if blocked else r["bio"],
                "interests": []
                if blocked
                else [
                    interests[i]
                    for i in json.loads(r["interests"] or "[]")
                    if i in interests
                ],
                # A to Z: the stored order (loved first) would hint at the private ratings
                "causes": []
                if blocked
                else sorted(labels.get(s, s) for s in json.loads(r["causes"] or "[]")),
                # a general summary (weekday/weekend parts of the day), never the days
                "availability": []
                if blocked
                else _match.general_buckets(json.loads(r["availability"] or "[]")),
                "languages": []
                if blocked
                else [
                    LANGUAGE_NAMES[k]
                    for k in json.loads(r["languages"] or "[]")
                    if k in LANGUAGE_NAMES
                ],
                "neighborhood": None if blocked else r["neighborhood"],
                "frequency": None if blocked else r["frequency"],
                "full": bool(r["full"]),
                "weekly": r["recurrence"] == "weekly",
            }
        )
    return out


def org_unanswered(coordinator_uid):
    """The chats this coordinator's orgs owe a reply, oldest first: accepted matches whose
    latest message is the volunteer's. Nothing to store: a reply from anyone at the org
    answers it. Chats that can't be written in (a block, a deactivated org) never wait.
    Rows: {"id", "name", "photo_key", "opening", "body", "at"} (at: UTC ISO)."""
    rows = _db.db.query(
        "SELECT m.id, o.title, p.display_name, p.last_name, p.photo_key, "
        "x.body, x.created_at "
        "FROM match m JOIN opening o ON o.id = m.opening_id "
        "JOIN org g ON g.id = o.org_id "
        "JOIN profile p ON p.id = m.volunteer_id "
        "JOIN message x ON x.id = (SELECT max(id) FROM message WHERE match_id = m.id) "
        "WHERE o.org_id IN (SELECT org_id FROM org_member WHERE user_id = ?) "
        "AND m.status = 'accepted' AND x.sender_id = m.volunteer_id "
        "AND p.deleted_at IS NULL AND p.suspended_at IS NULL "
        "AND g.deactivated_at IS NULL AND coalesce(m.org_blind, 0) = 0 "
        "AND NOT " + _BLOCKED + " ORDER BY x.id",
        [coordinator_uid],
    )
    return [
        {
            "id": r["id"],
            "name": volunteer_name(r["display_name"], r["last_name"], True),
            "photo_key": r["photo_key"],
            "opening": r["title"],
            "body": r["body"],
            "at": r["created_at"],
        }
        for r in rows
    ]


MAX_SHIFT_AHEAD = timedelta(days=366)
CORRECTION_WINDOW = timedelta(days=7)


def _member_of(coordinator_uid, org_id):
    if org_id not in coordinator_org_ids(coordinator_uid):
        raise PermissionError("not a member of this org")


def _parse_local(starts_at):
    """A naive LA wall-clock datetime from "YYYY-MM-DDTHH:MM" (":00" seconds allowed).

    ValueError, worded for the coordinator, if malformed or if that time does not exist
    (clocks skip it). A time in the fall-back hour is accepted as written.
    """
    if not isinstance(starts_at, str):
        raise ValueError("pick a date and a time")  # noqa: TRY004
    text = starts_at.strip()
    if len(text) == 19 and text.endswith(":00"):
        text = text[:-3]
    try:
        start = datetime.strptime(text, _LOCAL_FORMAT)
    except ValueError:
        raise ValueError("pick a date and a time") from None
    if (
        start.replace(tzinfo=LA).astimezone(UTC).astimezone(LA).replace(tzinfo=None)
        != start
    ):
        raise ValueError("that time doesn't exist (clocks skip it), pick another")
    return start


def _shift_org(shift_id):
    """(shift, org_id) of any shift; PermissionError for an id that is not one."""
    if not _is_id(shift_id):
        raise PermissionError("no such shift")
    try:
        sh = _db.shifts[shift_id]
        o = _db.openings[_db.matches[sh.match_id].opening_id]
    except NotFoundError:
        raise PermissionError("no such shift") from None
    return sh, o.org_id


def add_shift(coordinator_uid, match_id, starts_at, minutes=None):
    """Schedule another shift on an accepted match of the coordinator's org; the Shift.

    PermissionError unless a member of the org. ValueError unless the match is accepted
    and starts_at ("YYYY-MM-DDTHH:MM", LA time) is real, in the future, within 366 days,
    and not already a shift of this match. `minutes` is the shift's length, one of
    choices.SHIFT_LENGTHS (ValueError otherwise); None takes the opening's.
    """
    m, o = _match_and_opening(match_id)
    _member_of(coordinator_uid, o.org_id)
    if _db.orgs[o.org_id].deactivated_at:
        raise PermissionError("this org is deactivated")
    if m.status != "accepted":
        raise ValueError("the match is not accepted")
    if minutes is not None and str(minutes) not in dict(SHIFT_LENGTHS):
        raise ValueError("pick how long the shift is")
    start = _parse_local(starts_at)
    now = datetime.strptime(_local_now(), _LOCAL_FORMAT)
    if start <= now:
        raise ValueError("the shift has to be in the future")
    if start > now + MAX_SHIFT_AHEAD:
        raise ValueError("the shift must be within the next 12 months")
    try:
        return _new_shift(
            match_id,
            start.strftime(_LOCAL_FORMAT),
            "added",
            coordinator_uid,
            None if minutes is None else int(minutes),
        )
    except apsw.ConstraintError:
        raise ValueError("that shift is already scheduled") from None


def repeat_shift(coordinator_uid, match_id):
    """ "Same time next week": add a shift 7 days after the match's latest live shift.

    Only for a weekly opening. Same errors as add_shift, plus ValueError when the
    opening is not weekly or the match has no shift to repeat.
    """
    _, o = _match_and_opening(match_id)
    _member_of(coordinator_uid, o.org_id)
    if _db.orgs[o.org_id].deactivated_at:
        raise PermissionError("this org is deactivated")
    if o.recurrence != "weekly":
        raise ValueError("this opening does not repeat weekly")
    latest = _db.shifts(
        where="match_id = ? AND status != 'cancelled'",
        where_args=[match_id],
        order_by="starts_at DESC",
        limit=1,
    )
    if not latest:
        raise ValueError("there is no shift to repeat")
    nxt = datetime.strptime(latest[0].starts_at, _LOCAL_FORMAT) + timedelta(days=7)
    return add_shift(
        coordinator_uid,
        match_id,
        nxt.strftime(_LOCAL_FORMAT),
        latest[0].duration_minutes,
    )


def cancel_shift(coordinator_uid, shift_id):
    """Cancel a future scheduled shift of the coordinator's org; the Shift.

    PermissionError unless a member of the org. ValueError unless it is scheduled and
    has not started.
    """
    with _db.db.conn:
        sh, org_id = _shift_org(shift_id)
        _member_of(coordinator_uid, org_id)
        if sh.status != "scheduled" or sh.starts_at <= _local_now():
            raise ValueError("only a shift that has not started can be cancelled")
        _db.shifts.update(
            id=shift_id,
            status="cancelled",
            cancelled_at=_now(),
            cancelled_by=coordinator_uid,
        )
        return _db.shifts[shift_id]


class HoursRefused(ValueError):
    """Attended with hours that can't be confirmed: none on a shift with no length (when
    the caller requires them), or more than the shift's length and the time since it
    started, whichever is longer."""


def complete_shift(
    coordinator_uid, shift_id, attended, minutes=None, require_hours=False
):
    """Mark a started shift completed (attended) or no_show; the Shift.

    Attended confirms the hours: `minutes` (one of choices.SHIFT_HOURS, ValueError
    otherwise) or, when None, the shift's own length (NULL on an old shift: hours unknown,
    or HoursRefused with require_hours). Hours above both the shift's length and the time
    since it started raise HoursRefused, so nobody confirms 12 hours of a 1-hour shift at
    its start. A no-show confirms none.

    PermissionError unless a member of the shift's org. ValueError before the shift
    starts, for a cancelled shift, and for a change after the one correction or more than
    7 days after the first mark. Marking the same result with the same minutes again
    changes nothing; a different result, or different minutes on an attended shift, is
    the one correction.
    """
    with _db.db.conn:
        sh, org_id = _shift_org(shift_id)
        _member_of(coordinator_uid, org_id)
        status = "completed" if attended else "no_show"
        if attended and minutes is not None and str(minutes) not in dict(SHIFT_HOURS):
            raise ValueError("pick the hours from the list")
        confirmed = (
            None
            if not attended
            else sh.duration_minutes
            if minutes is None
            else int(minutes)
        )
        if sh.status == "cancelled":
            raise ValueError("this shift was cancelled")
        if sh.starts_at > _local_now():
            raise ValueError("this shift has not started")
        if attended and confirmed is None and require_hours:
            raise HoursRefused("pick the hours: this shift has no length")
        if confirmed is not None:
            elapsed = _parse_local(_local_now()) - _parse_local(sh.starts_at)
            if confirmed > max(sh.duration_minutes or 0, elapsed.total_seconds() // 60):
                raise HoursRefused(
                    "more hours than the shift and the time since it began"
                )
        if sh.status == status and sh.confirmed_minutes == confirmed:
            return sh
        if sh.status != "scheduled":
            if sh.corrected_at:
                raise ValueError("already corrected once")
            if (
                datetime.fromisoformat(_now()) - datetime.fromisoformat(sh.marked_at)
                > CORRECTION_WINDOW
            ):
                raise ValueError("it is more than 7 days since it was marked")
            _db.shifts.update(
                id=shift_id,
                status=status,
                confirmed_minutes=confirmed,
                corrected_at=_now(),
                corrected_by=coordinator_uid,
            )
        else:
            _db.shifts.update(
                id=shift_id,
                status=status,
                confirmed_minutes=confirmed,
                marked_at=_now(),
                marked_by=coordinator_uid,
            )
        return _db.shifts[shift_id]


# 1 when the volunteer p and the org of opening o blocked each other, either way
_BLOCKED = (
    "EXISTS (SELECT 1 FROM block b WHERE b.blocked_org_id = o.org_id AND "
    "(b.blocked_user_id = p.id OR (b.blocker_id = p.id AND b.blocked_user_id IS NULL)))"
)
_SHIFT_SELECT = (
    "SELECT s.id, s.match_id, s.starts_at, s.duration_minutes, s.confirmed_minutes, "
    "s.status, s.marked_at, "
    "s.corrected_at, s.confirmed_at, o.title, o.recurrence, g.name AS org, p.display_name, "
    "p.last_name, p.photo_key, "
    "m.status AS match_status, " + _BLOCKED + " AS blocked, "
    "g.logo_key AS org_logo, coalesce(o.photo_key, g.photo_key) AS opening_photo, "
    "o.causes "
    "FROM shift s JOIN match m ON m.id = s.match_id "
    "JOIN opening o ON o.id = m.opening_id JOIN org g ON g.id = o.org_id "
    "JOIN profile p ON p.id = m.volunteer_id "
)


def my_shifts(uid):
    """{"upcoming": [...], "past": [...]} of this volunteer's own shifts.

    Upcoming: scheduled, soonest first. Past: everything else but cancelled, latest first;
    a past shift nobody marked yet has status "scheduled". Rows carry the org's face
    (_face's keys).
    """
    now = _local_now()
    out = {"upcoming": [], "past": []}
    for r in _db.db.query(
        _SHIFT_SELECT + "WHERE m.volunteer_id = ? AND s.status != 'cancelled' "
        "ORDER BY s.starts_at, s.id",
        [uid],
    ):
        row = {
            k: r[k]
            for k in (
                "id",
                "match_id",
                "starts_at",
                "duration_minutes",
                "confirmed_minutes",
                "status",
                "title",
                "org",
            )
        }
        row["logo"], row["photo"] = r["org_logo"], r["opening_photo"]
        row["slug"] = next(iter(json.loads(r["causes"] or "[]")), None)
        upcoming = r["status"] == "scheduled" and r["starts_at"] > now
        out["upcoming" if upcoming else "past"].append(row)
    out["past"].reverse()
    return out


def my_hours(uid):
    """{"months": [...], "unconfirmed": [...]}: this volunteer's attended shifts.

    months, newest first: {"month": "2026-10", "minutes": total, "shifts": [...]} of the
    shifts with hours a coordinator confirmed, latest first; each row has starts_at,
    duration_minutes, title, org, minutes and by (the coordinator who last confirmed them, as their profile
    names them, else the org's name). unconfirmed: attended shifts with no confirmed hours
    (marked before hours were asked), which count toward nothing. Like my_shifts, a block
    hides nothing: these are the volunteer's own record.
    """
    out = {"months": [], "unconfirmed": []}
    by_month = {}
    for r in _db.db.query(
        "SELECT s.starts_at, s.duration_minutes, s.confirmed_minutes, o.title, "
        "g.name AS org, c.display_name AS coordinator "
        "FROM shift s JOIN match m ON m.id = s.match_id "
        "JOIN opening o ON o.id = m.opening_id JOIN org g ON g.id = o.org_id "
        "LEFT JOIN profile c ON c.id = coalesce(s.corrected_by, s.marked_by) "
        "WHERE m.volunteer_id = ? AND s.status = 'completed' "
        "ORDER BY s.starts_at DESC, s.id DESC",
        [uid],
    ):
        row = {k: r[k] for k in ("starts_at", "duration_minutes", "title", "org")}
        if r["confirmed_minutes"] is None:
            out["unconfirmed"].append(row)
            continue
        row["minutes"] = r["confirmed_minutes"]
        row["by"] = (r["coordinator"] or "").strip() or r["org"]
        month = by_month.get(r["starts_at"][:7])
        if month is None:
            month = by_month[r["starts_at"][:7]] = {
                "month": r["starts_at"][:7],
                "minutes": 0,
                "shifts": [],
            }
            out["months"].append(month)
        month["minutes"] += row["minutes"]
        month["shifts"].append(row)
    return out


def org_shifts(coordinator_uid):
    """{"upcoming", "to_mark", "marked"} for this coordinator's orgs only.

    to_mark: started and not yet marked. marked: marked within 7 days and still open to
    their one correction. Rows carry the volunteer's name as volunteer_name gives it
    (BLOCKED_NAME under a block) and photo, whether the opening repeats weekly, whether the
    volunteer confirmed they're coming ("confirmed"), and "latest" when the row is its match's latest live shift (set only
    on rows in these lists; an old corrected shift can be the latest but is not listed).
    """
    now = _local_now()
    out = {"upcoming": [], "to_mark": [], "marked": []}
    cutoff = datetime.fromisoformat(_now()) - CORRECTION_WINDOW
    latest = {}
    for r in _db.db.query(
        _SHIFT_SELECT + "WHERE o.org_id IN "
        "(SELECT org_id FROM org_member WHERE user_id = ?) "
        "AND p.deleted_at IS NULL AND p.suspended_at IS NULL AND s.status != 'cancelled' "
        "ORDER BY s.starts_at, s.id",
        [coordinator_uid],
    ):
        row = {
            k: r[k]
            for k in (
                "id",
                "match_id",
                "starts_at",
                "duration_minutes",
                "confirmed_minutes",
                "status",
                "title",
                "org",
            )
        }
        row["name"] = (
            BLOCKED_NAME
            if r["blocked"]
            else volunteer_name(
                r["display_name"], r["last_name"], r["match_status"] == "accepted"
            )
        )
        row["photo_key"] = None if r["blocked"] else r["photo_key"]
        row["weekly"] = r["recurrence"] == "weekly"
        row["confirmed"] = bool(r["confirmed_at"])
        if r["status"] == "scheduled":
            out["upcoming" if r["starts_at"] > now else "to_mark"].append(row)
        elif not r["corrected_at"] and datetime.fromisoformat(r["marked_at"]) >= cutoff:
            out["marked"].append(row)
        # ordered by start, so the last live row of a match is its latest shift
        latest[r["match_id"]] = row
    for row in latest.values():
        row["latest"] = True
    return out


MAX_MESSAGE = 2000


def chat_access(uid, match_id, write=False):
    """ "volunteer" or "coordinator": how this user takes part in the match's chat.

    PermissionError for anyone else, and for a match that never had a chat (pending,
    declined, or withdrawn before it was accepted). With write=True the match must be
    accepted: once it is withdrawn the history is read-only for both sides.
    """
    m, o = _match_and_opening(match_id)
    if m.volunteer_id == uid:
        role = "volunteer"
    elif o.org_id in coordinator_org_ids(uid):
        role = "coordinator"
    else:
        raise PermissionError("not part of this match")
    if m.status == "accepted" or (m.status == "withdrawn" and m.decided_at):
        if write and m.status != "accepted":
            raise PermissionError("this chat is read-only")
        if write and (
            _db.orgs[o.org_id].deactivated_at
            or _blocked_either(m.volunteer_id, o.org_id)
        ):
            raise PermissionError("this chat is read-only")
        return role
    raise PermissionError("no chat on this match")


def _first_name(name):
    parts = (name or "").split(maxsplit=1)
    return parts[0] if parts else "Volunteer"


# What an org sees for a volunteer after a block either way: no live field at all, so the
# volunteer can't keep changing what that org reads (their first name included)
BLOCKED_NAME = "Volunteer"


def volunteer_name(first, last, full=False):
    """What a nonprofit calls a volunteer: "Sam L." until it accepts them, "Sam Lee" once
    it has (full); just "Sam" when they gave no last name."""
    first, last = _first_name(first), (last or "").strip()
    initial = next((c for c in last if c.isalpha()), None)
    if not initial:
        return first
    return f"{first} {last}" if full else f"{first} {initial.upper()}."


def _message_row(msg, volunteer_id, names, blocked=False):
    """blocked: a block stands between the volunteer and the org, so the volunteer's
    messages carry BLOCKED_NAME, not their live (and still editable) first name."""
    if msg.sender_id != volunteer_id:
        sender = names.get(msg.sender_id) or "Coordinator"
    else:
        sender = BLOCKED_NAME if blocked else _first_name(names.get(msg.sender_id))
    return {
        "id": msg.id,
        "sender_uid": msg.sender_id,
        "sender": sender,
        "body": msg.body,
        "created_at": msg.created_at,
    }


def _names(ids):
    return {i: p.display_name for i in ids if (p := get_profile(i))}


def send_message(uid, match_id, body):
    """Save a message from a participant of an accepted match and return it.

    ValueError for a non-string, an empty message, one over 2000 characters, or one
    holding a bidi control;
    PermissionError when this user may not write in this chat.
    """
    if not isinstance(body, str):
        raise ValueError("message must be text")  # noqa: TRY004
    body = body.strip()
    if not body or len(body) > MAX_MESSAGE:
        raise ValueError(f"message must be 1 to {MAX_MESSAGE} characters")
    if BIDI.search(body):  # a bidi control can make a message read as something else
        raise ValueError("message holds a character we can't show")
    chat_access(uid, match_id, write=True)
    msg = _db.messages.insert(
        match_id=match_id, sender_id=uid, body=body, created_at=_now()
    )
    m = _db.matches[match_id]
    return _message_row(msg, m.volunteer_id, _names([uid]), _match_blocked(m))


def list_messages(uid, match_id, limit=200):
    """The latest `limit` messages of the chat, oldest first."""
    chat_access(uid, match_id)
    m = _db.matches[match_id]
    rows = _db.messages(
        where="match_id = ?",
        where_args=[match_id],
        order_by="id DESC",
        limit=max(1, min(int(limit), 1000)),
    )[::-1]
    names = _names({r.sender_id for r in rows})
    blocked = _match_blocked(m)
    return [_message_row(r, m.volunteer_id, names, blocked) for r in rows]


def _match_blocked(m):
    """True when a block stands either way between a match's volunteer and its org."""
    return _blocked_either(m.volunteer_id, _db.openings[m.opening_id].org_id)


def chat_header(uid, match_id):
    """What the chat page shows about the other side.

    The volunteer sees the org name and the contact name of the coordinator who accepted
    them; the coordinator sees the volunteer's name as volunteer_name gives it, and their
    photo when data.photo_for would serve it. Also whether the caller may still write,
    the org's face (_face), and while accepted the opening's notes and next shift.
    """
    role = chat_access(uid, match_id)
    m = _db.matches[match_id]
    o = _db.openings[m.opening_id]
    the_org = _db.orgs[o.org_id]
    org = the_org.name
    photo = None
    if role == "volunteer":
        decider = get_profile(m.decided_by) if m.decided_by else None
        other = decider.display_name if decider and decider.display_name else org
    else:
        volunteer = get_profile(m.volunteer_id)
        # what photo_for hides hides the last name too, and so does a block with the
        # org of this match (photo_for looks at all of the coordinator's orgs)
        blocked = bool(volunteer) and _blocked_either(volunteer.id, o.org_id)
        seen = (
            bool(volunteer)
            and not blocked
            and _sees_volunteer(get_profile(uid), volunteer.id)
        )
        other = (
            BLOCKED_NAME
            if blocked
            else volunteer_name(
                volunteer.display_name if volunteer else "",
                volunteer.last_name if seen else "",
                m.status == "accepted",
            )
        )
        if seen:
            photo = volunteer.photo_key
    return {
        "role": role,
        "title": o.title,
        "org": org,
        "other": other,
        "photo_key": photo,  # the volunteer's, for the coordinator when they may see it
        "writable": m.status == "accepted",
        "duration_minutes": o.duration_minutes,  # the opening's shift length, if said
        "notes": o.notes if m.status == "accepted" else None,
        "shift": _next_shift(m.id) if m.status == "accepted" else None,
        **_face(the_org, o),
    }


def active_causes():
    return [c for c in _db.causes(order_by="sort") if c.active]


def get_or_create_volunteer(phone):
    """Profile id for this E.164 phone, creating a volunteer profile on first sign-in."""
    where = {"where": "phone = ? AND deleted_at IS NULL", "where_args": [phone]}
    if found := _db.profiles(**where):
        return found[0].id
    # A number whose deleted account had a block, a report or a suspension against it comes
    # back with them: suspended stays suspended, org blocks apply to the new profile.
    kept = _db.retained_phones(where="hash = ?", where_args=[_phone_hash(phone)])
    try:
        with _db.db.conn:
            uid = _db.profiles.insert(
                _db.Profile(
                    role="volunteer",
                    phone=phone,
                    session_version=1,
                    created_at=_now(),
                    suspended_at=_now() if kept and kept[0].suspended else None,
                )
            ).id
            for blocker_id, org_id in json.loads(kept[0].blocks_json) if kept else []:
                _add_block(
                    blocker_id=blocker_id, blocked_user_id=uid, blocked_org_id=org_id
                )
        return uid
    except apsw.ConstraintError:  # a concurrent first sign-in won the race
        return _db.profiles(**where)[0].id


def phone_suspended(phone):
    """True when a live profile holds this number and an admin suspended it."""
    return bool(
        _db.profiles.count_where(
            "phone = ? AND deleted_at IS NULL AND suspended_at IS NOT NULL", [phone]
        )
    )


def sign_out_everywhere(uid):
    """Invalidate every session cookie this profile holds."""
    _db.profiles.update(
        id=uid, session_version=(_db.profiles[uid].session_version or 1) + 1
    )


def get_profile(uid):
    try:
        p = _db.profiles[uid]
    except NotFoundError:
        return None
    return None if p.deleted_at or p.suspended_at else p


def save_onboarding(
    uid,
    *,
    display_name,
    email="",
    neighborhood="",
    lat,
    lon,
    radius_km,
    causes,
    other_causes="",
    availability=(),
    skills=(),
    volunteered_last_year="",
    frequency="",
    heard_from="",
    cause_prefs=None,
    place=None,
    last_name="",
):
    """cause_prefs: {slug: love|open|no} (default: causes, all loved). place: the Home
    place (label, lat, lon, area, mode, max_minutes), saved as the volunteer's first place;
    without one, the profile's lat, lon and radius_km read as Home."""
    rated = (
        cause_prefs if cause_prefs is not None else dict.fromkeys(causes, _prefs.LOVE)
    )
    with _db.db.conn:
        _db.db.execute("DELETE FROM place WHERE user_id = ?", [uid])
        if place:
            _db.places.insert(_db.Place(user_id=uid, **place))
        _db.profiles.update(
            id=uid,
            display_name=display_name,
            last_name=last_name or None,
            email=email,
            neighborhood=neighborhood,
            volunteered_last_year=volunteered_last_year,
            adult_attested=True,
            lat=lat,
            lon=lon,
            radius_km=radius_km,
            causes=json.dumps(_prefs.picked(rated)),
            cause_prefs=json.dumps(rated),
            other_causes=other_causes,
            availability=json.dumps(list(availability)),
            skills=json.dumps(list(skills)),
            frequency=frequency,
            heard_from=heard_from,
            onboarded_at=_now(),
        )


def save_about(uid, heard_from, volunteered_last_year=""):
    """The answers of the one optional card after the first Like; heard_from is never
    blank here, so the card is asked once."""
    _volunteer(uid)
    _db.profiles.update(
        id=uid, heard_from=heard_from, volunteered_last_year=volunteered_last_year
    )


def preferences(uid):
    """The volunteer's match preferences, as /me/preferences shows them."""
    p = _db.profiles[uid]
    return {
        "causes": _prefs.cause_prefs(p),
        "availability": _match.day_slots(json.loads(p.availability or "[]")),
        "free_dates": _prefs.free_dates(p, local_today()),
        "horizon_days": _prefs.horizon_days(p),
        "commitment": _prefs.commitment(p),
        **_prefs.work(p),
        "orientation_ok": _prefs.orientation_ok(p),
        "languages": _prefs.languages(p),
        "dealbreakers": _prefs.dealbreakers(p),
        "verified_hours": _prefs.verified_hours(p),
        "surprise": bool(p.surprise),
        "skills": json.loads(p.skills or "[]"),
        "frequency": p.frequency or "",
        "places": _places(p),
        "urgent": _prefs.urgent(p),
        "weekly_limit": _prefs.weekly_limit(p),
    }


def save_preferences(
    uid,
    *,
    causes,
    availability,
    skills,
    frequency,
    places,
    free_dates=(),
    horizon_days=0,
    commitment="either",
    setting="either",
    effort="any",
    group_size="any",
    first_timer=False,
    orientation_ok=True,
    languages=(),
    dealbreakers=DEFAULT_DEALBREAKERS,
    verified_hours="",
    surprise=False,
    urgent=None,
    weekly_limit=None,
):
    """causes: {slug: love|open|no}; a cause left out is unrated. places: 1 or 2 dicts
    (label, lat, lon, area, mode, max_minutes), the first one Home; they replace the saved
    ones. The profile's own point follows the first place. urgent: last-minute calls as
    prefs.urgent() reads them; None keeps the saved ones. weekly_limit: 0 (off) to 5; None
    keeps the saved one."""
    with _db.db.conn:
        _db.db.execute("DELETE FROM place WHERE user_id = ?", [uid])
        for p in places:
            _db.places.insert(_db.Place(user_id=uid, **p))
        _db.profiles.update(
            id=uid,
            cause_prefs=json.dumps(causes),
            causes=json.dumps(_prefs.picked(causes)),
            availability=json.dumps(list(availability)),
            free_dates=json.dumps(sorted(free_dates)),
            horizon_days=horizon_days,
            commitment=commitment,
            setting=setting,
            effort=effort,
            group_size=group_size,
            first_timer=first_timer,
            orientation_ok=orientation_ok,
            languages=json.dumps(sorted(set(languages))),
            dealbreakers=json.dumps(list(dealbreakers)),
            verified_hours=verified_hours or None,
            surprise=surprise,
            skills=json.dumps(list(skills)),
            frequency=frequency,
            lat=places[0]["lat"],
            lon=places[0]["lon"],
            **(
                {f"urgent_{k}": v for k, v in urgent.items()}
                if urgent is not None
                else {}
            ),
            **({} if weekly_limit is None else {"weekly_limit": weekly_limit}),
        )


def save_profile_details(
    uid,
    *,
    first_name,
    last_name,
    pronouns,
    bio,
    interests,
    neighborhood=None,
    email=None,
    other_causes=None,
):
    """A volunteer's own name, pronouns, bio, interests and, unless None, the neighborhood
    nonprofits see, the email and the other causes they asked for (checked by the caller)."""
    _volunteer(uid)
    _db.profiles.update(
        id=uid,
        display_name=first_name,
        last_name=last_name or None,
        pronouns=pronouns or None,
        bio=bio or None,
        interests=json.dumps(list(interests)),
        **({} if neighborhood is None else {"neighborhood": neighborhood}),
        **({} if email is None else {"email": email}),
        **({} if other_causes is None else {"other_causes": other_causes}),
    )


TOKEN_MINUTES = 15


def _now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def org_exists(org_id):
    return bool(_db.orgs(where="id = ?", where_args=[org_id]))


def add_coordinator(email, org_id, role="coordinator", name="", phone=""):
    """Admin-only entry point (scripts/add_coordinator.py): profile id of the coordinator.

    name is the contact name shown to volunteers; phone (E.164) is optional.
    """
    if role not in ("owner", "coordinator"):
        raise ValueError(role)
    if not org_exists(org_id):
        raise LookupError(f"no org {org_id}")
    email = email.strip().lower()
    if admin_by_email(email):
        raise ValueError("that address is an admin")
    found = coordinator_by_email(email)
    if found:
        uid = found
    else:
        uid = _db.profiles.insert(
            _db.Profile(
                role="coordinator",
                email=email,
                display_name=name,
                contact_phone=phone,
                session_version=1,
                created_at=_now(),
            )
        ).id
        found = None
    if found and (name or phone):
        set_coordinator_details(found, name or get_profile(found).display_name, phone)
    if not _db.org_members(
        where="org_id = ? AND user_id = ?", where_args=[org_id, uid]
    ):
        _db.org_members.insert({"org_id": org_id, "user_id": uid, "role": role})
    return uid


def set_coordinator_details(uid, name, phone=""):
    """Contact name (required) and phone (E.164, optional) on a coordinator's profile."""
    if not name:
        raise ValueError("name")
    if (p := get_profile(uid)) is None or p.role != "coordinator":
        raise PermissionError("not a coordinator")
    _db.profiles.update(id=uid, display_name=name, contact_phone=phone)


def coordinator_by_email(email):
    return _staff(email, "coordinator")


def coordinator_org_ids(uid):
    return [m.org_id for m in _db.org_members(where="user_id = ?", where_args=[uid])]


def coordinator_orgs(uid):
    """[(org, member role)] for the orgs this coordinator belongs to."""
    by_id = {o.id: o for o in _db.orgs()}
    return [
        (by_id[m.org_id], m.role)
        for m in _db.org_members(
            where="user_id = ?", where_args=[uid], order_by="org_id"
        )
        if m.org_id in by_id
    ]


def add_admin(email, name):
    """Admin-only entry point (scripts/add_admin.py): profile id of the admin.

    Signs in through the same emailed link as a coordinator. ValueError when the address
    already belongs to a coordinator: one address, one role.
    """
    email = email.strip().lower()
    if not name:
        raise ValueError("name")
    if coordinator_by_email(email):
        raise ValueError("that address is a coordinator")
    found = _staff(email, "admin")
    if found:
        _db.profiles.update(id=found, display_name=name)
        return found
    return _db.profiles.insert(
        _db.Profile(
            role="admin",
            email=email,
            display_name=name,
            session_version=1,
            created_at=_now(),
        )
    ).id


def _staff(email, role):
    found = _db.profiles(
        where="role = ? AND lower(email) = ? AND deleted_at IS NULL",
        where_args=[role, email.strip().lower()],
    )
    return found[0].id if found else None


def remove_admin(email):
    """Revoke an admin: the profile is soft-deleted, so their session ends at once.
    LookupError when there is no such admin."""
    uid = _staff(email, "admin")
    if not uid:
        raise LookupError(f"no admin {email}")
    _db.profiles.update(id=uid, deleted_at=_now())
    return uid


def admin_by_email(email):
    return _staff(email, "admin")


def metrics_rows():
    """Plain rows for app.metrics.pilot_metrics: no names, phones or emails, only the
    attributes the breakdowns need. Soft-deleted volunteers are included."""
    return fetch_rows(_db.db.q)


def create_login_token(uid):
    """A fresh single-use token for the emailer; only its SHA-256 is stored."""
    token = secrets.token_urlsafe(32)
    _db.login_tokens.delete_where("user_id = ? OR expires_at < ?", [uid, _now()])
    _db.login_tokens.insert(
        _db.LoginToken(
            token_hash=_hash(token),
            user_id=uid,
            expires_at=(datetime.now(UTC) + timedelta(minutes=TOKEN_MINUTES)).isoformat(
                timespec="seconds"
            ),
        )
    )
    return token


def consume_login_token(token):
    """User id the token was issued to, or None. Marks it used in one statement."""
    rows = _db.db.q(
        "UPDATE login_token SET used_at = ? WHERE token_hash = ? "
        "AND used_at IS NULL AND expires_at > ? RETURNING user_id",
        [_now(), _hash(token), _now()],
    )
    return rows[0]["user_id"] if rows else None


def create_opening(
    coordinator_uid,
    org_id,
    *,
    title,
    about,
    cause,
    lat,
    lon,
    neighborhood,
    starts_at,
    recurrence,
    slots,
    orientation_required,
    notes,
    reply_channel,
    duration_minutes=None,
    skills=(),
    setting="",
    effort="",
    group_size="",
    first_timer_ok=False,
    languages=(),
):
    """Opening id. Only an org member may post; the status always starts open. The kind
    of work (setting ... languages) is optional: "" is not said.

    ValueError without coordinates: an in-person opening needs a place to be ranked.
    """
    _member_of(coordinator_uid, org_id)
    if lat is None or lon is None:
        raise ValueError("an opening needs coordinates")
    if _db.orgs[org_id].deactivated_at:
        raise PermissionError("this org is deactivated")
    return _db.openings.insert(
        _db.Opening(
            org_id=org_id,
            title=title,
            about=about,
            causes=json.dumps([cause]),
            skills=json.dumps(sorted(set(skills))),
            lat=lat,
            lon=lon,
            neighborhood=neighborhood,
            remote=False,
            starts_at=starts_at,
            duration_minutes=duration_minutes,
            recurrence=recurrence,
            slots=slots,
            orientation_required=orientation_required,
            status="open",
            notes=notes,
            reply_channel=reply_channel,
            adult_confirmed=True,
            created_at=_now(),
            setting=setting,
            effort=effort,
            group_size=group_size,
            first_timer_ok=first_timer_ok,
            languages=json.dumps(sorted(set(languages))),
        )
    ).id


def org_openings(coordinator_uid):
    """{org id: [openings, soonest first]} for the coordinator's own orgs only."""
    mine = coordinator_org_ids(coordinator_uid)
    return {
        org_id: _db.openings(
            where="org_id = ?", where_args=[org_id], order_by="starts_at DESC"
        )
        for org_id in mine
    }


def opening_counts(coordinator_uid):
    """{opening id: {"pending": n, "accepted": n}} for the coordinator's own orgs only.

    pending counts the likes their inbox lists as waiting (org_inbox's filters);
    accepted counts the accepted matches that fill the opening (_filling), as the full check does.
    """
    skip, args = _filling("a")
    rows = _db.db.query(
        "SELECT o.id AS id, "
        "(SELECT count(*) FROM match m JOIN profile p ON p.id = m.volunteer_id "
        "WHERE m.opening_id = o.id AND m.status = 'pending' "
        "AND p.deleted_at IS NULL AND p.suspended_at IS NULL "
        "AND coalesce(m.org_blind, 0) = 0 "
        "AND NOT " + _BLOCKED + ") AS pending, "
        "(SELECT count(*) FROM match a WHERE a.opening_id = o.id "
        f"AND a.status = 'accepted'{skip}) AS accepted "
        "FROM opening o "
        "WHERE o.org_id IN (SELECT org_id FROM org_member WHERE user_id = ?)",
        [*args, coordinator_uid],
    )
    return {r["id"]: {"pending": r["pending"], "accepted": r["accepted"]} for r in rows}


def org_waiting(coordinator_uid):
    """What waits for the coordinator's answer (the nav's Inbox badge): the likes, the
    same counts as the dashboard's cards so the two never disagree, plus the chats owed
    a reply."""
    pending = sum(c["pending"] for c in opening_counts(coordinator_uid).values())
    return pending + len(org_unanswered(coordinator_uid))


def close_opening(coordinator_uid, opening_id):
    coordinator_opening(coordinator_uid, opening_id)
    with _db.db.conn:
        _db.openings.update(id=opening_id, status="closed")
        _end_urgent(opening_id, "closed")
        _db.db.execute(
            "UPDATE shift SET status = 'cancelled', cancelled_at = ?, cancelled_by = ? "
            "WHERE status = 'scheduled' AND starts_at > ? AND match_id IN "
            "(SELECT id FROM match WHERE opening_id = ?)",
            [_now(), coordinator_uid, _local_now(), opening_id],
        )


URGENT_HOURS = 48  # an urgent call is for a shift starting this soon
URGENT_PER_DAY = 3  # urgent calls an org may post in any 24 hours


def _urgent_sweep():
    """Close the open calls whose time came or whose opening is no longer open; a filled
    opening fills its call."""
    _db.db.execute(
        "UPDATE urgent_call SET status = CASE WHEN "
        "(SELECT status FROM opening WHERE id = opening_id) = 'filled' "
        "THEN 'filled' ELSE 'closed' END, ended_at = ? "
        "WHERE status = 'open' AND (starts_at <= ? OR "
        "(SELECT status FROM opening WHERE id = opening_id) != 'open')",
        [_now(), _local_now()],
    )


# a call still open: not filled or stopped, its time ahead, its opening open. Reads go by
# this; _urgent_sweep stamps the others' status and ended_at, on writes and in the task.
_LIVE = (
    "status = 'open' AND starts_at > ? "
    "AND opening_id IN (SELECT id FROM opening WHERE status = 'open')"
)


def _open_calls():
    """{opening id: its open UrgentCall}. Writes nothing."""
    return {
        c.opening_id: c
        for c in _db.urgent_calls(where=_LIVE, where_args=[_local_now()])
    }


def _urgent_found(call_id):
    """How many likes on the call were accepted."""
    skip, args = _filling()
    return _db.matches.count_where(
        f"urgent_call_id = ? AND status = 'accepted'{skip}", [call_id, *args]
    )


def _sync_urgent(call_id):
    """open -> filled once `needed` likes on it were accepted; then the sweep, which fills
    a call whose opening just filled (call_id None: only the sweep)."""
    if call_id is not None:
        call = _db.urgent_calls[call_id]
        if call.status == "open" and _urgent_found(call_id) >= call.needed:
            _db.urgent_calls.update(id=call_id, status="filled", ended_at=_now())
    _urgent_sweep()


def _end_urgent(opening_id, status):
    _db.db.execute(
        "UPDATE urgent_call SET status = ?, ended_at = ? "
        "WHERE opening_id = ? AND status = 'open'",
        [status, _now(), opening_id],
    )


def open_spots(coordinator_uid, opening_id):
    """How many more people the opening can take (slots - accepted), for the call form."""
    o = coordinator_opening(coordinator_uid, opening_id)
    return max(0, o.slots - _accepted(o.id))


def post_urgent_call(coordinator_uid, opening_id, starts_at, needed, auto_accept):
    """Post "need `needed` more for `starts_at`" on an open opening; the call's id.

    starts_at: LA wall clock "YYYY-MM-DDTHH:MM", after now and within URGENT_HOURS.
    ValueError, its message one of: "not open" (the opening is filled or closed),
    "already" (it has an open call), "time", "needed" (1 to its open spots), "limit"
    (URGENT_PER_DAY calls in the last 24 hours by the org).
    """
    o = coordinator_opening(coordinator_uid, opening_id)
    if _db.orgs[o.org_id].deactivated_at:
        raise PermissionError("no such opening")
    return _post_call(o, starts_at, needed, auto_accept, coordinator_uid)


def _call_refused(o, starts_at, needed):
    """Why post_urgent_call would refuse this call on opening o (its ValueError message),
    or None when it may go up."""
    _urgent_sweep()
    if o.status != "open":
        return "not open"
    if o.id in _open_calls():
        return "already"
    now = datetime.now(LA)
    latest = (now + timedelta(hours=URGENT_HOURS)).strftime(_LOCAL_FORMAT)
    if not now.strftime(_LOCAL_FORMAT) < starts_at <= latest:
        return "time"
    if not 1 <= needed <= o.slots - _accepted(o.id):
        return "needed"
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    if (
        _db.urgent_calls.count_where("org_id = ? AND created_at > ?", [o.org_id, since])
        >= URGENT_PER_DAY
    ):
        return "limit"
    return None


def _post_call(o, starts_at, needed, auto_accept, by, refill=False):
    """post_urgent_call's rules and insert, for opening o, posted by `by`."""
    with _db.db.conn:
        refused = _call_refused(o, starts_at, needed)
        if refused:
            raise ValueError(refused)
        return _db.urgent_calls.insert(
            _db.UrgentCall(
                opening_id=o.id,
                org_id=o.org_id,
                starts_at=starts_at,
                needed=needed,
                auto_accept=bool(auto_accept),
                status="open",
                created_by=by,
                created_at=_now(),
                refill=refill,
            )
        ).id


def _refill(m, starts_at):
    """A late cancel's urgent call: 1 person on match m's opening at starts_at, posted
    for the coordinator who accepted m under post_urgent_call's rules, auto-accepting when
    the org's last call did (else not). It counts toward the org's URGENT_PER_DAY. Once
    per opening and time: none when a refill was posted for them before, so a refill's
    own volunteer cancelling again can't send more texts (the org's own calls don't
    count). The UrgentCall, or None when the rules say no (no open spot, another open
    call, the daily limit) or the org is deactivated."""
    o = _db.openings[m.opening_id]
    if _db.orgs[o.org_id].deactivated_at or _db.urgent_calls.count_where(
        "opening_id = ? AND starts_at = ? AND refill = 1", [o.id, starts_at]
    ):
        return None
    last = _db.urgent_calls(
        where="org_id = ?", where_args=[o.org_id], order_by="id DESC", limit=1
    )
    auto = bool(last and last[0].auto_accept)
    try:
        return _db.urgent_calls[
            _post_call(o, starts_at, 1, auto, m.decided_by, refill=True)
        ]
    except ValueError:
        return None


def close_urgent_call(coordinator_uid, opening_id):
    """Stop the opening's open call, if it has one."""
    o = coordinator_opening(coordinator_uid, opening_id)
    with _db.db.conn:
        _end_urgent(o.id, "closed")


def org_urgent_calls(coordinator_uid):
    """{opening id: {"call": its open UrgentCall, "found": accepted likes on it}} for
    the coordinator's own orgs only."""
    mine = coordinator_org_ids(coordinator_uid)
    return {
        oid: {"call": c, "found": _urgent_found(c.id)}
        for oid, c in _open_calls().items()
        if c.org_id in mine
    }


def urgent_when(starts_at, today=None):
    """ "today 9 am", "tomorrow 2:30 pm", or the weekday; today: a date, else now's."""
    start = datetime.fromisoformat(starts_at)
    today = today or datetime.now(LA).date()
    day = {0: "today", 1: "tomorrow"}.get(
        (start.date() - today).days, start.strftime("%a")
    )
    clock = start.strftime("%-I:%M %p" if start.minute else "%-I %p").lower()
    return f"{day} {clock}"


def urgent_label(starts_at, left):
    """ "Urgent · today 9 am · needs 2": the deck's badge and the org's pill."""
    return f"Urgent · {urgent_when(starts_at)} · needs {left}"


URGENT_WAVES = 2  # notification waves per call
URGENT_WAVE_GAP = timedelta(minutes=30)  # wave 2 goes out this long after wave 1
URGENT_PER_NEEDED = 3  # a wave notifies up to 3 x the people still needed ...
URGENT_WAVE_MAX = 25  # ... and never more than this many
URGENT_WEEK_MAX = 3  # urgent notices a volunteer gets in 7 days, whatever they picked
URGENT_BUSY = timedelta(
    hours=2
)  # a volunteer with a shift this close to it isn't asked
# a notice in these states counts as given (a deferred or failed one may go again)
_GIVEN = "('pending', 'sent', 'unknown', 'skipped')"


def urgent_waves_due(at):
    """[(call id, waves sent so far, how many to notify)] for the open calls whose next
    wave is due at `at` (UTC): wave 1 at once, wave 2 URGENT_WAVE_GAP after wave 1 while
    still unfilled. Closes the stale calls first (the task's sweep). Nothing is claimed:
    urgent_claim_wave does that once there is someone to notify, so a wave that would
    reach no one (all in their quiet hours, say) waits for a later sweep."""
    _urgent_sweep()
    due = []
    for call in _open_calls().values():
        done = call.waves or 0
        if done >= URGENT_WAVES or (
            done and datetime.fromisoformat(call.wave_at) + URGENT_WAVE_GAP > at
        ):
            continue
        left = call.needed - _urgent_found(call.id)
        if left > 0:
            due.append((call.id, done, min(URGENT_PER_NEEDED * left, URGENT_WAVE_MAX)))
    return due


def urgent_claim_wave(call_id, done, at):
    """Claim the call's next wave (after `done`) at `at`: True for exactly one caller, so
    two sweeps never send the same wave."""
    _db.db.execute(
        "UPDATE urgent_call SET waves = ?, wave_at = ? "
        "WHERE id = ? AND coalesce(waves, 0) = ?",
        [done + 1, at.isoformat(timespec="seconds"), call_id, done],
    )
    return _db.db.conn.changes() == 1


def urgent_recipients(call_id, limit, at):
    """[(volunteer id, "sms" or "email")] to notify about the call at `at` (UTC), best
    fit first (match.score), at most `limit`.

    Eligible: opted in; the call's opening is in their deck (verified org, not blocked by
    them, no cause "not for me", their Only-show choices, not swiped yet); the org hasn't
    blocked them; within their travel minutes of a place; free then (a ticked slot or a
    free date); the window they picked; outside their quiet hours; under their weekly
    cap; not already notified about this call; no shift of theirs within
    URGENT_BUSY of it; their weekly limit of shifts not reached in the call's week; a
    number that never replied STOP, else an email.
    """
    calls = _open_calls()  # read once for every volunteer's deck below
    call = _db.urgent_calls[call_id]
    if calls.get(call.opening_id) != call:
        return []
    o = copy.copy(_db.openings[call.opening_id])
    o.starts_at = call.starts_at
    local = at.astimezone(LA)
    week_ago = (at - timedelta(days=7)).isoformat(timespec="seconds")
    notified = {
        r["recipient_id"]
        for r in _db.db.query(
            f"SELECT recipient_id FROM notification WHERE key LIKE ? AND status IN {_GIVEN}",
            [f"urgent:{call_id}:%"],
        )
    }
    this_week = {
        r["recipient_id"]: r["n"]
        for r in _db.db.query(
            "SELECT recipient_id, count(*) AS n FROM notification WHERE kind = 'urgent' "
            f"AND status IN {_GIVEN} AND sent_at > ? GROUP BY recipient_id",
            [week_ago],
        )
    }
    start = datetime.fromisoformat(call.starts_at)
    busy = {
        r["volunteer_id"]
        for r in _db.db.query(
            "SELECT m.volunteer_id FROM shift h JOIN match m ON m.id = h.match_id "
            "WHERE h.status = 'scheduled' AND h.starts_at > ? AND h.starts_at < ?",
            [
                (start - URGENT_BUSY).strftime(_LOCAL_FORMAT),
                (start + URGENT_BUSY).strftime(_LOCAL_FORMAT),
            ],
        )
    }
    blocked_by_org = {
        b.blocked_user_id
        for b in _db.blocks(
            where="blocked_org_id = ? AND blocked_user_id IS NOT NULL",
            where_args=[call.org_id],
        )
    }
    active = {c.slug for c in _db.causes() if c.active}
    slugs = [s for s in json.loads(o.causes or "[]") if s in active]
    rates = _org_reply_rates()
    confirming = _confirming_orgs()
    ranked = []
    for me in _db.profiles(
        where="role = 'volunteer' AND urgent_on = 1 AND deleted_at IS NULL "
        "AND suspended_at IS NULL AND onboarded_at IS NOT NULL"
    ):
        u = _prefs.urgent(me)
        if (
            me.id in notified
            or me.id in busy
            or me.id in blocked_by_org
            or this_week.get(me.id, 0) >= min(u["per_week"], URGENT_WEEK_MAX)
            or _prefs.quiet_now(u, local.hour)
            or _week(call.starts_at) in _full_weeks(me)
            or (
                u["window"] == "today"
                and call.starts_at[:10] != local.date().isoformat()
            )
        ):
            continue
        channel = (
            "sms"
            if me.phone and not me.sms_opted_out
            else "email"
            if me.email
            else None
        )
        if channel is None:
            continue
        p = _deck_prefs(me)
        if o.id not in {d.id for d, *_ in _deck_openings(me.id, True, p, calls)}:
            continue
        near = None
        if not o.remote:
            near = _match.best_place(p["places"], o.lat, o.lon)  # their full reach
            if near is None:
                continue
        if (
            _match.availability_overlap(
                p["availability"], call.starts_at, "", p["free_dates"]
            )
            < 1
        ):
            continue
        rated = {s: v for s, v in p["causes"].items() if s in active}
        fit = _fit(
            p,
            rated,
            json.loads(me.skills or "[]"),
            _prefs.languages(me),
            o,
            slugs,
            near,
            rates,
            confirming,
        )
        ranked.append((-round(fit, 6), me.id, channel))
    ranked.sort()
    return [(uid, channel) for _, uid, channel in ranked[:limit]]


def urgent_notice(call_id, at):
    """What an urgent call's text or email sent at `at` (UTC) says: org, title, when, how
    many more, the opening's id (for /deck?card=); None once the call is not open."""
    call = _db.urgent_calls[call_id]
    if _open_calls().get(call.opening_id) != call:
        return None
    o = _db.openings[call.opening_id]
    return {
        "org": _db.orgs[call.org_id].name,
        "opening": o.title,
        "opening_id": o.id,
        "when": urgent_when(call.starts_at, at.astimezone(LA).date()),
        "left": call.needed - _urgent_found(call.id),
    }


def auto_accept_notice(match_id):
    """What the org's "accepted automatically" email says, or None when the match isn't an
    auto-accepted like on an urgent call (or the org must not hear of it)."""
    m = _db.matches[match_id]
    if m.status != "accepted" or not m.urgent_call_id or m.org_blind:
        return None
    call = _db.urgent_calls[m.urgent_call_id]
    org = _db.orgs[call.org_id]
    if org.deactivated_at:
        return None
    return {
        "org_id": org.id,
        "opening": _db.openings[call.opening_id].title,
        "when": urgent_when(call.starts_at),
        "found": _urgent_found(call.id),
        "needed": call.needed,
    }


def _urgent_off_mac(uid):
    msg = f"urgent-off:{uid}".encode()
    return hmac.new(phone_hash_key().encode(), msg, hashlib.sha256).hexdigest()[:32]


def urgent_off_key(uid):
    """The signed key of an urgent email's unsubscribe link: "<uid>-<HMAC>"."""
    return f"{uid}-{_urgent_off_mac(uid)}"


def urgent_off_volunteer(key):
    """The volunteer an unsubscribe key was made for, or None for a forged or garbled
    one (or a deleted account)."""
    uid, _, mac = key.partition("-")
    if not (
        uid.isascii() and uid.isdigit() and len(uid) < 19
    ) or not hmac.compare_digest(mac.encode(), _urgent_off_mac(int(uid)).encode()):
        return None
    p = get_profile(int(uid))
    return p.id if p and p.role == "volunteer" else None


def urgent_off(key):
    """Turn last-minute calls off for the key's volunteer; False for a bad key."""
    uid = urgent_off_volunteer(key)
    if uid is None:
        return False
    _db.profiles.update(id=uid, urgent_on=False)
    return True


def urgent_email(uid):
    p = get_profile(uid)
    return p.email if p and p.role == "volunteer" else None


def notification_claim(kind, key, recipient_id, at, cutoff=None, max_attempts=5):
    """Reserve one send for `key` at time `at`: the notification id, or None.

    A key sends once. A `failed` send is reserved again until it has had `max_attempts`.
    With `cutoff` the key is a stream: a send made before `cutoff` is reserved again too.
    One conditional UPDATE decides, so two callers never both win.
    """
    with _db.db.conn:
        if not _db.notifications(where="key = ?", where_args=[key]):
            return _db.notifications.insert(
                _db.Notification(
                    kind=kind,
                    key=key,
                    recipient_id=recipient_id,
                    sent_at=at,
                    status="pending",
                    attempts=1,
                )
            ).id
        # A deferred send (the SMS ceiling) is reserved again and keeps its attempts.
        _db.db.execute(
            "UPDATE notification SET status = 'pending', sent_at = ?, "
            "attempts = CASE WHEN status = 'failed' THEN coalesce(attempts, 0) + 1 "
            "WHEN status = 'deferred' THEN coalesce(attempts, 1) ELSE 1 END "
            "WHERE key = ? AND ((status IN ('failed', 'deferred') AND coalesce(attempts, 0) < ?) "
            "OR (? IS NOT NULL AND status IN ('sent', 'unknown') AND sent_at < ?))",
            [at, key, max_attempts, cutoff, cutoff],
        )
        if _db.db.conn.changes() != 1:
            return None
        return _db.notifications(where="key = ?", where_args=[key])[0].id


def notification_done(notification_id, status):
    _db.notifications.update(id=notification_id, status=status)


def failed_accepts(max_attempts):
    """Match ids whose "you're matched" text failed, or was deferred, and has tries left."""
    return [
        int(r[0].split(":")[1])
        for r in _db.db.execute(
            "SELECT key FROM notification WHERE kind = 'accepted' "
            "AND status IN ('failed', 'deferred') AND coalesce(attempts, 0) < ?",
            [max_attempts],
        )
    ]


def deferred_messages():
    """Match ids whose new-message text the SMS ceiling deferred."""
    return [
        int(r[0].split(":")[1])
        for r in _db.db.execute(
            "SELECT key FROM notification WHERE kind = 'message' AND status = 'deferred'"
        )
    ]


def sms_number(uid):
    """The volunteer's phone, or None when they opted out, were deleted, or are no volunteer."""
    p = get_profile(uid)
    if not p or p.role != "volunteer" or p.sms_opted_out or not p.phone:
        return None
    return p.phone


def sms_opted_out(uid):
    """The number replied STOP: remember it so nothing texts it again."""
    _db.profiles.update(id=uid, sms_opted_out=1)


def match_notice(match_id):
    """{"volunteer_id", "org_id", "org", "opening", "status"} of a match, or None."""
    try:
        m = _db.matches[match_id]
        o = _db.openings[m.opening_id]
        org = _db.orgs[o.org_id]
    except NotFoundError:
        return None
    return {
        "volunteer_id": m.volunteer_id,
        "org_id": org.id,
        "org": org.name,
        "opening": o.title,
        "status": m.status,
        # a blocked volunteer's like never reaches the org; a deactivated org sends nothing
        "silent": bool(m.org_blind or org.deactivated_at),
    }


def org_pending_count(org_id):
    return _db.db.execute(
        "SELECT count(*) FROM match JOIN opening ON opening.id = match.opening_id "
        "WHERE opening.org_id = ? AND match.status = 'pending' "
        "AND coalesce(match.org_blind, 0) = 0",
        [org_id],
    ).fetchone()[0]


def org_coordinator_emails(org_id):
    return [
        p.email
        for m in _db.org_members(where="org_id = ?", where_args=[org_id])
        if (p := get_profile(m.user_id)) and p.email
    ]


def due_reminders(first, last):
    """[(shift id, volunteer id)] of scheduled shifts of accepted matches starting between
    `first` and `last` (LA wall clock, "YYYY-MM-DDTHH:MM"), inclusive."""
    return [
        (r[0], r[1])
        for r in _db.db.execute(
            "SELECT shift.id, match.volunteer_id FROM shift "
            "JOIN match ON match.id = shift.match_id "
            "WHERE shift.status = 'scheduled' AND match.status = 'accepted' "
            "AND shift.starts_at BETWEEN ? AND ? ORDER BY shift.starts_at, shift.id",
            [first, last],
        )
    ]


def shift_notice(shift_id):
    """match_notice's keys and the shift's "match_id" and "starts_at", or None."""
    try:
        sh = _db.shifts[shift_id]
    except NotFoundError:
        return None
    info = match_notice(sh.match_id)
    return info and {**info, "match_id": sh.match_id, "starts_at": sh.starts_at}


# --- trust and safety: reports, blocks, suspension, org verification, deletion ---------------

REASONS = [
    ("harassment", "Harassment"),
    ("no_show_pattern", "A pattern of no-shows"),
    ("unsafe", "I felt unsafe"),
    ("inappropriate_profile", "Inappropriate photo or profile"),
    ("spam", "Spam"),
    ("other", "Something else"),
]
MAX_DETAILS = 1000


def _org_blocked_volunteer(volunteer_id, org_id):
    """True when a coordinator of this org blocked the volunteer."""
    return bool(
        _db.blocks.count_where(
            "blocked_user_id = ? AND blocked_org_id = ?", [volunteer_id, org_id]
        )
    )


def _volunteer_blocked_org(volunteer_id, org_id):
    """True when the volunteer blocked the org."""
    return bool(
        _db.blocks.count_where(
            "blocker_id = ? AND blocked_user_id IS NULL AND blocked_org_id = ?",
            [volunteer_id, org_id],
        )
    )


def _blocked_either(volunteer_id, org_id):
    """True when a block stands either way between the volunteer and the org."""
    return _org_blocked_volunteer(volunteer_id, org_id) or _volunteer_blocked_org(
        volunteer_id, org_id
    )


def _match_and_opening(match_id):
    if not _is_id(match_id):
        raise PermissionError("no such match")
    try:
        m = _db.matches[match_id]
        return m, _db.openings[m.opening_id]
    except NotFoundError:
        raise PermissionError("no such match") from None


def _end_match(m, by, pending_to):
    """Close one pending or accepted match: pending becomes `pending_to` (declined or
    withdrawn), accepted becomes withdrawn, and its future shifts are cancelled."""
    if m.status == "pending" and pending_to == "declined":
        _db.matches.update(id=m.id, status="declined", decided_at=_now(), decided_by=by)
    elif m.status in ("pending", "accepted"):
        _db.matches.update(id=m.id, status="withdrawn", withdrawn_at=_now())
    else:
        return
    _db.db.execute(
        "UPDATE shift SET status = 'cancelled' "
        "WHERE match_id = ? AND status = 'scheduled' AND starts_at > ?",
        [m.id, _local_now()],
    )
    _sync_fill(m.opening_id)


def _checked_report_text(reason, details):
    if reason not in {r for r, _ in REASONS}:
        raise ValueError("choose a reason")
    if not isinstance(details, str) or len(details.strip()) > MAX_DETAILS:
        raise ValueError(f"details can be up to {MAX_DETAILS} characters")
    if BIDI.search(details):  # reordering characters can make a report read wrong
        raise ValueError("details hold a character we can't show")
    return details.strip()


REPORTS_PER_DAY = 10  # per reporter


def _add_report(reporter_id, match_id, reason, details, *, user_id=None, org_id=None):
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    if (
        _db.reports.count_where(
            "reporter_id = ? AND created_at > ?", [reporter_id, since]
        )
        >= REPORTS_PER_DAY
    ):
        raise ValueError(
            f"you can send {REPORTS_PER_DAY} reports a day; try again tomorrow"
        )
    if _db.reports.count_where(
        "reporter_id = ? AND match_id = ? AND status = 'open'", [reporter_id, match_id]
    ):
        return  # already open: a repeat adds nothing
    _db.reports.insert(
        _db.Report(
            reporter_id=reporter_id,
            target_user_id=user_id,
            target_org_id=org_id,
            match_id=match_id,
            reason=reason,
            details=details,
            status="open",
            created_at=_now(),
        )
    )


def org_match_volunteer(coordinator_uid, match_id):
    """The name the org sees of the volunteer on a match of the coordinator's org ("Sam
    L.", "Sam Lee" once accepted); PermissionError otherwise. A match its inbox doesn't
    list as theirs to see (org_inbox's rule: a like dropped unseen, withdrawn before a
    decision, a suspended or deleted volunteer) or one under a block either way shows
    BLOCKED_NAME: ids are sequential, and a report or block page must not name anyone
    the inbox wouldn't."""
    m, o = _match_and_opening(match_id)
    _member_of(coordinator_uid, o.org_id)
    p = _db.profiles[m.volunteer_id]
    listed = (
        not p.deleted_at
        and not p.suspended_at
        and not m.org_blind
        and (
            m.status in ("pending", "accepted", "declined")
            or (m.status == "withdrawn" and m.decided_at)
        )
    )
    if not listed or _match_blocked(m):
        return BLOCKED_NAME
    return volunteer_name(p.display_name, p.last_name, m.status == "accepted")


def report_org(uid, match_id, reason, details=""):
    """A volunteer reports the org of their own match. PermissionError for any other id,
    ValueError for a reason that is not one of REASONS or details over 1000 characters."""
    m, o = _match_and_opening(match_id)
    if m.volunteer_id != uid:
        raise PermissionError("not your match")
    details = _checked_report_text(reason, details)
    _add_report(uid, match_id, reason, details, org_id=o.org_id)


def report_volunteer(coordinator_uid, match_id, reason, details=""):
    """A coordinator reports the volunteer of a match of their org. Same errors."""
    m, o = _match_and_opening(match_id)
    _member_of(coordinator_uid, o.org_id)
    details = _checked_report_text(reason, details)
    _add_report(coordinator_uid, match_id, reason, details, user_id=m.volunteer_id)


def _add_block(**cols):
    try:
        _db.blocks.insert(_db.Block(created_at=_now(), **cols))
    except apsw.ConstraintError:
        pass  # already blocked


def _end_matches_at_org(volunteer_id, org_id, by, pending_to):
    """_end_match on each of the volunteer's pending and accepted matches at this org."""
    for m in _db.matches(
        where="volunteer_id = ? AND status IN ('pending', 'accepted') "
        "AND opening_id IN (SELECT id FROM opening WHERE org_id = ?)",
        where_args=[volunteer_id, org_id],
    ):
        _end_match(m, by, pending_to)


def block_org(uid, match_id):
    """A volunteer blocks the org of their own match: its openings leave their deck, their
    pending and accepted matches with it are withdrawn and their future shifts cancelled,
    and chat is read-only."""
    with _db.db.conn:
        m, o = _match_and_opening(match_id)
        if m.volunteer_id != uid:
            raise PermissionError("not your match")
        _add_block(blocker_id=uid, blocked_org_id=o.org_id)
        _end_matches_at_org(uid, o.org_id, uid, "withdrawn")


def block_volunteer(coordinator_uid, match_id):
    """A coordinator blocks a match's volunteer for their org: their pending likes there are
    declined, an accepted match is withdrawn, and their future likes are silently dropped."""
    with _db.db.conn:
        m, o = _match_and_opening(match_id)
        _member_of(coordinator_uid, o.org_id)
        _add_block(
            blocker_id=coordinator_uid,
            blocked_user_id=m.volunteer_id,
            blocked_org_id=o.org_id,
        )
        _end_matches_at_org(m.volunteer_id, o.org_id, coordinator_uid, "declined")


def _admin(uid):
    p = get_profile(uid)
    if not p or p.role != "admin":
        raise PermissionError("admins only")


def _volunteer(uid):
    """The volunteer's own profile; PermissionError for anyone else."""
    p = get_profile(uid)
    if p is None or p.role != "volunteer":
        raise PermissionError("not a volunteer")
    return p


def admin_blocks(admin_uid):
    """Every block, for the admin who may lift it: first names and org names only."""
    _admin(admin_uid)
    return [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "org": r["org"],
            "kind": "coordinator" if r["blocked_user_id"] else "volunteer",
            "volunteer": _first_name(r["vol"]),
        }
        for r in _db.db.query(
            "SELECT b.id, b.created_at, b.blocked_user_id, g.name AS org, "
            "(SELECT display_name FROM profile WHERE id = coalesce(b.blocked_user_id, b.blocker_id)) AS vol "
            "FROM block b JOIN org g ON g.id = b.blocked_org_id ORDER BY b.id DESC"
        )
    ]


def unblock(admin_uid, block_id):
    """Lift a block. Admin only; matches the block ended stay ended."""
    _admin(admin_uid)
    if not _is_id(block_id) or not _db.blocks.count_where("id = ?", [block_id]):
        raise LookupError("no such block")
    b = _db.blocks[block_id]
    _db.blocks.delete(block_id)
    if b.blocked_user_id:
        _refresh_retained(b.blocked_user_id)


def admin_reports(admin_uid):
    """Open reports, oldest first, with the least context a decision needs: the match, the
    opening, the reason, the details, and first names. No phones, emails or places."""
    _admin(admin_uid)
    out = []
    for r in _db.db.query(
        "SELECT r.id, r.match_id, r.reason, r.details, r.created_at, r.target_user_id, "
        "r.target_org_id, o.title, g.name AS org, "
        "(SELECT display_name FROM profile WHERE id = r.reporter_id) AS reporter, "
        "(SELECT display_name FROM profile WHERE id = m.volunteer_id) AS volunteer, "
        "(SELECT suspended_at FROM profile WHERE id = r.target_user_id) AS suspended, "
        "(SELECT photo_key FROM profile WHERE id = r.target_user_id) AS photo_key, "
        "g.deactivated_at "
        "FROM report r JOIN match m ON m.id = r.match_id "
        "JOIN opening o ON o.id = m.opening_id JOIN org g ON g.id = o.org_id "
        "WHERE r.status = 'open' ORDER BY r.id"
    ):
        by_volunteer = r["target_org_id"] is not None
        out.append(
            {
                "id": r["id"],
                "match_id": r["match_id"],
                "opening": r["title"],
                "org": r["org"],
                "reason": dict(REASONS).get(r["reason"], r["reason"]),
                "details": r["details"] or "",
                "created_at": r["created_at"],
                "reporter": _first_name(r["reporter"]),
                "by": "volunteer" if by_volunteer else "coordinator",
                "target": r["org"] if by_volunteer else _first_name(r["volunteer"]),
                "target_user_id": r["target_user_id"],
                "target_org_id": r["target_org_id"],
                "suspended": bool(r["suspended"]),
                "deactivated": bool(r["deactivated_at"]),
                "photo_key": r["photo_key"],  # the reported volunteer's, for review
            }
        )
    return out


def _open_report(admin_uid, report_id):
    _admin(admin_uid)
    if not _is_id(report_id):
        raise LookupError("no such report")
    found = _db.reports(where="id = ? AND status = 'open'", where_args=[report_id])
    if not found:
        raise LookupError("no such open report")
    return found[0]


def _resolve(admin_uid, where, args, status):
    _db.db.execute(
        f"UPDATE report SET status = ?, resolved_by = ?, resolved_at = ? "
        f"WHERE status = 'open' AND {where}",
        [status, admin_uid, _now(), *args],
    )


def dismiss_report(admin_uid, report_id):
    """Close one report with no action. LookupError unless it is open."""
    with _db.db.conn:
        r = _open_report(admin_uid, report_id)
        _resolve(admin_uid, "id = ?", [report_id], "dismissed")
        if r.target_user_id:
            _refresh_retained(r.target_user_id)


def suspend_target(admin_uid, report_id):
    """Suspend the reported user: they cannot sign in, every session ends, a volunteer's
    pending and accepted matches are withdrawn. Every open report on them is actioned."""
    with _db.db.conn:
        r = _open_report(admin_uid, report_id)
        uid = r.target_user_id
        if uid is None:
            raise ValueError("this report names an org, not a user")
        if _db.profiles[uid].role == "admin":
            raise PermissionError("an admin cannot be suspended here")
        _db.db.execute(
            "UPDATE profile SET suspended_at = ?, "
            "session_version = coalesce(session_version, 1) + 1 WHERE id = ?",
            [_now(), uid],
        )
        for m in _db.matches(
            where="volunteer_id = ? AND status IN ('pending', 'accepted')",
            where_args=[uid],
        ):
            _end_match(m, admin_uid, "withdrawn")
        _resolve(admin_uid, "target_user_id = ?", [uid], "actioned")
        _refresh_retained(uid, suspended=True)  # a deleted account cannot come back


def remove_reported_photo(admin_uid, report_id):
    """Delete the reported volunteer's photo (they may add another); this report is
    actioned. ValueError for a report on an org."""
    with _db.db.conn:
        r = _open_report(admin_uid, report_id)
        if r.target_user_id is None:
            raise ValueError("this report names an org, not a user")
        _drop_photo(_db.profiles[r.target_user_id].photo_key)
        _db.profiles.update(id=r.target_user_id, photo_key=None)
        _resolve(admin_uid, "id = ?", [report_id], "actioned")


def deactivate_target_org(admin_uid, report_id):
    """Deactivate the reported org: its openings close, every pending match is declined and
    every accepted one withdrawn (chat read-only, future shifts cancelled, no texts), it leaves
    every deck and cannot post, accept, schedule or message. Every open report on it is actioned."""
    with _db.db.conn:
        r = _open_report(admin_uid, report_id)
        org_id = r.target_org_id
        if org_id is None:
            raise ValueError("this report names a user, not an org")
        _db.db.execute(
            "UPDATE org SET deactivated_at = ? WHERE id = ?", [_now(), org_id]
        )
        _db.db.execute(
            "UPDATE shift SET status = 'cancelled', cancelled_at = ?, cancelled_by = ? "
            "WHERE status = 'scheduled' AND starts_at > ? AND match_id IN "
            "(SELECT m.id FROM match m JOIN opening o ON o.id = m.opening_id "
            "WHERE o.org_id = ?)",
            [_now(), admin_uid, _local_now(), org_id],
        )
        for m in _db.matches(
            where="status IN ('pending', 'accepted') AND opening_id IN "
            "(SELECT id FROM opening WHERE org_id = ?)",
            where_args=[org_id],
        ):
            _end_match(m, admin_uid, "declined")  # pending declined, accepted withdrawn
        _db.db.execute(
            "UPDATE opening SET status = 'closed' WHERE org_id = ?", [org_id]
        )
        _resolve(admin_uid, "target_org_id = ?", [org_id], "actioned")


def admin_orgs(admin_uid):
    """Every org with its EIN and verification state, for /admin/orgs. Admin only."""
    _admin(admin_uid)
    return [
        {
            "id": o.id,
            "name": o.name,
            "ein": o.ein or "",
            "verified": bool(o.verified_at),
            "deactivated": bool(o.deactivated_at),
        }
        for o in _db.orgs(order_by="id")
    ]


def _check_ein(ein):
    if not (isinstance(ein, str) and len(ein) == 9 and ein.isascii() and ein.isdigit()):
        raise ValueError("an EIN is nine digits")


def verify_org(org_id, ein):
    """Mark an org verified under this EIN (nine digits). Admin-only entry point, used by the
    admin page and scripts/verify_org.py. LookupError for an unknown org; ValueError for a
    malformed EIN or one another org already carries."""
    if not _is_id(org_id) or not org_exists(org_id):
        raise LookupError(f"no org {org_id}")
    _check_ein(ein)
    try:
        _db.db.execute(
            "UPDATE org SET ein = ?, verified_at = ? WHERE id = ?",
            [ein, _now(), org_id],
        )
    except apsw.ConstraintError:
        raise ValueError("another org already has that EIN") from None


def upsert_imported_org(ein, name, city, zip_code, ntee_code, cause, lat, lon):
    """scripts/import_orgs.py's one write: "created" or "updated", keyed by EIN.

    A new org is unverified and unclaimed, with intro "cold" and its one cause. An org that
    already exists gets only its ProPublica record refreshed (name, city, ZIP, NTEE code,
    place): its cause, intro, about and verification stay as an admin left them.
    ValueError for a malformed EIN or an unknown cause.
    """
    _check_ein(ein)
    if not (isinstance(name, str) and name.strip()):
        raise ValueError("a name")
    if not _db.causes(where="slug = ?", where_args=[cause]):
        raise ValueError(f"no cause {cause!r}")
    record = {
        "name": name.strip(),
        "city": city,
        "zip": zip_code,
        "ntee_code": ntee_code,
        "lat": lat,
        "lon": lon,
    }
    found = _db.orgs(where="ein = ?", where_args=[ein])
    if found:
        _db.orgs.update(id=found[0].id, **record)
        return "updated"
    _db.orgs.insert(
        _db.Org(
            ein=ein,
            causes=json.dumps([cause]),
            intro="cold",
            created_at=_now(),
            **record,
        )
    )
    return "created"


CLAIM_LIMITS = {"org_name": 200, "name": 80, "email": 254}  # name: as a coordinator's


def submit_claim(ein, org_name, name, email, phone=""):
    """A pending ClaimRequest from the public claim page. Nothing is checked against the orgs
    and nothing is reported back, so the page can't reveal which EINs exist or are claimed.
    A repeat of a pending request (same EIN and email) is dropped. ValueError for a
    malformed EIN or an over-long or empty field (the route has checked them already)."""
    _check_ein(ein)
    fields = {"org_name": org_name, "name": name, "email": email.strip().lower()}
    for key, limit in CLAIM_LIMITS.items():
        if not 0 < len(fields[key]) <= limit:
            raise ValueError(key)
    if hidden_chars(fields["org_name"]) or hidden_chars(fields["name"]):
        raise ValueError("a hidden or special character")
    if _db.claim_requests(
        where="status = 'pending' AND ein = ? AND email = ?",
        where_args=[ein, fields["email"]],
    ):
        return
    _db.claim_requests.insert(
        _db.ClaimRequest(
            ein=ein, phone=phone, status="pending", created_at=_now(), **fields
        )
    )


def _live_members(org_id):
    """[{"name", "role"}] of an org's members who can still sign in, oldest first."""
    return [
        {"name": r["display_name"] or "(no name yet)", "role": r["role"]}
        for r in _db.db.q(
            "SELECT p.display_name, m.role FROM org_member m JOIN profile p "
            "ON p.id = m.user_id WHERE m.org_id = ? AND p.deleted_at IS NULL "
            "AND p.suspended_at IS NULL ORDER BY m.id",
            [org_id],
        )
    ]


def _members_text(members):
    return ", ".join(f"{m['name']} ({m['role']})" for m in members)


def _claim_org(ein):
    found = _db.orgs(where="ein = ?", where_args=[ein])
    return found[0] if found else None


def admin_claims(admin_uid):
    """Pending claims, oldest first, each beside the org its EIN names (None when no org
    has that EIN) and the people who can already sign in to it."""
    _admin(admin_uid)
    out = []
    for c in _db.claim_requests(where="status = 'pending'", order_by="id"):
        org = _claim_org(c.ein)
        out.append(
            {
                "id": c.id,
                "ein": c.ein,
                "org_name": c.org_name,
                "name": c.name,
                "email": c.email,
                "phone": c.phone or "",
                "created_at": c.created_at,
                "org": {
                    "name": org.name,
                    "city": org.city or "",
                    "ntee": org.ntee_code or "",
                    "verified": bool(org.verified_at),
                    "deactivated": bool(org.deactivated_at),
                }
                if org
                else None,
                "members": _live_members(org.id) if org else [],
            }
        )
    return out


def _pending_claim(claim_id):
    found = (
        _db.claim_requests(where="id = ? AND status = 'pending'", where_args=[claim_id])
        if _is_id(claim_id)
        else []
    )
    if not found:
        raise LookupError(f"no pending claim {claim_id}")
    return found[0]


def approve_claim(admin_uid, claim_id, confirm_claimed=False):
    """Admin approval: the only way a claimant gets membership. Creates or links the
    coordinator, adds them to the org (owner when it has no live owner, else coordinator),
    verifies the org if it is not yet, and returns (profile id, email, org name) for the
    approval email.

    LookupError for a claim that is not pending; ValueError when no org has the EIN, the org
    is deactivated, the address is an admin's or a suspended coordinator's, or the org
    already has someone who can sign in (an owner or a coordinator) and confirm_claimed is
    false."""
    _admin(admin_uid)
    c = _pending_claim(claim_id)
    org = _claim_org(c.ein)
    if org is None:
        raise ValueError("no org has that EIN; import it or reject the request")
    if org.deactivated_at:
        raise ValueError("that org is deactivated")
    members = _live_members(org.id)
    if members and not confirm_claimed:
        raise ValueError(
            f"already claimed by {_members_text(members)}: confirm to add another person"
        )
    if _db.profiles(
        where="role = 'coordinator' AND lower(email) = ? AND suspended_at IS NOT NULL "
        "AND deleted_at IS NULL",
        where_args=[c.email],
    ):
        raise ValueError("that address belongs to a suspended coordinator")
    has_owner = any(m["role"] == "owner" for m in members)
    with _db.db.conn:
        existing = coordinator_by_email(c.email)
        uid = add_coordinator(
            c.email,
            org.id,
            "coordinator" if has_owner else "owner",
            # a name from before claims were checked, unfit to show: they add their own
            name=""
            if existing or len(c.name) > CLAIM_LIMITS["name"] or hidden_chars(c.name)
            else c.name,
            phone="" if existing else (c.phone or ""),
        )
        if not org.verified_at:
            _db.orgs.update(id=org.id, verified_at=_now())
        _db.claim_requests.update(
            id=c.id,
            status="approved",
            org_id=org.id,
            decided_at=_now(),
            decided_by=admin_uid,
        )
    return uid, c.email, org.name


def reject_claim(admin_uid, claim_id):
    """Record a rejection; the claimant's email, for the notice. LookupError for a claim
    that is not pending."""
    _admin(admin_uid)
    c = _pending_claim(claim_id)
    _db.claim_requests.update(
        id=c.id, status="rejected", decided_at=_now(), decided_by=admin_uid
    )
    return c.email


def claim_for_approval(admin_uid, claim_id):
    """(EIN, typed org name, {"name", "city"} of the org with that EIN or None) of a
    pending claim. With None, approving imports the org from ProPublica first
    (app/org_import.py, outside the data lock). LookupError when the claim is not pending."""
    _admin(admin_uid)
    c = _pending_claim(claim_id)
    org = _claim_org(c.ein)
    return (
        c.ein,
        c.org_name,
        {"name": org.name, "city": org.city or ""} if org else None,
    )


CLAIM_KEEP_DAYS = 90


def scrub_decided_claims(days=CLAIM_KEEP_DAYS):
    """Blank the name, email and phone of claims decided more than `days` ago. How many."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    return len(
        _db.db.q(
            "UPDATE claim_request SET name = NULL, email = NULL, phone = NULL "
            "WHERE status != 'pending' AND decided_at < ? "
            "AND (name IS NOT NULL OR email IS NOT NULL OR phone IS NOT NULL) RETURNING id",
            [cutoff],
        )
    )


def verify_org_as(admin_uid, org_id, ein):
    """verify_org for the admin page: PermissionError unless admin_uid is an admin."""
    _admin(admin_uid)
    verify_org(org_id, ein)


PLACEHOLDER = "[message removed]"


def _phone_hash(phone):
    return hmac.new(phone_hash_key().encode(), phone.encode(), "sha256").hexdigest()


def _blocks_against(uid):
    return [
        [b.blocker_id, b.blocked_org_id]
        for b in _db.blocks(where="blocked_user_id = ?", where_args=[uid])
    ]


def _reports_against(uid):
    return _db.reports.count_where(
        "target_user_id = ? AND status IN ('open', 'actioned')", [uid]
    )


def _retain_phone(uid, phone, suspended):
    """Keep a keyed hash of a deleted account's phone only when a block, a report that is
    open or actioned, or a suspension must outlive the account. Never the number."""
    blocks, reports = _blocks_against(uid), _reports_against(uid)
    if not (blocks or reports or suspended):
        return
    reason = ", ".join(
        x
        for x, on in (("block", blocks), ("report", reports), ("suspended", suspended))
        if on
    )
    _db.db.execute(
        "INSERT OR REPLACE INTO retained_phone_hash "
        "(hash, profile_id, reason, blocks_json, suspended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [_phone_hash(phone), uid, reason, json.dumps(blocks), int(suspended), _now()],
    )


def _refresh_retained(uid, suspended=False):
    """After a report is dismissed, a block lifted or a deleted account suspended: bring the
    kept hash of profile `uid` in line, or drop it when nothing needs it any more."""
    rows = _db.retained_phones(where="profile_id = ?", where_args=[uid])
    if not rows:
        return
    row = rows[0]
    blocks = _blocks_against(uid)
    sus = bool(row.suspended) or suspended
    if blocks or sus or _reports_against(uid):
        _db.db.execute(
            "UPDATE retained_phone_hash SET blocks_json = ?, suspended = ? WHERE hash = ?",
            [json.dumps(blocks), int(sus), row.hash],
        )
    else:
        _db.db.execute("DELETE FROM retained_phone_hash WHERE hash = ?", [row.hash])


def delete_account(uid):
    """A volunteer deletes their own account.

    Soft-deleted, with every personal column scrubbed (kept: the answers the metrics group
    by), and their photos deleted. Pending and accepted matches are withdrawn and future
    shifts cancelled, their
    message bodies are replaced, texts still queued for them are cancelled, and every
    session ends. They can sign up again later as a new profile.
    """
    with _db.db.conn:
        try:
            p = _db.profiles[uid] if _is_id(uid) else None
        except NotFoundError:
            p = None
        if p is None or p.role != "volunteer":
            raise PermissionError("not a volunteer")
        if p.deleted_at:
            return
        if p.phone:
            _retain_phone(uid, p.phone, bool(p.suspended_at))
        for m in _db.matches(
            where="volunteer_id = ? AND status IN ('pending', 'accepted')",
            where_args=[uid],
        ):
            _end_match(m, uid, "withdrawn")
        _db.db.execute(
            "UPDATE message SET body = ? WHERE sender_id = ?", [PLACEHOLDER, uid]
        )
        _db.db.execute(
            "UPDATE notification SET status = 'cancelled' WHERE recipient_id = ? "
            "AND kind != 'likes' AND status IN ('pending', 'failed')",
            [uid],
        )
        _db.db.execute("DELETE FROM login_token WHERE user_id = ?", [uid])
        _db.db.execute("DELETE FROM place WHERE user_id = ?", [uid])
        _db.db.execute(
            "DELETE FROM photo WHERE owner_kind = 'volunteer' AND owner_id = ?", [uid]
        )
        _db.db.execute(
            "UPDATE profile SET deleted_at = ?, phone = NULL, email = NULL, "
            "display_name = NULL, contact_phone = NULL, lat = NULL, lon = NULL, "
            "neighborhood = NULL, other_causes = NULL, language_other = NULL, "
            "free_dates = NULL, verified_hours = NULL, "
            "last_name = NULL, pronouns = NULL, bio = NULL, interests = NULL, "
            "photo_key = NULL, "
            "session_version = coalesce(session_version, 1) + 1 WHERE id = ?",
            [_now(), uid],
        )


def remove_coordinator(email):
    """Admin-only entry point (scripts/remove_coordinator.py): delete a coordinator's account.

    Soft-deleted and scrubbed like a volunteer's, taken out of every org, signed out at
    once. Their messages stay: they are the org's side of the record. LookupError when there
    is no such coordinator."""
    uid = coordinator_by_email(email)
    if not uid:
        raise LookupError(f"no coordinator {email}")
    with _db.db.conn:
        _db.db.execute(
            "UPDATE claim_request SET name = NULL, email = NULL, phone = NULL "
            "WHERE lower(email) = ?",
            [email.strip().lower()],
        )
        _db.db.execute("DELETE FROM org_member WHERE user_id = ?", [uid])
        _db.db.execute("DELETE FROM login_token WHERE user_id = ?", [uid])
        _db.db.execute(
            "UPDATE profile SET deleted_at = ?, email = NULL, display_name = NULL, "
            "contact_phone = NULL, session_version = coalesce(session_version, 1) + 1 "
            "WHERE id = ?",
            [_now(), uid],
        )
    return uid


# --- photos ------------------------------------------------------------------------------


def store_photo(owner_kind, owner_id, processed):
    """Insert a re-encoded picture (photos.Processed) under a new random key; the key.
    Callers point a row at it (scripts/demo.py too, for the orgs it seeds)."""
    key = _photos.new_key()
    _db.photos.insert(
        _db.Photo(
            key=key,
            owner_kind=owner_kind,
            owner_id=owner_id,
            full=processed.full,
            thumb=processed.thumb,
            sha256=hashlib.sha256(processed.full).hexdigest(),
            created_at=_now(),
        )
    )
    return key


def _drop_photo(key):
    if key:
        _db.db.execute("DELETE FROM photo WHERE key = ?", [key])


def set_volunteer_photo(uid, processed):
    """The volunteer's photo becomes this one, under a new key; the old one is deleted."""
    with _db.db.conn:
        p = _volunteer(uid)
        key = store_photo("volunteer", uid, processed)
        _db.profiles.update(id=uid, photo_key=key)
        _drop_photo(p.photo_key)
    return key


def remove_volunteer_photo(uid):
    with _db.db.conn:
        p = _volunteer(uid)
        _drop_photo(p.photo_key)
        _db.profiles.update(id=uid, photo_key=None)


def coordinator_org(coordinator_uid, org_id):
    """An org this coordinator belongs to; PermissionError for any other id."""
    if not _is_id(org_id):
        raise PermissionError("no such org")
    _member_of(coordinator_uid, org_id)
    return _db.orgs[org_id]


def set_org_picture(coordinator_uid, org_id, which, processed):
    """The org's logo or photo (which: logo | photo) becomes this one; the old one goes."""
    column = {"logo": "logo_key", "photo": "photo_key"}[which]
    with _db.db.conn:
        org = coordinator_org(coordinator_uid, org_id)
        key = store_photo(f"org_{which}", org_id, processed)
        _db.orgs.update(id=org_id, **{column: key})
        _drop_photo(getattr(org, column))
    return key


def set_org_details(coordinator_uid, org_id, about, website, confirms_hours):
    """About text, website (https, or None) and whether the org can confirm volunteer
    hours, of an org this coordinator belongs to."""
    with _db.db.conn:
        coordinator_org(coordinator_uid, org_id)
        _db.orgs.update(
            id=org_id, about=about, website=website, confirms_hours=confirms_hours
        )


def coordinator_opening(coordinator_uid, opening_id):
    """An opening of one of the coordinator's orgs; PermissionError for any other id."""
    if not _is_id(opening_id):
        raise PermissionError("no such opening")
    try:
        o = _db.openings[opening_id]
    except NotFoundError:
        raise PermissionError("no such opening") from None
    _member_of(coordinator_uid, o.org_id)
    return o


def set_opening_photo(coordinator_uid, opening_id, processed):
    with _db.db.conn:
        o = coordinator_opening(coordinator_uid, opening_id)
        key = store_photo("opening", opening_id, processed)
        _db.openings.update(id=opening_id, photo_key=key)
        _drop_photo(o.photo_key)
    return key


def set_opening_link(coordinator_uid, opening_id, url):
    """The event's page as the org advertises it (https, checked by the caller), or None."""
    with _db.db.conn:
        coordinator_opening(coordinator_uid, opening_id)
        _db.openings.update(id=opening_id, event_url=url)


def dismiss_photo_nudge(uid):
    _db.profiles.update(id=uid, photo_nudge_dismissed_at=_now())


def _sees_volunteer(viewer, volunteer_id):
    """A volunteer's photo is theirs, an admin's, and a coordinator's whose org can see one
    of their matches in its inbox (pending, accepted, declined, or withdrawn after the org
    decided), not org_blind, and with no block between them either way."""
    if viewer.id == volunteer_id or viewer.role == "admin":
        return True
    if viewer.role != "coordinator":
        return False
    return bool(
        _db.db.execute(
            "SELECT 1 FROM match m JOIN opening o ON o.id = m.opening_id "
            "JOIN org_member om ON om.org_id = o.org_id AND om.user_id = ? "
            "WHERE m.volunteer_id = ? AND coalesce(m.org_blind, 0) = 0 "
            "AND (m.status IN ('pending', 'accepted', 'declined') "
            "OR (m.status = 'withdrawn' AND m.decided_at IS NOT NULL)) "
            "AND NOT EXISTS (SELECT 1 FROM block b WHERE b.blocked_user_id = ? "
            "AND b.blocked_org_id = o.org_id) "
            "AND NOT EXISTS (SELECT 1 FROM block b WHERE b.blocker_id = ? "
            "AND b.blocked_user_id IS NULL AND b.blocked_org_id = o.org_id) LIMIT 1",
            [viewer.id, volunteer_id, volunteer_id, volunteer_id],
        ).fetchone()
    )


def photo_for(viewer_uid, key, thumb=False):
    """(JPEG bytes, ETag, a volunteer's?) of the photo `key` when this signed-in viewer
    may see it, else None: a photo they may not see and one that doesn't exist look the
    same."""
    viewer = get_profile(viewer_uid) if _is_id(viewer_uid) else None
    if viewer is None or not _photos.is_key(key):
        return None
    row = _db.db.execute(
        f"SELECT owner_kind, owner_id, {'thumb' if thumb else 'full'}, sha256 "
        "FROM photo WHERE key = ?",
        [key],
    ).fetchone()
    if row is None:
        return None
    kind, owner_id, jpeg, sha = row
    # An org's logo and photo and an opening's photo are public to anyone signed in.
    if kind == "volunteer":
        owner = get_profile(owner_id)  # None once deleted or suspended
        if (owner is None and viewer.role != "admin") or not _sees_volunteer(
            viewer, owner_id
        ):
            return None
    return jpeg, f'"{sha}{"-t" if thumb else ""}"', kind == "volunteer"


RATE_KEEP_SECONDS = 24 * 3600  # the longest window in app/security.py


def rate_take(keys, now, record=True):
    """The first key that already holds `limit` events within its window, counting nothing;
    else None, after counting one event under every key (unless `record` is False).
    keys: [(key, limit, window_seconds)].
    """
    _db.db.execute("DELETE FROM rate_event WHERE at < ?", [now - RATE_KEEP_SECONDS])
    for key, limit, window in keys:
        (row,) = _db.db.q(
            "SELECT count(*) AS n FROM rate_event WHERE key = ? AND at > ?",
            [key, now - window],
        )
        if row["n"] >= limit:
            return key
    if record:
        with _db.db.conn:
            for key, _, _ in keys:
                _db.rate_events.insert(key=key, at=now)
    return None


# --- the demo: development, test and a preview only (config.demo_matches) -------------

# Stand-in coordinators the demo seed gives every org but the demo coordinator's: nobody
# signs in as them, and their orgs say yes to a like by themselves (demo_accept_due).
DEMO_TEAM_DOMAIN = "team.demo.causematch.test"
DEMO_ACCEPT_AFTER = timedelta(seconds=4)  # how long a demo team takes to say yes
DEMO_ACCEPT_WITHIN = timedelta(hours=1)  # a like older than this is left pending
DEMO_WELCOME = "Hi {}! Thanks for signing up. We'll see you there, and you can ask us anything here."
DEMO_COPIES = 200  # demo volunteers kept at once; past it the oldest is reused
# Their numbers: exchange 000 is no real number, and the sign-in form refuses it
DEMO_PHONE_PREFIX = "+1310000"


# The demo seed's own volunteers (demo_seed: ME_PHONE and 0101 to 0103)
DEMO_SEEDED_PHONES = frozenset(f"+1310555010{n}" for n in range(4))


def _hidden_in_demo(uid):
    """While config.demo_matches is on, anyone may sign in as a demo org's coordinator
    (the one-tap button, the preview's on-page links): a like from someone who is not a
    demo person (a tester with their own number) is made blind, like a blocked one, so
    no org ever sees their name, photo or count. Off: False, as production."""
    if not demo_matches():
        return False
    phone = (get_profile(uid) or _db.profiles[uid]).phone or ""
    return not (phone.startswith(DEMO_PHONE_PREFIX) or phone in DEMO_SEEDED_PHONES)


def is_demo_volunteer(uid):
    """True for a demo copy (new_demo_volunteer): only its likes are auto-accepted."""
    p = get_profile(uid)
    return bool(p and (p.phone or "").startswith(DEMO_PHONE_PREFIX))


def demo_team(org_id):
    """The org's stand-in demo coordinator (an address at DEMO_TEAM_DOMAIN), else None."""
    rows = _db.db.q(
        "SELECT p.id FROM org_member m JOIN profile p ON p.id = m.user_id "
        "WHERE m.org_id = ? AND p.email LIKE ? AND p.deleted_at IS NULL "
        "ORDER BY p.id LIMIT 1",
        [org_id, f"%@{DEMO_TEAM_DOMAIN}"],
    )
    return rows[0]["id"] if rows else None


def _drop_demo_volunteer(uid):
    """One demo volunteer and every row of theirs, gone; the openings they held reopen."""
    held = {
        m.opening_id for m in _db.matches(where="volunteer_id = ?", where_args=[uid])
    }
    theirs = "(SELECT id FROM match WHERE volunteer_id = ?)"
    for sql, n in (
        (f"DELETE FROM message WHERE match_id IN {theirs}", 1),
        (f"DELETE FROM shift WHERE match_id IN {theirs}", 1),
        ("DELETE FROM match WHERE volunteer_id = ?", 1),
        ("DELETE FROM swipe WHERE volunteer_id = ?", 1),
        ("DELETE FROM place WHERE user_id = ?", 1),
        ("DELETE FROM photo WHERE owner_kind = 'volunteer' AND owner_id = ?", 1),
        ("DELETE FROM deck_empty WHERE volunteer_id = ?", 1),
        ("DELETE FROM notification WHERE recipient_id = ? AND kind != 'likes'", 1),
        ("DELETE FROM report WHERE reporter_id = ? OR target_user_id = ?", 2),
        ("DELETE FROM block WHERE blocker_id = ? OR blocked_user_id = ?", 2),
        ("DELETE FROM profile WHERE id = ?", 1),
    ):
        _db.db.execute(sql, [uid] * n)
    for opening_id in held:
        _sync_fill(opening_id)


def new_demo_volunteer():
    """A new, empty volunteer profile for one visitor of a demo (demo_seed fills it in),
    under a number nobody can sign in with. With DEMO_COPIES of them already, the oldest is
    dropped and its number reused, so a busy preview stays bounded. A copy is signed in
    once, when it is made, so the oldest is also the one least recently signed in. Demo
    only."""
    if not demo_matches():
        raise PermissionError("demo and preview only")
    with _db.db.conn:
        taken = _db.profiles(
            where="phone LIKE ? AND deleted_at IS NULL",
            where_args=[DEMO_PHONE_PREFIX + "%"],
            order_by="id",
        )
        used = {p.phone for p in taken}
        phone = next(
            (
                f"{DEMO_PHONE_PREFIX}{n:04d}"
                for n in range(DEMO_COPIES)
                if f"{DEMO_PHONE_PREFIX}{n:04d}" not in used
            ),
            None,
        )
        if phone is None:
            phone = taken[0].phone
            _drop_demo_volunteer(taken[0].id)
        return _db.profiles.insert(
            _db.Profile(
                role="volunteer", phone=phone, session_version=1, created_at=_now()
            )
        ).id


def demo_accept_due(uid):
    """Demo only, and a demo copy's likes only (an ordinary volunteer of a preview waits
    like anyone), else []: the volunteer's likes made DEMO_ACCEPT_AFTER to
    DEMO_ACCEPT_WITHIN ago, on openings of an org a demo team runs, are accepted by that
    team with a welcome message, so a like turns into a match while the visitor watches.
    [{"id", "org"}] of the matches accepted now. Production never runs it: with
    CAUSEMATCH_DEMO_MATCHES set it refuses to start."""
    if not demo_matches() or not is_demo_volunteer(uid):
        return []
    now = datetime.now(UTC)
    due = _db.matches(
        where="volunteer_id = ? AND status = 'pending' AND NOT coalesce(org_blind, 0) "
        "AND created_at BETWEEN ? AND ?",
        where_args=[
            uid,
            (now - DEMO_ACCEPT_WITHIN).isoformat(timespec="seconds"),
            (now - DEMO_ACCEPT_AFTER).isoformat(timespec="seconds"),
        ],
        order_by="id",
    )
    first = _first_name(_db.profiles[uid].display_name)
    accepted = []
    for m in due:
        org_id = _db.openings[m.opening_id].org_id
        team = demo_team(org_id)
        if team is None:
            continue
        try:
            decide(team, m.id, True)
        except (PermissionError, ValueError):
            continue  # blocked, full or decided meanwhile: it stays as it is
        send_message(team, m.id, DEMO_WELCOME.format(first))
        accepted.append({"id": m.id, "org": _db.orgs[org_id].name})
    return accepted


DEMO_REPLY = "Thanks, {}! We're looking forward to it. See you there."


def demo_reply(uid, match_id):
    """Demo only: a demo copy's first message in a chat with a stand-in org gets one
    friendly reply from that org's stand-in coordinator (never Pat's org, a real
    volunteer, or production). The message sent, else None; once per match: it answers
    only while the team has said nothing since the volunteer's first message."""
    if not demo_matches() or not is_demo_volunteer(uid):
        return None
    m = _db.matches[match_id]
    team = demo_team(_db.openings[m.opening_id].org_id)
    if team is None or m.volunteer_id != uid:
        return None
    first = _db.messages(
        where="match_id = ? AND sender_id = ?",
        where_args=[match_id, uid],
        order_by="id",
        limit=1,
    )
    if not first or _db.messages.count_where(
        "match_id = ? AND sender_id = ? AND id > ?", [match_id, team, first[0].id]
    ):
        return None
    return send_message(
        team, match_id, DEMO_REPLY.format(_first_name(_db.profiles[uid].display_name))
    )


# One apsw connection is shared by every request and websocket thread; apsw
# raises ThreadingViolationError when two threads use it at once. SQLite allows a single
# writer anyway, so every public function here runs under one re-entrant lock.
_lock = threading.RLock()


def _serialized(fn):
    @functools.wraps(fn)
    def run(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)

    return run


for _name, _fn in list(globals().items()):
    if (
        inspect.isfunction(_fn)
        and _fn.__module__ == __name__
        and not _name.startswith("_")
    ):
        globals()[_name] = _serialized(_fn)
