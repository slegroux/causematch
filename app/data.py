import functools
import hashlib
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
from app.choices import ANY_DISTANCE_KM


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


def _deck_openings(uid):
    """[(opening, active cause labels, distance_km or None)] this volunteer could still swipe on.

    swipe() checks visibility with this same function, so whatever hides an opening
    from the deck (closed, past, inactive cause, out of radius) also stops a like sent
    by id. Distance is None for a remote opening or when either side has no location.
    """
    labels = {c.slug: c.label for c in _db.causes() if c.active}
    seen = {
        s.opening_id for s in _db.swipes(where="volunteer_id = ?", where_args=[uid])
    }
    me = _db.profiles[uid]
    out = []
    for o in _db.openings(
        where="status = ? AND starts_at >= ?",
        where_args=["open", _local_now()],
        order_by="starts_at",
    ):
        cause_labels = [labels[s] for s in json.loads(o.causes or "[]") if s in labels]
        if not cause_labels or o.id in seen:
            continue
        km = None
        if not o.remote:
            if None in (o.lat, o.lon):
                continue  # an in-person opening with no place cannot be ranked or gated
            if None not in (me.lat, me.lon):
                km = _match.haversine_km(me.lat, me.lon, o.lat, o.lon)
                if (
                    me.radius_km
                    and me.radius_km < ANY_DISTANCE_KM
                    and km > me.radius_km
                ):
                    continue
        out.append((o, cause_labels, km))
    return out


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
        "WHERE p.deleted_at IS NULL AND ("
        "m.status IN ('accepted', 'declined') "
        "OR (m.status = 'pending' AND julianday('now') - julianday(m.created_at) > 2.0) "
        "OR (m.status = 'withdrawn' AND (m.decided_at IS NOT NULL "
        "OR julianday(m.withdrawn_at) - julianday(m.created_at) > 2.0))) "
        "GROUP BY o.org_id"
    )
    return {r["org_id"]: _match.reply_rate(r["fast"], r["eligible"]) for r in rows}


def _distance_label(km):
    return None if km is None else "< 1 km" if km < 1 else f"{round(km)} km"


def deck(uid, limit=20):
    """Cards for open openings with an active cause, best fit first.

    Cards carry the distance rounded to whole km, never coordinates.
    """
    me = _db.profiles[uid]
    active = {c.slug for c in _db.causes() if c.active}
    mine = set(json.loads(me.causes or "[]")) & active, json.loads(me.skills or "[]")
    free = json.loads(me.availability or "[]")
    org_names = {o.id: o.name for o in _db.orgs()}
    rates = _org_reply_rates()
    ranked = []
    for o, cause_labels, km in _deck_openings(uid):
        fit = _match.score(
            causes=(mine[0], set(json.loads(o.causes or "[]")) & active),
            skills=(mine[1], json.loads(o.skills or "[]")),
            distance=_match.distance_score(km, me.radius_km, o.remote),
            availability=_match.availability_overlap(free, o.starts_at, o.recurrence),
            reply=rates.get(o.org_id, 0.5),
        )
        ranked.append((-round(fit, 6), o.starts_at, o.id, o, cause_labels, km))
    ranked.sort(key=lambda r: r[:3])
    return [
        {
            "id": o.id,
            "title": o.title,
            "org": org_names.get(o.org_id, ""),
            "causes": cause_labels,
            "starts_at": o.starts_at,
            "neighborhood": o.neighborhood,
            "slots": o.slots,
            "remote": bool(o.remote),
            "distance": _distance_label(km),
        }
        for *_, o, cause_labels, km in ranked[:limit]
    ]


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


