import json

from app.db import causes, openings, orgs


def deck(uid=None, limit=20):
    """Open openings with at least one active cause, soonest first."""
    labels = {c.slug: c.label for c in causes() if c.active}
    org_names = {o.id: o.name for o in orgs()}
    cards = []
    for o in openings(where="status = ?", where_args=["open"], order_by="starts_at"):
        cause_labels = [labels[s] for s in json.loads(o.causes) if s in labels]
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
