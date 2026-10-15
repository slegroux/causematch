import json
from datetime import UTC, datetime

from apswutils.db import NotFoundError

from app.db import Profile, causes, openings, orgs, profiles


def deck(uid=None, limit=20):
    """Open openings with at least one active cause, soonest first."""
    labels = {c.slug: c.label for c in causes() if c.active}
    org_names = {o.id: o.name for o in orgs()}
    cards = []
    for o in openings(where="status = ?", where_args=["open"], order_by="starts_at"):
        cause_labels = [labels[s] for s in json.loads(o.causes or "[]") if s in labels]
        if not cause_labels:
            continue
        cards.append(
            {
                "id": o.id,
                "title": o.title,
                "org": org_names.get(o.org_id, ""),
                "causes": cause_labels,
                "starts_at": o.starts_at,
                "neighborhood": o.neighborhood,
                "slots": o.slots,
            }
        )
    return cards[:limit]


def active_causes():
    return [c for c in causes(order_by="sort") if c.active]


def get_or_create_volunteer(phone):
    """Profile id for this E.164 phone, creating a volunteer profile on first sign-in."""
    found = profiles(where="phone = ? AND deleted_at IS NULL", where_args=[phone])
    if found:
        return found[0].id
    return profiles.insert(
        Profile(
            role="volunteer",
            phone=phone,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
    ).id


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
    lat,
    lon,
    radius_km,
    causes,
    other_causes="",
    availability=(),
    skills=(),
    frequency="",
    heard_from,
):
    profiles.update(
        id=uid,
        display_name=display_name,
        email=email,
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
