import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

import apsw
from apswutils.db import NotFoundError

from app.db import (
    LoginToken,
    Match,
    Opening,
    Profile,
    Swipe,
    causes,
    db,
    login_tokens,
    matches,
    openings,
    org_members,
    orgs,
    profiles,
    swipes,
)


def _deck_openings(uid):
    """[(opening, active cause labels)] this volunteer could still swipe on, soonest first."""
    labels = {c.slug: c.label for c in causes() if c.active}
    seen = {s.opening_id for s in swipes(where="volunteer_id = ?", where_args=[uid])}
    out = []
    for o in openings(where="status = ?", where_args=["open"], order_by="starts_at"):
        cause_labels = [labels[s] for s in json.loads(o.causes or "[]") if s in labels]
        if cause_labels and o.id not in seen:
            out.append((o, cause_labels))
    return out


def deck(uid, limit=20):
    """Cards for open openings with an active cause that this volunteer has not swiped."""
    org_names = {o.id: o.name for o in orgs()}
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
    return matches.count_where("opening_id = ? AND status = 'accepted'", [opening_id])


def _match_for(uid, opening_id):
    found = matches(
        where="volunteer_id = ? AND opening_id = ?", where_args=[uid, opening_id]
    )
    return found[0] if found else None


def swipe(uid, opening_id, liked):
    """Record a swipe; a like makes a pending match. The match, or None for a pass.

    Repeating a swipe changes nothing. LookupError when the volunteer could not see
    the opening in their deck (closed, inactive cause, unknown id).
    """
    with db.conn:
        if swipes(
            where="volunteer_id = ? AND opening_id = ?", where_args=[uid, opening_id]
        ):
            return _match_for(uid, opening_id)
        if opening_id not in {o.id for o, _ in _deck_openings(uid)}:
            raise LookupError("no such opening")
        swipes.insert(
            Swipe(
                volunteer_id=uid,
                opening_id=opening_id,
                liked=bool(liked),
                created_at=_now(),
            )
        )
        if not liked:
            return None
        return matches.insert(
            Match(
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
    try:
        m = matches[match_id]
        o = openings[m.opening_id]
    except NotFoundError:
        raise PermissionError("no such match") from None
    if o.org_id not in coordinator_org_ids(coordinator_uid):
        raise PermissionError("not a member of this org")
    status = "accepted" if accept else "declined"
    with db.conn:
        # One statement, so two coordinators can't both take the last slot.
        db.execute(
            "UPDATE match SET status = ?, decided_at = ?, decided_by = ? "
            "WHERE id = ? AND status = 'pending' AND (? = 0 OR "
            "(SELECT count(*) FROM match WHERE opening_id = ? AND status = 'accepted') "
            "< (SELECT slots FROM opening WHERE id = ?))",
            [status, _now(), coordinator_uid, match_id, int(bool(accept)),
             o.id, o.id],
        )  # fmt: skip
        if db.conn.changes() != 1:
            now_status = matches[match_id].status
            raise ValueError(
                "already decided" if now_status != "pending" else "no slots left"
            )
        if accept and _accepted(o.id) >= (o.slots or 0):
            openings.update(id=o.id, status="filled")


def withdraw(uid, match_id):
    """The volunteer's own pending or accepted match becomes withdrawn."""
    with db.conn:
        try:
            m = matches[match_id]
        except NotFoundError:
            raise PermissionError("no such match") from None
        if m.volunteer_id != uid:
            raise PermissionError("not your match")
        if m.status not in ("pending", "accepted"):
            raise ValueError("already " + m.status)
        matches.update(id=match_id, status="withdrawn", decided_at=_now())
        o = openings[m.opening_id]
        if o.status == "filled" and _accepted(o.id) < (o.slots or 0):
            openings.update(id=o.id, status="open")


def my_matches(uid):
    """This volunteer's matches, newest first, with the opening's public details."""
    org_names = {o.id: o.name for o in orgs()}
    out = []
    for m in matches(where="volunteer_id = ?", where_args=[uid], order_by="id DESC"):
        o = openings[m.opening_id]
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

    Each row carries what the volunteer agreed to share: first name, causes,
    availability, neighborhood, frequency. Never phone, email, location or surname.
    """
    mine = set(coordinator_org_ids(coordinator_uid))
    labels = {c.slug: c.label for c in causes()}
    out = {"pending": [], "decided": []}
    for m in matches(
        where="status IN ('pending', 'accepted', 'declined')", order_by="id DESC"
    ):
        o = openings[m.opening_id]
        v = get_profile(m.volunteer_id)
        if o.org_id not in mine or v is None:
            continue
        row = {
            "id": m.id,
            "status": m.status,
            "opening": o.title,
            "first_name": (v.display_name or "").split(maxsplit=1)[0]
            if (v.display_name or "").strip()
            else "A volunteer",
            "causes": [labels.get(s, s) for s in json.loads(v.causes or "[]")],
            "availability": json.loads(v.availability or "[]"),
            "neighborhood": v.neighborhood,
            "frequency": v.frequency,
        }
        out["pending" if m.status == "pending" else "decided"].append(row)
    return out


def active_causes():
    return [c for c in causes(order_by="sort") if c.active]


def get_or_create_volunteer(phone):
    """Profile id for this E.164 phone, creating a volunteer profile on first sign-in."""
    where = {"where": "phone = ? AND deleted_at IS NULL", "where_args": [phone]}
    if found := profiles(**where):
        return found[0].id
    try:
        return profiles.insert(
            Profile(
                role="volunteer",
                phone=phone,
                session_version=1,
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
        ).id
    except apsw.ConstraintError:  # a concurrent first sign-in won the race
        return profiles(**where)[0].id


def sign_out_everywhere(uid):
    """Invalidate every session cookie this profile holds."""
    profiles.update(id=uid, session_version=(profiles[uid].session_version or 1) + 1)


def get_profile(uid):
    try:
        p = profiles[uid]
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
    profiles.update(
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
    return bool(orgs(where="id = ?", where_args=[org_id]))


def add_coordinator(email, org_id, role="coordinator"):
    """Admin-only entry point (scripts/add_coordinator.py): profile id of the coordinator."""
    if role not in ("owner", "coordinator"):
        raise ValueError(role)
    if not org_exists(org_id):
        raise LookupError(f"no org {org_id}")
    email = email.strip().lower()
    found = coordinator_by_email(email)
    if found:
        uid = found
    else:
        uid = profiles.insert(
            Profile(
                role="coordinator",
                email=email,
                session_version=1,
                created_at=_now(),
            )
        ).id
    if not org_members(where="org_id = ? AND user_id = ?", where_args=[org_id, uid]):
        org_members.insert({"org_id": org_id, "user_id": uid, "role": role})
    return uid


def coordinator_by_email(email):
    found = profiles(
        where="role = 'coordinator' AND lower(email) = ? AND deleted_at IS NULL",
        where_args=[email.strip().lower()],
    )
    return found[0].id if found else None


def coordinator_org_ids(uid):
    return [m.org_id for m in org_members(where="user_id = ?", where_args=[uid])]


def coordinator_orgs(uid):
    """[(org, member role)] for the orgs this coordinator belongs to."""
    by_id = {o.id: o for o in orgs()}
    return [
        (by_id[m.org_id], m.role)
        for m in org_members(where="user_id = ?", where_args=[uid], order_by="org_id")
        if m.org_id in by_id
    ]


def create_login_token(uid):
    """A fresh single-use token for the emailer; only its SHA-256 is stored."""
    token = secrets.token_urlsafe(32)
    login_tokens.delete_where("user_id = ? OR expires_at < ?", [uid, _now()])
    login_tokens.insert(
        LoginToken(
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
    db.execute(
        "UPDATE login_token SET used_at = ? WHERE token_hash = ? "
        "AND used_at IS NULL AND expires_at > ?",
        [_now(), _hash(token), _now()],
    )
    if db.conn.changes() != 1:
        return None
    return login_tokens[_hash(token)].user_id


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
    return openings.insert(
        Opening(
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
        org_id: openings(
            where="org_id = ?", where_args=[org_id], order_by="starts_at DESC"
        )
        for org_id in mine
    }


def close_opening(coordinator_uid, opening_id):
    try:
        o = openings[opening_id]
    except NotFoundError:
        raise PermissionError("no such opening") from None
    if o.org_id not in coordinator_org_ids(coordinator_uid):
        raise PermissionError("not a member of this org")
    openings.update(id=opening_id, status="closed")
