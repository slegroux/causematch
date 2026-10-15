import functools
import hashlib
import hmac
import inspect
import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

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
)
from app.config import phone_hash_key
from app.forms import BIDI, hidden_chars
from app.metrics_rows import fetch_rows


def start(demo):
    """Run once at startup: the cause rows, and the invented demo rows when asked."""
    _db.ensure_causes()
    if demo:
        _db.seed_demo()


def _is_id(n):
    """A row id some table could hold: an int in 1 .. 2**63-1 (SQLite's integer range)."""
    return isinstance(n, int) and not isinstance(n, bool) and 0 < n < 2**63


LA = ZoneInfo("America/Los_Angeles")


def _local_now():
    """Wall clock in Los Angeles, the zone openings' starts_at are stored in."""
    return datetime.now(LA).strftime("%Y-%m-%dT%H:%M")


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


def _places(me):
    """The volunteer's places, as match.best_place takes them; scale_km scales the distance
    score. A profile with no saved place has one, Home: its sign-up ZIP by car, reaching
    exactly its radius, so its deck stays as it was."""
    rows = _db.places(where="user_id = ?", where_args=[me.id], order_by="id")
    if rows:
        return [
            {
                **{k: getattr(p, k) for k in ("id", "label", "lat", "lon", "area")},
                "mode": p.mode,
                "max_minutes": p.max_minutes,
                "reach_km": (reach := _match.reach_km(p.mode, p.max_minutes)),
                "scale_km": reach or ANY_DISTANCE_KM,
            }
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


TODAY_HOURS = 48  # "I'm free today": shifts starting this soon


def _deck_prefs(me):
    """Everything about the volunteer that shapes the deck, read once. relaxations()
    changes one value of a copy and counts what the deck would hold."""
    return {
        "causes": _prefs.cause_prefs(me),
        "places": _places(me),
        "horizon_days": _prefs.horizon_days(me),
        "dealbreakers": _prefs.dealbreakers(me),
        "availability": json.loads(me.availability or "[]"),
        "free_dates": _prefs.free_dates(me, local_today()),
        "commitment": _prefs.commitment(me),
        "work": _prefs.work(me),
        "orientation_ok": _prefs.orientation_ok(me),
    }


def _deck_openings(uid, today=False, p=None):
    """[(opening, active cause labels, km, (place, km, minutes) or None)] this volunteer
    could still swipe on.

    swipe() checks visibility with this same function, so whatever hides an opening
    from the deck (closed, past, inactive cause, a cause that is "not for me", past the
    volunteer's horizon, a dealbreaker: out of reach of every place, ...) also stops a like
    sent by id. today: only shifts starting in the next TODAY_HOURS, whatever the
    volunteer's times. The place is the one quickest to reach the opening from; it and km
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
    until = (start + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M") if days else None
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
    out = []
    for o in _db.openings(
        where="status = ? AND starts_at >= ?",
        where_args=["open", _local_now()],
        order_by="starts_at",
    ):
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


def _when_ok(p, o):
    """The "when" dealbreaker: free then (or a flexible opening), and the commitment fits."""
    free = _match.availability_overlap(
        p["availability"], o.starts_at, o.recurrence, p["free_dates"]
    )
    return free > 0 and _match.commitment_fits(p["commitment"], o.recurrence)


def record_empty_deck(uid, today=False):
    """Log an empty deck, once per volunteer and mode per hour: refreshing doesn't
    inflate it, and the "I'm free today" split stays whole."""
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
    elif key.startswith("widen:"):
        i, to = map(int, key.split(":")[1:])
        reach = _match.reach_km(p["places"][i]["mode"], to)
        q["places"] = [
            {
                **pl,
                "max_minutes": to,
                "reach_km": reach,
                "scale_km": reach or ANY_DISTANCE_KM,
            }
            if j == i
            else pl
            for j, pl in enumerate(p["places"])
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
            i, to = map(int, key.split(":")[1:])
            _db.places.update(id=_saved_places(uid)[i].id, max_minutes=to)
        elif key == "horizon":
            _db.profiles.update(id=uid, horizon_days=0)
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
        reach = _match.reach_km(p["places"][0]["mode"], minutes)
        q["places"] = [
            {
                **p["places"][0],
                "max_minutes": minutes,
                "reach_km": reach,
                "scale_km": reach or ANY_DISTANCE_KM,
            },
            *p["places"][1:],
        ]
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


def _distance_label(km):
    return None if km is None else "< 1 km" if km < 1 else f"{round(km)} km"


def _work_of(o):
    return {
        "setting": o.setting,
        "effort": o.effort,
        "group_size": o.group_size,
        "first_timer_ok": bool(o.first_timer_ok),
    }


SURPRISE_EVERY = 8  # "Surprise me": about one card in this many is from outside love


def deck(uid, limit=20, today=False, view=None):
    """deck_and_count()'s cards."""
    return deck_and_count(uid, limit, today, view)[0]


def deck_and_count(uid, limit=20, today=False, view=None):
    """Cards for open openings with an active cause, best fit first. today ("I'm free
    today"): only shifts starting in the next TODAY_HOURS, soonest first, then nearest.

    With "Surprise me", every SURPRISE_EVERY-th card the volunteer meets (counted by their
    swipes) is the best one with no loved cause. Each card carries 1 to 4 why-chips, the
    distance rounded to whole km and the travel estimate, never coordinates. Returns
    (cards, how many the deck holds in all). view: the filter sheet's (see _viewed).
    """
    me = _db.profiles[uid]
    p = _viewed(_deck_prefs(me), view)
    active = {c.slug for c in _db.causes() if c.active}
    rated = {s: v for s, v in p["causes"].items() if s in active}
    loved = {s for s, v in rated.items() if v == _prefs.LOVE}
    skills = json.loads(me.skills or "[]")
    spoken = _prefs.languages(me)
    org_names = {o.id: o.name for o in _db.orgs()}
    org_media = {o.id: (o.photo_key, o.logo_key) for o in _db.orgs()}
    rates = _org_reply_rates()
    ranked = []
    for o, cause_labels, km, near in _deck_openings(uid, today, p):
        slugs = [s for s in json.loads(o.causes or "[]") if s in active]
        when = _match.availability_overlap(
            p["availability"], o.starts_at, o.recurrence, p["free_dates"]
        )
        if not _match.commitment_fits(p["commitment"], o.recurrence):
            when /= 2
        fit = _match.score(
            causes=(rated, set(slugs)),
            skills=(skills, json.loads(o.skills or "[]")),
            distance=_match.distance_score(km, near and near[0]["scale_km"], o.remote),
            availability=when,
            reply=rates.get(o.org_id, 0.5),
            work=_match.work_fit(p["work"], _work_of(o)),
            language=_match.language_fit(spoken, json.loads(o.languages or "[]")),
        )
        if today:  # ignores fit: the soonest, then the nearest; remote after in-person
            key = (o.starts_at, km if km is not None else float("inf"), o.id)
        else:
            key = (-round(fit, 6), o.starts_at, o.id)
        ranked.append((*key, o, cause_labels, slugs, near))
    ranked.sort(key=lambda r: r[:3])
    pool = [r[3:] for r in ranked]
    seen = _db.swipes.count_where("volunteer_id = ?", [uid])
    surprise = bool(me.surprise) and bool(loved) and not today
    cards = []
    while pool and len(cards) < limit:
        pick, odd = 0, False
        if surprise and (seen + len(cards)) % SURPRISE_EVERY == SURPRISE_EVERY - 1:
            outside = [i for i, r in enumerate(pool) if not loved & set(r[2])]
            if outside:
                pick, odd = outside[0], True
        o, cause_labels, slugs, near = pool.pop(pick)
        cards.append(
            {
                "id": o.id,
                "title": o.title,
                "org": org_names.get(o.org_id, ""),
                "causes": cause_labels,
                # the same active causes as slugs, in the same order (art and chips)
                "slugs": slugs,
                "starts_at": o.starts_at,
                "neighborhood": o.neighborhood,
                "slots": o.slots,
                "remote": bool(o.remote),
                "distance": _distance_label(near and near[1]),
                "travel": near
                and f"{near[2]} min {TRAVEL_PHRASE[near[0]['mode']]} "
                f"from {near[0]['label']}",
                "surprise": odd,
                "why": _why(p, rated, spoken, o, cause_labels, slugs, near, odd),
                # the card's picture: the opening's photo, else the org's; its logo
                # as a badge
                "photo": o.photo_key or org_media.get(o.org_id, (None, None))[0],
                "logo": org_media.get(o.org_id, (None, None))[1],
                "event_url": o.event_url,
            }
        )
    return cards, len(ranked)


MAX_WHY = 4
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


def _why(p, rated, spoken, o, cause_labels, slugs, near, surprise):
    """1 to 4 reasons a card is shown, most telling first: [{"text", "slug"}], slug set on a
    cause chip (drawn in its color). Its causes always come first, so there is at least one.
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
    if surprise:
        more.append("Surprise pick")
    if near:
        more.append(
            f"{near[2]} min {TRAVEL_PHRASE[near[0]['mode']]} from {near[0]['label']}"
        )
    elif o.remote:
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
    return why + [{"text": t, "slug": None} for t in more][: MAX_WHY - len(why)]


def _accepted(opening_id):
    return _db.matches.count_where(
        "opening_id = ? AND status = 'accepted'", [opening_id]
    )


def _sync_fill(opening_id):
    """open <-> filled from accepted count vs slots; a closed opening stays closed."""
    _db.db.execute(
        "UPDATE opening SET status = CASE WHEN "
        "(SELECT count(*) FROM match WHERE opening_id = ? AND status = 'accepted') "
        ">= slots THEN 'filled' ELSE 'open' END "
        "WHERE id = ? AND status IN ('open', 'filled')",
        [opening_id, opening_id],
    )


def _match_for(uid, opening_id):
    found = _db.matches(
        where="volunteer_id = ? AND opening_id = ?", where_args=[uid, opening_id]
    )
    return found[0] if found else None


def _swipe(uid, opening_id, liked, today=False, view=None):
    """Record a swipe; a like makes a pending match. (match, created), match None for a pass.

    Repeating a swipe changes nothing. LookupError when the volunteer could not see
    the opening in their deck (closed, inactive cause, unknown id): the deck they swiped
    in (today, view), or their saved deck, since a card drawn under one may be swiped
    after the other changed.
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
        return _db.matches.insert(
            _db.Match(
                opening_id=opening_id,
                volunteer_id=uid,
                status="pending",
                created_at=_now(),
                # a blocked volunteer's like looks normal to them and never reaches the org
                org_blind=_org_blocked_volunteer(uid, _db.openings[opening_id].org_id),
            )
        ), True


def swipe(uid, opening_id, liked, today=False, view=None):
    """Record a swipe; a like makes a pending match. The match, or None for a pass.

    Repeating a swipe changes nothing. LookupError when the volunteer could not see
    the opening in their deck (closed, inactive cause, unknown id).
    """
    return _swipe(uid, opening_id, liked, today, view)[0]


def swipe_created(uid, opening_id, liked, today=False, view=None):
    """Like `swipe`, but the Match only when this call created it (not a pass or a repeat)."""
    match, created = _swipe(uid, opening_id, liked, today, view)
    return match if created and not match.org_blind else None


def decide(coordinator_uid, match_id, accept):
    """Accept or decline a pending match. PermissionError unless a member of the org.

    ValueError when the match is not pending or no slot is left to accept into.
    """
    if not _is_id(match_id):
        raise PermissionError("no such match")
    try:
        m = _db.matches[match_id]
        o = _db.openings[m.opening_id]
    except NotFoundError:
        raise PermissionError("no such match") from None
    if o.org_id not in coordinator_org_ids(coordinator_uid):
        raise PermissionError("not a member of this org")
    if _db.orgs[o.org_id].deactivated_at or _org_blocked_volunteer(
        m.volunteer_id, o.org_id
    ):
        raise PermissionError("no such match")
    status = "accepted" if accept else "declined"
    with _db.db.conn:
        # One statement, so two coordinators can't both take the last slot.
        _db.db.execute(
            "UPDATE match SET status = ?, decided_at = ?, decided_by = ? "
            "WHERE id = ? AND status = 'pending' AND (? = 0 OR "
            "(SELECT count(*) FROM match WHERE opening_id = ? AND status = 'accepted') "
            "< (SELECT slots FROM opening WHERE id = ?))",
            [status, _now(), coordinator_uid, match_id, int(bool(accept)),
             o.id, o.id],
        )  # fmt: skip
        if _db.db.conn.changes() != 1:
            now_status = _db.matches[match_id].status
            raise ValueError(
                "already decided" if now_status != "pending" else "no slots left"
            )
        if accept:
            _sync_fill(o.id)
            # An opening that already started gets no shift: the coordinator schedules one.
            if o.starts_at > _local_now():
                _db.shifts.insert(
                    _db.Shift(
                        match_id=match_id,
                        starts_at=o.starts_at,
                        status="scheduled",
                        kind="accept",
                        created_by=coordinator_uid,
                        created_at=_now(),
                    )
                )


def _own_match(uid, match_id):
    """The volunteer's own match row; PermissionError for any other id."""
    if not _is_id(match_id):
        raise PermissionError("no such match")
    try:
        m = _db.matches[match_id]
    except NotFoundError:
        raise PermissionError("no such match") from None
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
    """The volunteer's own pending or accepted match becomes withdrawn."""
    with _db.db.conn:
        m = _own_match(uid, match_id)
        if m.status not in ("pending", "accepted"):
            raise ValueError("already " + m.status)
        _db.matches.update(id=match_id, status="withdrawn", withdrawn_at=_now())
        _db.db.execute(
            "UPDATE shift SET status = 'cancelled' "
            "WHERE match_id = ? AND status = 'scheduled' AND starts_at > ?",
            [match_id, _local_now()],
        )
        _sync_fill(m.opening_id)


def my_matches(uid):
    """This volunteer's matches, newest first, with the opening's public details."""
    org_names = {o.id: o.name for o in _db.orgs()}
    out = []
    for m in _db.matches(
        where="volunteer_id = ?", where_args=[uid], order_by="id DESC"
    ):
        o = _db.openings[m.opening_id]
        out.append(
            {
                "blocked": _volunteer_blocked_org(uid, o.org_id),
                "id": m.id,
                "status": m.status,
                "title": o.title,
                "org": org_names.get(o.org_id, ""),
                "starts_at": o.starts_at,
                "neighborhood": o.neighborhood,
                "event_url": o.event_url,
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
    labels = {c.slug: c.label for c in _db.causes()}
    interests = dict(INTERESTS)
    rows = _db.db.query(
        "SELECT m.id, m.status, o.title, o.recurrence, p.display_name, p.causes, "
        "p.availability, "
        "p.neighborhood, p.frequency, p.languages, p.last_name, p.pronouns, p.bio, "
        "p.interests, "
        "p.photo_key, " + _BLOCKED + " AS blocked, "
        "(SELECT count(*) FROM match a WHERE a.opening_id = o.id "
        "AND a.status = 'accepted') >= o.slots AS full "
        "FROM match m JOIN opening o ON o.id = m.opening_id "
        "JOIN profile p ON p.id = m.volunteer_id "
        "WHERE o.org_id IN (SELECT org_id FROM org_member WHERE user_id = ?) "
        "AND p.deleted_at IS NULL AND p.suspended_at IS NULL "
        "AND coalesce(m.org_blind, 0) = 0 "
        "AND (m.status IN ('pending', 'accepted', 'declined') "
        "OR (m.status = 'withdrawn' AND m.decided_at IS NOT NULL)) "
        "AND (m.status = 'withdrawn' OR (NOT EXISTS (SELECT 1 FROM block b "
        "WHERE b.blocked_user_id = p.id AND b.blocked_org_id = o.org_id) "
        "AND NOT EXISTS (SELECT 1 FROM block b WHERE b.blocker_id = p.id "
        "AND b.blocked_user_id IS NULL AND b.blocked_org_id = o.org_id))) "
        "ORDER BY m.id DESC",
        [coordinator_uid],
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


MAX_SHIFT_AHEAD = timedelta(days=366)
CORRECTION_WINDOW = timedelta(days=7)
_LOCAL_FORMAT = "%Y-%m-%dT%H:%M"


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


def add_shift(coordinator_uid, match_id, starts_at):
    """Schedule another shift on an accepted match of the coordinator's org; the Shift.

    PermissionError unless a member of the org. ValueError unless the match is accepted
    and starts_at ("YYYY-MM-DDTHH:MM", LA time) is real, in the future, within 366 days,
    and not already a shift of this match.
    """
    if not _is_id(match_id):
        raise PermissionError("no such match")
    try:
        m = _db.matches[match_id]
        o = _db.openings[m.opening_id]
    except NotFoundError:
        raise PermissionError("no such match") from None
    _member_of(coordinator_uid, o.org_id)
    if _db.orgs[o.org_id].deactivated_at:
        raise PermissionError("this org is deactivated")
    if m.status != "accepted":
        raise ValueError("the match is not accepted")
    start = _parse_local(starts_at)
    now = datetime.strptime(_local_now(), _LOCAL_FORMAT)
    if start <= now:
        raise ValueError("the shift has to be in the future")
    if start > now + MAX_SHIFT_AHEAD:
        raise ValueError("the shift must be within the next 12 months")
    try:
        shift = _db.shifts.insert(
            _db.Shift(
                match_id=match_id,
                starts_at=start.strftime(_LOCAL_FORMAT),
                status="scheduled",
                kind="added",
                created_by=coordinator_uid,
                created_at=_now(),
            )
        )
    except apsw.ConstraintError:
        raise ValueError("that shift is already scheduled") from None
    return shift


def repeat_shift(coordinator_uid, match_id):
    """ "Same time next week": add a shift 7 days after the match's latest live shift.

    Only for a weekly opening. Same errors as add_shift, plus ValueError when the
    opening is not weekly or the match has no shift to repeat.
    """
    if not _is_id(match_id):
        raise PermissionError("no such match")
    try:
        o = _db.openings[_db.matches[match_id].opening_id]
    except NotFoundError:
        raise PermissionError("no such match") from None
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
    return add_shift(coordinator_uid, match_id, nxt.strftime(_LOCAL_FORMAT))


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


def complete_shift(coordinator_uid, shift_id, attended):
    """Mark a started shift completed (attended) or no_show; the Shift.

    PermissionError unless a member of the shift's org. ValueError before the shift
    starts, for a cancelled shift, and for a change after the one correction or more than
    7 days after the first mark. Marking the same result again changes nothing.
    """
    with _db.db.conn:
        sh, org_id = _shift_org(shift_id)
        _member_of(coordinator_uid, org_id)
        status = "completed" if attended else "no_show"
        if sh.status == "cancelled":
            raise ValueError("this shift was cancelled")
        if sh.starts_at > _local_now():
            raise ValueError("this shift has not started")
        if sh.status == status:
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
                corrected_at=_now(),
                corrected_by=coordinator_uid,
            )
        else:
            _db.shifts.update(
                id=shift_id, status=status, marked_at=_now(), marked_by=coordinator_uid
            )
        return _db.shifts[shift_id]


# 1 when the volunteer p and the org of opening o blocked each other, either way
_BLOCKED = (
    "EXISTS (SELECT 1 FROM block b WHERE b.blocked_org_id = o.org_id AND "
    "(b.blocked_user_id = p.id OR (b.blocker_id = p.id AND b.blocked_user_id IS NULL)))"
)
_SHIFT_SELECT = (
    "SELECT s.id, s.match_id, s.starts_at, s.status, s.marked_at, s.corrected_at, "
    "o.title, o.recurrence, g.name AS org, p.display_name, p.last_name, p.photo_key, "
    "m.status AS match_status, " + _BLOCKED + " AS blocked "
    "FROM shift s JOIN match m ON m.id = s.match_id "
    "JOIN opening o ON o.id = m.opening_id JOIN org g ON g.id = o.org_id "
    "JOIN profile p ON p.id = m.volunteer_id "
)


def my_shifts(uid):
    """{"upcoming": [...], "past": [...]} of this volunteer's own shifts.

    Upcoming: scheduled, soonest first. Past: everything else but cancelled, latest first;
    a past shift nobody marked yet has status "scheduled".
    """
    now = _local_now()
    out = {"upcoming": [], "past": []}
    for r in _db.db.query(
        _SHIFT_SELECT + "WHERE m.volunteer_id = ? AND s.status != 'cancelled' "
        "ORDER BY s.starts_at, s.id",
        [uid],
    ):
        row = {
            k: r[k] for k in ("id", "match_id", "starts_at", "status", "title", "org")
        }
        upcoming = r["status"] == "scheduled" and r["starts_at"] > now
        out["upcoming" if upcoming else "past"].append(row)
    out["past"].reverse()
    return out


def org_shifts(coordinator_uid):
    """{"upcoming", "to_mark", "marked"} for this coordinator's orgs only.

    to_mark: started and not yet marked. marked: marked within 7 days and still open to
    their one correction. Rows carry the volunteer's name as volunteer_name gives it
    (BLOCKED_NAME under a block) and photo, whether the opening repeats weekly, and "latest" when the row is its match's latest live shift (set only
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
            k: r[k] for k in ("id", "match_id", "starts_at", "status", "title", "org")
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
    if not _is_id(match_id):
        raise PermissionError("no such match")
    try:
        m = _db.matches[match_id]
        o = _db.openings[m.opening_id]
    except NotFoundError:
        raise PermissionError("no such match") from None
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
            or _org_blocked_volunteer(m.volunteer_id, o.org_id)
            or _volunteer_blocked_org(m.volunteer_id, o.org_id)
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
    org_id = _db.openings[m.opening_id].org_id
    return _org_blocked_volunteer(m.volunteer_id, org_id) or _volunteer_blocked_org(
        m.volunteer_id, org_id
    )


def chat_header(uid, match_id):
    """What the chat page shows about the other side.

    The volunteer sees the org name and the contact name of the coordinator who accepted
    them; the coordinator sees the volunteer's name as volunteer_name gives it, and their
    photo when data.photo_for would serve it. Also whether the caller may still write.
    """
    role = chat_access(uid, match_id)
    m = _db.matches[match_id]
    o = _db.openings[m.opening_id]
    org = _db.orgs[o.org_id].name
    photo = None
    if role == "volunteer":
        decider = get_profile(m.decided_by) if m.decided_by else None
        other = decider.display_name if decider and decider.display_name else org
    else:
        volunteer = get_profile(m.volunteer_id)
        # what photo_for hides hides the last name too, and so does a block with the
        # org of this match (photo_for looks at all of the coordinator's orgs)
        blocked = bool(volunteer) and (
            _org_blocked_volunteer(volunteer.id, o.org_id)
            or _volunteer_blocked_org(volunteer.id, o.org_id)
        )
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
                    created_at=datetime.now(UTC).isoformat(timespec="seconds"),
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
    language_other="",
    volunteered_last_year="",
    frequency="",
    heard_from,
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
            language_other=language_other,
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
            onboarded_at=datetime.now(UTC).isoformat(timespec="seconds"),
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
        "surprise": bool(p.surprise),
        "skills": json.loads(p.skills or "[]"),
        "frequency": p.frequency or "",
        "places": _places(p),
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
    surprise=False,
):
    """causes: {slug: love|open|no}; a cause left out is unrated. places: 1 or 2 dicts
    (label, lat, lon, area, mode, max_minutes), the first one Home; they replace the saved
    ones. The profile's own point follows the first place."""
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
            surprise=surprise,
            skills=json.dumps(list(skills)),
            frequency=frequency,
            lat=places[0]["lat"],
            lon=places[0]["lon"],
        )


def save_profile_details(
    uid, *, first_name, last_name, pronouns, bio, interests, neighborhood=None
):
    """A volunteer's own name, pronouns, bio, interests and, unless None, the neighborhood
    nonprofits see (checked by the caller)."""
    p = get_profile(uid)
    if p is None or p.role != "volunteer":
        raise PermissionError("not a volunteer")
    _db.profiles.update(
        id=uid,
        display_name=first_name,
        last_name=last_name or None,
        pronouns=pronouns or None,
        bio=bio or None,
        interests=json.dumps(list(interests)),
        **({} if neighborhood is None else {"neighborhood": neighborhood}),
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
    found = _db.profiles(
        where="role = 'coordinator' AND lower(email) = ? AND deleted_at IS NULL",
        where_args=[email.strip().lower()],
    )
    return found[0].id if found else None


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
    if org_id not in coordinator_org_ids(coordinator_uid):
        raise PermissionError("not a member of this org")
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
    accepted counts every accepted match, as the full check does.
    """
    rows = _db.db.query(
        "SELECT o.id AS id, "
        "(SELECT count(*) FROM match m JOIN profile p ON p.id = m.volunteer_id "
        "WHERE m.opening_id = o.id AND m.status = 'pending' "
        "AND p.deleted_at IS NULL AND p.suspended_at IS NULL "
        "AND coalesce(m.org_blind, 0) = 0 "
        "AND NOT EXISTS (SELECT 1 FROM block b WHERE b.blocked_user_id = p.id "
        "AND b.blocked_org_id = o.org_id) "
        "AND NOT EXISTS (SELECT 1 FROM block b WHERE b.blocker_id = p.id "
        "AND b.blocked_user_id IS NULL AND b.blocked_org_id = o.org_id)) AS pending, "
        "(SELECT count(*) FROM match a WHERE a.opening_id = o.id "
        "AND a.status = 'accepted') AS accepted "
        "FROM opening o "
        "WHERE o.org_id IN (SELECT org_id FROM org_member WHERE user_id = ?)",
        [coordinator_uid],
    )
    return {r["id"]: {"pending": r["pending"], "accepted": r["accepted"]} for r in rows}


def org_waiting(coordinator_uid):
    """How many likes wait for the coordinator's answer (the nav's Inbox badge): the
    same counts as the dashboard's cards, so the two never disagree."""
    return sum(c["pending"] for c in opening_counts(coordinator_uid).values())


def close_opening(coordinator_uid, opening_id):
    if not _is_id(opening_id):
        raise PermissionError("no such opening")
    try:
        o = _db.openings[opening_id]
    except NotFoundError:
        raise PermissionError("no such opening") from None
    if o.org_id not in coordinator_org_ids(coordinator_uid):
        raise PermissionError("not a member of this org")
    with _db.db.conn:
        _db.openings.update(id=opening_id, status="closed")
        _db.db.execute(
            "UPDATE shift SET status = 'cancelled', cancelled_at = ?, cancelled_by = ? "
            "WHERE status = 'scheduled' AND starts_at > ? AND match_id IN "
            "(SELECT id FROM match WHERE opening_id = ?)",
            [_now(), coordinator_uid, _local_now(), opening_id],
        )


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
    """{"volunteer_id", "org", "opening", "starts_at"} of a shift, or None."""
    try:
        sh = _db.shifts[shift_id]
    except NotFoundError:
        return None
    info = match_notice(sh.match_id)
    return info and {**info, "starts_at": sh.starts_at}


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


REPORTS_PER_DAY = 10  # one reporter; item 17 lists it with the other limits


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


def block_org(uid, match_id):
    """A volunteer blocks the org of their own match: its openings leave their deck, their
    pending and accepted matches with it are withdrawn and their future shifts cancelled,
    and chat is read-only."""
    with _db.db.conn:
        m, o = _match_and_opening(match_id)
        if m.volunteer_id != uid:
            raise PermissionError("not your match")
        _add_block(blocker_id=uid, blocked_org_id=o.org_id)
        for mine in _db.matches(
            where="volunteer_id = ? AND status IN ('pending', 'accepted') "
            "AND opening_id IN (SELECT id FROM opening WHERE org_id = ?)",
            where_args=[uid, o.org_id],
        ):
            _end_match(mine, uid, "withdrawn")


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
        for theirs in _db.matches(
            where="volunteer_id = ? AND status IN ('pending', 'accepted') "
            "AND opening_id IN (SELECT id FROM opening WHERE org_id = ?)",
            where_args=[m.volunteer_id, o.org_id],
        ):
            _end_match(theirs, coordinator_uid, "declined")


def _admin(uid):
    p = get_profile(uid)
    if not p or p.role != "admin":
        raise PermissionError("admins only")


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


def verify_org(org_id, ein):
    """Mark an org verified under this EIN (nine digits). Admin-only entry point, used by the
    admin page and scripts/verify_org.py. LookupError for an unknown org; ValueError for a
    malformed EIN or one another org already carries."""
    if not _is_id(org_id) or not org_exists(org_id):
        raise LookupError(f"no org {org_id}")
    if not (isinstance(ein, str) and len(ein) == 9 and ein.isascii() and ein.isdigit()):
        raise ValueError("an EIN is nine digits")
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
    if not (isinstance(ein, str) and len(ein) == 9 and ein.isascii() and ein.isdigit()):
        raise ValueError("an EIN is nine digits")
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
    if not (isinstance(ein, str) and len(ein) == 9 and ein.isascii() and ein.isdigit()):
        raise ValueError("an EIN is nine digits")
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


def pending_claim_ein(admin_uid, claim_id):
    """The EIN a pending claim names, for the admin's "Import this EIN". LookupError when
    the claim is not pending."""
    _admin(admin_uid)
    return _pending_claim(claim_id).ein


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
            "free_dates = NULL, "
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
        p = get_profile(uid)
        if p is None or p.role != "volunteer":
            raise PermissionError("not a volunteer")
        key = store_photo("volunteer", uid, processed)
        _db.profiles.update(id=uid, photo_key=key)
        _drop_photo(p.photo_key)
    return key


def remove_volunteer_photo(uid):
    with _db.db.conn:
        p = get_profile(uid)
        if p is None or p.role != "volunteer":
            raise PermissionError("not a volunteer")
        _drop_photo(p.photo_key)
        _db.profiles.update(id=uid, photo_key=None)


def _coordinated_org(coordinator_uid, org_id):
    if not _is_id(org_id):
        raise PermissionError("no such org")
    _member_of(coordinator_uid, org_id)
    return _db.orgs[org_id]


def coordinator_org(coordinator_uid, org_id):
    """An org this coordinator belongs to; PermissionError for any other id."""
    return _coordinated_org(coordinator_uid, org_id)


def set_org_picture(coordinator_uid, org_id, which, processed):
    """The org's logo or photo (which: logo | photo) becomes this one; the old one goes."""
    column = {"logo": "logo_key", "photo": "photo_key"}[which]
    with _db.db.conn:
        org = _coordinated_org(coordinator_uid, org_id)
        key = store_photo(f"org_{which}", org_id, processed)
        _db.orgs.update(id=org_id, **{column: key})
        _drop_photo(getattr(org, column))
    return key


def set_org_details(coordinator_uid, org_id, about, website):
    """About text and website (https, or None) of an org this coordinator belongs to."""
    with _db.db.conn:
        _coordinated_org(coordinator_uid, org_id)
        _db.orgs.update(id=org_id, about=about, website=website)


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


# One apsw connection is shared by every request thread (and, later, websockets); apsw
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
