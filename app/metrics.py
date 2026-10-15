"""Pilot metrics: pure computation over the plain rows from data.metrics_rows().

Every figure is an aggregate; no row here carries a name, phone or email.
"""

from datetime import UTC, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

LA = ZoneInfo("America/Los_Angeles")
LOCAL = "%Y-%m-%dT%H:%M"
OVERDUE_HOURS = 48
REPEAT_WINDOW_DAYS = 14  # a volunteer needs this long since their first completed shift
MIN_DENOM = 10  # below this a rate is shown as "too few", not as a percentage
MIN_GROUP_VOLUNTEERS = 5  # a breakdown row with fewer volunteers is merged into OTHER
OTHER = "other (small groups)"

# metric -> label, in column order
METRICS = {
    "volunteers": "Volunteers",
    "like_rate": "Like rate",
    "accept_rate": "Accept rate",
    "replied_within_48h_rate": "Org replied < 48h",
    "median_reply_h": "Median reply (h)",
    "pending_over_48h": "Pending > 48h",
    "shift1_rate": "First shift done",
    "unmarked_first_shifts": "First shift unmarked",
    "withdrew_before_first_shift": "Withdrew before 1st shift",
    "unmarked_past_shifts": "Unmarked past shifts",
    "shift2_rate": "Repeat (2+ shifts)",
    "shift2_same_org_rate": "Repeat, same org",
    "eligible_for_repeat": "Eligible for repeat",
    "no_show_rate": "No-show rate",
}
RATES = {
    "like_rate",
    "accept_rate",
    "replied_within_48h_rate",
    "shift1_rate",
    "shift2_rate",
    "shift2_same_org_rate",
    "no_show_rate",
}
DEFINITIONS = {
    "volunteers": "Distinct volunteers with a swipe, match or shift in the row.",
    "like_rate": "Likes / swipes.",
    "accept_rate": (
        "Accepted / (accepted + declined). A match accepted and later withdrawn still "
        "counts as accepted: the org did accept."
    ),
    "replied_within_48h_rate": (
        "Matches decided within 48 hours / matches old enough to judge: decided, "
        "pending after 48 hours, or withdrawn undecided after 48 hours."
    ),
    "median_reply_h": "Median hours from like to accept or decline; n is the matches counted.",
    "pending_over_48h": "Pending matches created more than 48 hours ago.",
    "shift1_rate": (
        "Accepted matches whose first non-cancelled shift (earliest start) was completed / "
        "those whose first shift was completed or a no-show, plus those withdrawn before "
        "any shift. A first shift not yet marked is in neither count."
    ),
    "unmarked_first_shifts": "First shifts that have started and are still unmarked.",
    "withdrew_before_first_shift": (
        "Accepted matches withdrawn with no shift left: counted as not showing up in the "
        "first-shift rate."
    ),
    "unmarked_past_shifts": "Any shift that has started and is still unmarked.",
    "shift2_rate": (
        "Volunteers with 2+ completed shifts (any opening) / volunteers whose first "
        f"completed shift was {REPEAT_WINDOW_DAYS}+ days ago. Only completed shifts "
        "count; a no-show does not. Cause, intro and heard-from rows place a volunteer "
        "by their first completed shift."
    ),
    "shift2_same_org_rate": (
        "As above, but the 2+ completed shifts must be at the org of the first one."
    ),
    "eligible_for_repeat": (
        f"Volunteers whose first completed shift was {REPEAT_WINDOW_DAYS}+ days ago / "
        "all volunteers with a completed shift. Newer volunteers are not yet judged."
    ),
    "no_show_rate": "No-shows / (completed + no-shows).",
}
BREAKDOWNS = {
    "overall": "Overall",
    "cause": "By cause (an opening counts under each of its causes)",
    "intro": "By how the org was introduced",
    "heard_from": "By how the volunteer heard of us",
}
INTRO = ("warm", "cold", "unknown")
HEARD_FROM = ("personal", "second_degree", "public", "other", "unknown")


def _rate(n, d):
    return {"n": n, "d": d, "value": n / d if d else None}


def _count(n, d=None):
    return {"n": n, "d": d, "value": n}


def _hours(a, b):
    return (
        datetime.fromisoformat(b) - datetime.fromisoformat(a)
    ).total_seconds() / 3600


