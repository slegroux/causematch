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
    """[(opening, active cause labels)] this volunteer could still swipe on, soonest first.

    swipe() checks visibility with this same function, so whatever hides an opening
    from the deck (closed, past, inactive cause, and later radius and blocks) also
    stops a like sent by id.
    """
    labels = {c.slug: c.label for c in _db.causes() if c.active}
    seen = {
        s.opening_id for s in _db.swipes(where="volunteer_id = ?", where_args=[uid])
    }
    out = []
    for o in _db.openings(
        where="status = ? AND starts_at >= ?",
        where_args=["open", _local_now()],
        order_by="starts_at",
    ):
        cause_labels = [labels[s] for s in json.loads(o.causes or "[]") if s in labels]
        if cause_labels and o.id not in seen:
            out.append((o, cause_labels))
    return out


def deck(uid, limit=20):
    """Cards for open openings with an active cause that this volunteer has not swiped."""
    org_names = {o.id: o.name for o in _db.orgs()}
    return [
        {
            "id": o.id,
            "title": o.title,
            "org": org_names.get(o.org_id, ""),
            "causes": cause_labels,
            "starts_at": o.starts_at,
            "neighborhood": o.neighborhood,
            "slots": o.slots,
        }
        for o, cause_labels in _deck_openings(uid)[:limit]
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
        if opening_id not in {o.id for o, _ in _deck_openings(uid)}:
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
):
    """Opening id. Only an org member may post; the status always starts open."""
    if org_id not in coordinator_org_ids(coordinator_uid):
        raise PermissionError("not a member of this org")
    return _db.openings.insert(
        _db.Opening(
            org_id=org_id,
            title=title,
            about=about,
            causes=json.dumps([cause]),
            skills="[]",
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