def swipe(uid, opening_id, liked):
    """Record a swipe; a like makes a pending match. The match, or None for a pass.

    Repeating a swipe changes nothing. LookupError when the volunteer could not see
    the opening in their deck (closed, inactive cause, unknown id).
    """
    if not _is_id(opening_id):
        raise LookupError("no such opening")
    with _db.db.conn:
        if _db.swipes(
            where="volunteer_id = ? AND opening_id = ?", where_args=[uid, opening_id]
        ):
            return _match_for(uid, opening_id)
        if opening_id not in {o.id for o, *_ in _deck_openings(uid)}:
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
            return None
        return _db.matches.insert(
            _db.Match(
                opening_id=opening_id,
                volunteer_id=uid,
                status="pending",
                created_at=_now(),
            )
        )


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
            _db.shifts.insert(
                _db.Shift(
                    match_id=match_id,
                    starts_at=o.starts_at,
                    status="scheduled",
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
    return {
        "id": m.id,
        "status": m.status,
        "title": _db.openings[m.opening_id].title,
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
                "id": m.id,
                "status": m.status,
                "title": o.title,
                "org": org_names.get(o.org_id, ""),
                "starts_at": o.starts_at,
                "neighborhood": o.neighborhood,
            }
        )
    return out


def org_inbox(coordinator_uid):
    """{"pending": [...], "decided": [...]} for this coordinator's orgs only.

    Each row carries what the volunteer agreed to share (see choices.SHARED_WITH_NONPROFIT):
    first name, causes, availability, neighborhood, frequency. One query selects only
    those columns, so phone, email, location and surname never leave SQLite.
    """
    labels = {c.slug: c.label for c in _db.causes()}
    rows = _db.db.query(
        "SELECT m.id, m.status, o.title, p.display_name, p.causes, p.availability, "
        "p.neighborhood, p.frequency, "
        "(SELECT count(*) FROM match a WHERE a.opening_id = o.id "
        "AND a.status = 'accepted') >= o.slots AS full "
        "FROM match m JOIN opening o ON o.id = m.opening_id "
        "JOIN profile p ON p.id = m.volunteer_id "
        "WHERE o.org_id IN (SELECT org_id FROM org_member WHERE user_id = ?) "
        "AND p.deleted_at IS NULL AND m.status IN ('pending', 'accepted', 'declined') "
        "ORDER BY m.id DESC",
        [coordinator_uid],
    )
    out = {"pending": [], "decided": []}
    for r in rows:
        name = (r["display_name"] or "").split(maxsplit=1)
        out["pending" if r["status"] == "pending" else "decided"].append(
            {
                "id": r["id"],
                "status": r["status"],
                "opening": r["title"],
                "first_name": name[0] if name else "A volunteer",
                "causes": [labels.get(s, s) for s in json.loads(r["causes"] or "[]")],
                "availability": json.loads(r["availability"] or "[]"),
                "neighborhood": r["neighborhood"],
                "frequency": r["frequency"],
                "full": bool(r["full"]),
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
    """A naive LA wall-clock datetime from "YYYY-MM-DDTHH:MM"; ValueError if malformed
    or if that time does not exist (clocks skip it)."""
    if not isinstance(starts_at, str):
        raise ValueError("not a date and time")  # noqa: TRY004
    start = datetime.strptime(starts_at, _LOCAL_FORMAT)
    if (
        start.replace(tzinfo=LA).astimezone(UTC).astimezone(LA).replace(tzinfo=None)
        != start
    ):
        raise ValueError("that time does not exist")
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
    if m.status != "accepted":
        raise ValueError("the match is not accepted")
    start = _parse_local(starts_at)
    now = datetime.strptime(_local_now(), _LOCAL_FORMAT)
    if start <= now:
        raise ValueError("a shift has to be in the future")
    if start > now + MAX_SHIFT_AHEAD:
        raise ValueError("a shift must be within the next 12 months")
    try:
        shift = _db.shifts.insert(
            _db.Shift(
                match_id=match_id,
                starts_at=start.strftime(_LOCAL_FORMAT),
                status="scheduled",
                created_at=_now(),
            )
        )
    except apsw.ConstraintError:
        raise ValueError("that shift is already scheduled") from None
    return shift


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
            _db.shifts.update(id=shift_id, status=status, corrected_at=_now())
        else:
            _db.shifts.update(
                id=shift_id, status=status, marked_at=_now(), marked_by=coordinator_uid
            )
        return _db.shifts[shift_id]


_SHIFT_SELECT = (
    "SELECT s.id, s.match_id, s.starts_at, s.status, s.marked_at, s.corrected_at, "
    "o.title, g.name AS org, p.display_name "
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
    their one correction. Rows carry the volunteer's first name only.
    """
    now = _local_now()
    out = {"upcoming": [], "to_mark": [], "marked": []}
    cutoff = datetime.fromisoformat(_now()) - CORRECTION_WINDOW
    for r in _db.db.query(
        _SHIFT_SELECT + "WHERE o.org_id IN "
        "(SELECT org_id FROM org_member WHERE user_id = ?) "
        "AND p.deleted_at IS NULL AND s.status != 'cancelled' "
        "ORDER BY s.starts_at, s.id",
        [coordinator_uid],
    ):
        row = {
            k: r[k] for k in ("id", "match_id", "starts_at", "status", "title", "org")
        }
        row["first_name"] = _first_name(r["display_name"])
        if r["status"] == "scheduled":
            out["upcoming" if r["starts_at"] > now else "to_mark"].append(row)
        elif not r["corrected_at"] and datetime.fromisoformat(r["marked_at"]) >= cutoff:
            out["marked"].append(row)
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
        return role
    raise PermissionError("no chat on this match")


def _first_name(name):
    parts = (name or "").split(maxsplit=1)
    return parts[0] if parts else "Volunteer"


def _message_row(msg, volunteer_id, names):
    return {
        "id": msg.id,
        "sender_uid": msg.sender_id,
        "sender": _first_name(names.get(msg.sender_id))
        if msg.sender_id == volunteer_id
        else names.get(msg.sender_id) or "Coordinator",
        "body": msg.body,
        "created_at": msg.created_at,
    }


def _names(ids):
    return {i: p.display_name for i in ids if (p := get_profile(i))}


def send_message(uid, match_id, body):
    """Save a message from a participant of an accepted match and return it.

    ValueError for a non-string, an empty message, or one over 2000 characters;
    PermissionError when this user may not write in this chat.
    """
    if not isinstance(body, str):
        raise ValueError("message must be text")  # noqa: TRY004
    body = body.strip()
    if not body or len(body) > MAX_MESSAGE:
        raise ValueError(f"message must be 1 to {MAX_MESSAGE} characters")
    chat_access(uid, match_id, write=True)
    msg = _db.messages.insert(
        match_id=match_id, sender_id=uid, body=body, created_at=_now()
    )
    m = _db.matches[match_id]
    return _message_row(msg, m.volunteer_id, _names([uid]))


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
    return [_message_row(r, m.volunteer_id, names) for r in rows]


def chat_header(uid, match_id):
    """What the chat page shows about the other side.

    The volunteer sees the org name and the contact name of the coordinator who accepted
    them; the coordinator sees the volunteer's first name only. Also whether the caller
    may still write.
    """
    role = chat_access(uid, match_id)
    m = _db.matches[match_id]
    o = _db.openings[m.opening_id]
    org = _db.orgs[o.org_id].name
    if role == "volunteer":
        decider = get_profile(m.decided_by) if m.decided_by else None
        other = decider.display_name if decider and decider.display_name else org
    else:
        volunteer = get_profile(m.volunteer_id)
        other = _first_name(volunteer.display_name if volunteer else "")
    return {
        "role": role,
        "title": o.title,
        "org": org,
        "other": other,
        "writable": m.status == "accepted",
    }


def active_causes():
    return [c for c in _db.causes(order_by="sort") if c.active]


def get_or_create_volunteer(phone):
    """Profile id for this E.164 phone, creating a volunteer profile on first sign-in."""
    where = {"where": "phone = ? AND deleted_at IS NULL", "where_args": [phone]}
    if found := _db.profiles(**where):
        return found[0].id
    try:
        return _db.profiles.insert(
            _db.Profile(
                role="volunteer",
                phone=phone,
                session_version=1,
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
        ).id
    except apsw.ConstraintError:  # a concurrent first sign-in won the race
        return _db.profiles(**where)[0].id


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
    return None if p.deleted_at else p


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
):
    _db.profiles.update(
        id=uid,
        display_name=display_name,
        email=email,
        neighborhood=neighborhood,
        language_other=language_other,
        volunteered_last_year=volunteered_last_year,
        adult_attested=True,
        lat=lat,
        lon=lon,
        radius_km=radius_km,
        causes=json.dumps(list(causes)),
        other_causes=other_causes,
        availability=json.dumps(list(availability)),
        skills=json.dumps(list(skills)),
        frequency=frequency,
        heard_from=heard_from,
        onboarded_at=datetime.now(UTC).isoformat(timespec="seconds"),
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
):
    """Opening id. Only an org member may post; the status always starts open.

    ValueError without coordinates: an in-person opening needs a place to be ranked.
    """
    if org_id not in coordinator_org_ids(coordinator_uid):
        raise PermissionError("not a member of this org")
    if lat is None or lon is None:
        raise ValueError("an opening needs coordinates")
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


def close_opening(coordinator_uid, opening_id):
    if not _is_id(opening_id):
        raise PermissionError("no such opening")
    try:
        o = _db.openings[opening_id]
    except NotFoundError:
        raise PermissionError("no such opening") from None
    if o.org_id not in coordinator_org_ids(coordinator_uid):
        raise PermissionError("not a member of this org")
    _db.openings.update(id=opening_id, status="closed")


def notification_claim(kind, key, recipient_id):
    """Reserve one logical send: the notification id, or None when `key` was already sent.

    A send that failed earlier is reserved again, so a later attempt can retry it.
    """
    with _db.db.conn:
        if not _db.notifications(where="key = ?", where_args=[key]):
            return _db.notifications.insert(
                _db.Notification(
                    kind=kind,
                    key=key,
                    recipient_id=recipient_id,
                    sent_at="",
                    status="pending",
                )
            ).id
        _db.db.execute(
            "UPDATE notification SET status = 'pending' "
            "WHERE key = ? AND status = 'failed'",
            [key],
        )
        if _db.db.conn.changes() != 1:
            return None
        return _db.notifications(where="key = ?", where_args=[key])[0].id


def notification_done(notification_id, status):
    _db.notifications.update(id=notification_id, status=status, sent_at=_now())


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
    }


def org_pending_count(org_id):
    return _db.db.execute(
        "SELECT count(*) FROM match JOIN opening ON opening.id = match.opening_id "
        "WHERE opening.org_id = ? AND match.status = 'pending'",
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