def _volunteers(shifts, now):
    """One record per volunteer with a completed shift, tagged by their FIRST one."""
    cutoff = (now.astimezone(LA) - timedelta(days=REPEAT_WINDOW_DAYS)).strftime(LOCAL)
    done = {}
    for s in sorted(shifts, key=lambda s: s["starts_at"]):
        if s["status"] == "completed":
            done.setdefault(s["volunteer_id"], []).append(s)
    return [
        {
            "volunteer_id": vid,
            "causes": first["causes"],
            "intro": first["intro"],
            "heard_from": first["heard_from"],
            "completed": len(ss),
            "same_org": sum(1 for s in ss if s["org_id"] == first["org_id"]),
            "eligible": first["starts_at"] <= cutoff,
        }
        for vid, ss in done.items()
        for first in ss[:1]
    ]


def _group(swipes, matches, shifts, vols, now):
    """All metrics for one slice of rows; the definitions are in DEFINITIONS.

    A rate with a zero denominator is None. Each metric is {"n", "d", "value"}: for a
    rate n/d are its counts; for the median n is the matches counted and d is None; for
    a count n and value are the count.
    """
    local_now = now.astimezone(LA).strftime(LOCAL)
    likes = sum(1 for s in swipes if s["liked"])

    def ever_accepted(m):
        return m["status"] == "accepted" or (
            m["status"] == "withdrawn" and bool(m["decided_at"])
        )

    accepted = [m for m in matches if ever_accepted(m)]
    declined = sum(1 for m in matches if m["status"] == "declined")
    replies = [
        _hours(m["created_at"], m["decided_at"]) for m in matches if m["decided_at"]
    ]

    def age(m):
        return _hours(m["created_at"], now.isoformat())

    pending = sum(
        1 for m in matches if m["status"] == "pending" and age(m) > OVERDUE_HOURS
    )
    judged = [
        m
        for m in matches
        if m["decided_at"]
        or (m["status"] == "pending" and age(m) > OVERDUE_HOURS)
        or (
            m["status"] == "withdrawn"
            and m["withdrawn_at"]
            and _hours(m["created_at"], m["withdrawn_at"]) > OVERDUE_HOURS
        )
    ]
    fast = sum(
        1
        for m in judged
        if m["decided_at"] and _hours(m["created_at"], m["decided_at"]) <= OVERDUE_HOURS
    )
    first = {}
    for s in sorted(shifts, key=lambda s: s["starts_at"]):
        if s["status"] != "cancelled":
            first.setdefault(s["match_id"], s)
    done = no_show = unmarked_first = withdrew = 0
    for m in accepted:
        s = first.get(m["id"])
        if s is None:
            withdrew += m["status"] == "withdrawn"
        elif s["status"] == "completed":
            done += 1
        elif s["status"] == "no_show":
            no_show += 1
        elif s["status"] == "scheduled" and s["starts_at"] <= local_now:
            unmarked_first += 1
    unmarked = sum(
        1 for s in shifts if s["status"] == "scheduled" and s["starts_at"] <= local_now
    )
    completed = sum(1 for s in shifts if s["status"] == "completed")
    all_no_show = sum(1 for s in shifts if s["status"] == "no_show")
    eligible = [v for v in vols if v["eligible"]]
    return {
        "volunteers": _count(
            len(
                {r["volunteer_id"] for r in swipes}
                | {r["volunteer_id"] for r in matches}
                | {r["volunteer_id"] for r in shifts}
            )
        ),
        "like_rate": _rate(likes, len(swipes)),
        "accept_rate": _rate(len(accepted), len(accepted) + declined),
        "replied_within_48h_rate": _rate(fast, len(judged)),
        "median_reply_h": {
            "n": len(replies),
            "d": None,
            "value": median(replies) if replies else None,
        },
        "pending_over_48h": _count(pending),
        "shift1_rate": _rate(done, done + no_show + withdrew),
        "unmarked_first_shifts": _count(unmarked_first),
        "withdrew_before_first_shift": _count(withdrew),
        "unmarked_past_shifts": _count(unmarked),
        "shift2_rate": _rate(
            sum(1 for v in eligible if v["completed"] >= 2), len(eligible)
        ),
        "shift2_same_org_rate": _rate(
            sum(1 for v in eligible if v["same_org"] >= 2), len(eligible)
        ),
        "eligible_for_repeat": _count(len(eligible), len(vols)),
        "no_show_rate": _rate(all_no_show, completed + all_no_show),
    }


