"""Pilot metrics: pure computation over the plain rows from data.metrics_rows().

Every figure is an aggregate; no row here carries a name, phone or email.
"""

from datetime import UTC, datetime
from statistics import median
from zoneinfo import ZoneInfo

LA = ZoneInfo("America/Los_Angeles")
OVERDUE_HOURS = 48
# metric -> label, in column order
METRICS = {
    "like_rate": "Like rate",
    "accept_rate": "Accept rate",
    "median_reply_h": "Median reply (h)",
    "pending_over_48h": "Pending > 48h",
    "shift1_rate": "First shift done",
    "shift2_rate": "Repeat volunteers",
    "no_show_rate": "No-show rate",
    "unmarked_past_shifts": "Unmarked past shifts",
}
BREAKDOWNS = {
    "overall": "Overall",
    "cause": "By cause",
    "intro": "By how the org was introduced",
    "heard_from": "By how the volunteer heard of us",
}
INTRO = ("warm", "cold")
HEARD_FROM = ("personal", "second_degree", "public", "other")


def _rate(n, d):
    return {"n": n, "d": d, "value": n / d if d else None}


def _hours(a, b):
    return (
        datetime.fromisoformat(b) - datetime.fromisoformat(a)
    ).total_seconds() / 3600


def _group(swipes, matches, shifts, now):
    """All metrics for one slice of rows. Definitions:

    like_rate         likes / swipes
    accept_rate       accepted / (accepted + declined), decided matches; a match withdrawn
                      later counts as neither
    median_reply_h    median hours from created_at to decided_at, over matches with a
                      decided_at (accepted, declined, or withdrawn after an accept)
    pending_over_48h  pending matches created more than 48 hours before `now`
    shift1_rate       accepted matches whose first non-cancelled shift (earliest starts_at)
                      is completed / accepted matches whose first such shift is marked
                      (completed or no_show); a shift not yet marked is in neither count
    unmarked_past_shifts  non-cancelled shifts that have started and are still scheduled:
                      how many shifts the shift1 and no-show figures cannot yet see
    shift2_rate       volunteers with >= 2 completed shifts / volunteers with >= 1, counting
                      the slice's shifts (in a cause or intro row: that cause's or intro's
                      shifts; overall and heard-from: any opening)
    no_show_rate      no_show / (completed + no_show)

    A rate with a zero denominator is None. Each metric is {"n", "d", "value"}; the
    median is {"n": matches counted, "d": None, "value": hours}, the pending count
    {"n": count, "d": None, "value": count}.
    """
    local_now = now.astimezone(LA).strftime("%Y-%m-%dT%H:%M")
    likes = sum(1 for s in swipes if s["liked"])
    accepted = sum(1 for m in matches if m["status"] == "accepted")
    declined = sum(1 for m in matches if m["status"] == "declined")
    replies = [
        _hours(m["created_at"], m["decided_at"]) for m in matches if m["decided_at"]
    ]
    pending = sum(
        1
        for m in matches
        if m["status"] == "pending"
        and _hours(m["created_at"], now.isoformat()) > OVERDUE_HOURS
    )
    accepted_ids = {m["id"] for m in matches if m["status"] == "accepted"}
    first = {}
    for s in sorted(shifts, key=lambda s: s["starts_at"]):
        if s["match_id"] in accepted_ids and s["status"] != "cancelled":
            first.setdefault(s["match_id"], s)
    marked = [s for s in first.values() if s["status"] in ("completed", "no_show")]
    done = sum(1 for s in marked if s["status"] == "completed")
    unmarked = sum(
        1 for s in shifts if s["status"] == "scheduled" and s["starts_at"] <= local_now
    )
    per_volunteer = {}
    for s in shifts:
        if s["status"] == "completed":
            per_volunteer[s["volunteer_id"]] = (
                per_volunteer.get(s["volunteer_id"], 0) + 1
            )
    completed = sum(1 for s in shifts if s["status"] == "completed")
    no_show = sum(1 for s in shifts if s["status"] == "no_show")
    return {
        "like_rate": _rate(likes, len(swipes)),
        "accept_rate": _rate(accepted, accepted + declined),
        "median_reply_h": {
            "n": len(replies),
            "d": None,
            "value": median(replies) if replies else None,
        },
        "pending_over_48h": {"n": pending, "d": None, "value": pending},
        "shift1_rate": _rate(done, len(marked)),
        "shift2_rate": _rate(
            sum(1 for n in per_volunteer.values() if n >= 2), len(per_volunteer)
        ),
        "no_show_rate": _rate(no_show, completed + no_show),
        "unmarked_past_shifts": {"n": unmarked, "d": None, "value": unmarked},
    }


def pilot_metrics(rows, now=None):
    """{"overall": group, "cause": {label: group}, "intro": {...}, "heard_from": {...}}.

    Overall counts every row, soft-deleted volunteers and inactive causes included: it is
    what happened. The by-cause table lists active causes only; a row belongs to each cause
    of its opening, so cause rows add up to more than overall. Unknown or blank heard-from
    values appear as "unknown". See _group for the metric definitions.
    """
    now = now or datetime.now(UTC)
    swipes, matches, shifts = rows["swipes"], rows["matches"], rows["shifts"]

    def slice_(pick):
        return _group(
            [r for r in swipes if pick(r)],
            [r for r in matches if pick(r)],
            [r for r in shifts if pick(r)],
            now,
        )

    heard = (*HEARD_FROM, "unknown")
    out = {
        "overall": _group(swipes, matches, shifts, now),
        "cause": {
            c["label"]: slice_(lambda r, s=c["slug"]: s in r["causes"])
            for c in sorted(rows["causes"], key=lambda c: c["sort"])
            if c["active"]
        },
        "intro": {k: slice_(lambda r, k=k: r["intro"] == k) for k in INTRO},
        "heard_from": {
            k: slice_(
                lambda r, k=k: (
                    (r["heard_from"] if r["heard_from"] in HEARD_FROM else "unknown")
                    == k
                )
            )
            for k in heard
        },
    }
    return out


def fmt(metric, cell):
    """A cell as the page shows it: the figure, then its counts."""
    v = cell["value"]
    if v is None:
        return "—"
    if metric == "median_reply_h":
        return f"{v:.1f}"
    if metric in ("pending_over_48h", "unmarked_past_shifts"):
        return str(v)
    return f"{v * 100:.0f}%"


def csv_rows(metrics):
    """(breakdown, group, metric, numerator, denominator, value) for every cell."""
    yield ("breakdown", "group", "metric", "numerator", "denominator", "value")
    for kind in BREAKDOWNS:
        groups = {"overall": metrics["overall"]} if kind == "overall" else metrics[kind]
        for group, cells in groups.items():
            for metric in METRICS:
                c = cells[metric]
                yield (
                    kind,
                    group,
                    metric,
                    c["n"],
                    "" if c["d"] is None else c["d"],
                    "" if c["value"] is None else round(c["value"], 4),
                )