def pilot_metrics(rows, now=None):
    """{"overall": group, "cause"|"intro"|"heard_from": {key: group}, "labels": {...}}.

    Overall counts every row, soft-deleted volunteers and inactive causes included: it is
    what happened. The by-cause table lists active causes only; a row belongs to each cause
    of its opening, so cause rows add up to more than overall. Every other row places a
    volunteer's repeat by their first completed shift. A row with fewer than
    MIN_GROUP_VOLUNTEERS volunteers (but some) is merged into OTHER, and OTHER is dropped
    when it is still that small, so no row describes a handful of people.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    swipes, matches, shifts = rows["swipes"], rows["matches"], rows["shifts"]
    vols = _volunteers(shifts, now)

    def slice_(pick):
        return _group(
            [r for r in swipes if pick(r)],
            [r for r in matches if pick(r)],
            [r for r in shifts if pick(r)],
            [v for v in vols if pick(v)],
            now,
        )

    def group_of(kind, row):
        if kind == "cause":
            return row["causes"]
        value = row[kind]
        known = INTRO if kind == "intro" else HEARD_FROM
        return [value if value in known else "unknown"]

    causes = sorted((c for c in rows["causes"] if c["active"]), key=lambda c: c["sort"])
    keys = {
        "cause": [c["slug"] for c in causes],
        "intro": list(INTRO),
        "heard_from": list(HEARD_FROM),
    }
    out = {
        "overall": _group(swipes, matches, shifts, vols, now),
        "labels": {c["slug"]: c["label"] for c in rows["causes"]},
    }
    for kind, ks in keys.items():
        groups = {
            k: slice_(lambda r, k=k, kind=kind: k in group_of(kind, r)) for k in ks
        }
        small = {
            k
            for k, g in groups.items()
            if 0 < g["volunteers"]["n"] < MIN_GROUP_VOLUNTEERS
        }
        out[kind] = {k: g for k, g in groups.items() if k not in small}
        if small:
            merged = slice_(
                lambda r, kind=kind, small=small: bool(small & set(group_of(kind, r)))
            )
            if merged["volunteers"]["n"] >= MIN_GROUP_VOLUNTEERS:
                out[kind][OTHER] = merged
    return out


def label(metrics, kind, key):
    return metrics["labels"].get(key, key) if kind == "cause" else key


def too_few(metric, cell):
    """A rate over fewer than MIN_DENOM: shown as "too few", never as a percentage."""
    return metric in RATES and bool(cell["d"]) and cell["d"] < MIN_DENOM


def fmt(metric, cell):
    """The figure as the page shows it."""
    v = cell["value"]
    if too_few(metric, cell):
        return f"too few (n<{MIN_DENOM})"
    if v is None:
        return "—"
    if metric == "median_reply_h":
        return f"{v:.1f}"
    if metric in RATES:
        return f"{v * 100:.0f}%"
    return str(v)


def counts(metric, cell):
    """The counts shown beneath the figure."""
    if metric == "median_reply_h":
        return f"n={cell['n']}"
    if metric in RATES:
        if not cell["d"]:
            return ""
        return f"n={cell['d']}" if too_few(metric, cell) else f"{cell['n']}/{cell['d']}"
    return "" if cell["d"] is None else f"of {cell['d']}"


def _safe(text):
    """A spreadsheet must not read a cell as a formula."""
    return "'" + text if text and text[0] in "=+-@\t\r" else text


def csv_rows(metrics):
    """Definition comments, then (breakdown, group, metric, n, numerator, denominator, value).

    n is the sample size: a rate's denominator, the median's matches, a count's count.
    numerator and value are empty where a rate has fewer than MIN_DENOM observations.
    """
    for metric, text in DEFINITIONS.items():
        yield (f"# {metric}: {text}",)
    yield ("breakdown", "group", "metric", "n", "numerator", "denominator", "value")
    for kind in BREAKDOWNS:
        groups = {"all": metrics["overall"]} if kind == "overall" else metrics[kind]
        for key, cells in groups.items():
            for metric in METRICS:
                c = cells[metric]
                hide = too_few(metric, c)
                is_rate = metric in RATES
                yield (
                    kind,
                    _safe(label(metrics, kind, key)),
                    metric,
                    c["d"] if is_rate else c["n"],
                    "" if hide or not is_rate else c["n"],
                    "" if c["d"] is None else c["d"],
                    "" if hide or c["value"] is None else round(c["value"], 4),
                )
