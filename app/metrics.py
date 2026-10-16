"""Pilot metrics: pure computation over the plain rows from data.metrics_rows().

Every figure is an aggregate; no row here carries a name, phone or email.
"""

from collections import Counter
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
    "accepted_no_shift": "Accepted, no shift",
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
    "accepted_no_shift": (
        "Accepted (or accepted then withdrawn) matches with no non-cancelled shift at all, "
        "such as an accept after the opening started. In neither first-shift count; "
        "withdrawn ones are also in withdrew_before_first_shift."
    ),
    "withdrew_before_first_shift": (
        "Accepted matches withdrawn with no shift left: counted as not showing up in the "
        "first-shift rate."
    ),
    "unmarked_past_shifts": (
        "Every shift that has started and is still unmarked: later shifts and shifts on "
        "withdrawn matches too, not only first shifts."
    ),
    "shift2_rate": (
        "Volunteers with 2+ completed shifts (any opening) / volunteers whose first "
        f"completed shift was {REPEAT_WINDOW_DAYS}+ days ago. Only completed shifts "
        "count; a no-show does not. Cause, intro and heard-from rows place a volunteer "
        "by their first completed shift."
    ),
    "shift2_same_org_rate": (
        "As above, but the 2+ completed shifts must be at one org (any two openings of it)."
    ),
    "eligible_for_repeat": (
        f"Volunteers whose first completed shift was {REPEAT_WINDOW_DAYS}+ days ago / "
        "all volunteers with a completed shift. Newer volunteers are not yet judged."
    ),
    "no_show_rate": "No-shows / (completed + no-shows).",
}
# Last-minute calls (app/data.py post_urgent_call): one table, overall only
URGENT = {
    "urgent_posted": "Urgent calls posted",
    "urgent_filled_rate": "Filled",
    "urgent_median_fill_min": "Median minutes to fill",
    "urgent_no_show_rate": "No-show, urgent fills",
    "normal_no_show_rate": "No-show, other shifts",
}
URGENT_RATES = {"urgent_filled_rate", "urgent_no_show_rate", "normal_no_show_rate"}
MEDIANS = {"median_reply_h", "urgent_median_fill_min"}
URGENT_DEFINITIONS = {
    "urgent_posted": "Urgent calls posted by nonprofits, open ones included.",
    "urgent_filled_rate": (
        "Calls that filled / calls that ended (filled, stopped by the org, their time "
        "passed or their opening closed). Open calls are in neither count."
    ),
    "urgent_median_fill_min": (
        "Median minutes from posting to filling, over the calls that filled; n is the "
        "calls counted."
    ),
    "urgent_no_show_rate": (
        "No-shows / (completed + no-shows), over shifts of likes made on an urgent call. "
        f"Withheld, with the other no-show rate, while either has fewer than "
        f"{MIN_GROUP_VOLUNTEERS} volunteers: overall's no-show rate minus one would give "
        "the other back."
    ),
    "normal_no_show_rate": "The same, over every other shift.",
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
            "same_org": max(Counter(s["org_id"] for s in ss).values()),
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
    done = no_show = unmarked_first = withdrew = no_shift = 0
    for m in accepted:
        s = first.get(m["id"])
        if s is None:
            no_shift += 1
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
        "accepted_no_shift": _count(no_shift),
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
    MIN_GROUP_VOLUNTEERS volunteers (but some) is merged into OTHER; while OTHER is still that
    small the smallest shown row is folded in too, and OTHER is dropped when nothing is left
    to fold. Overall minus the shown rows then never rebuilds a small group. A rate hidden
    as "too few" next to shown rows has its smallest neighbour withheld for the same reason.
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
    empty = rows.get("empty_decks", [])
    out = {
        "overall": _group(swipes, matches, shifts, vols, now),
        "urgent": _urgent(rows),
        "labels": {c["slug"]: c["label"] for c in rows["causes"]},
        # how often filters left nothing to swipe (plan risk: over-filtering)
        "empty_deck": {
            "views": len(empty),
            "volunteers": len({r["volunteer_id"] for r in empty}),
        },
    }
    for kind, ks in keys.items():
        groups = {
            k: slice_(lambda r, k=k, kind=kind: k in group_of(kind, r)) for k in ks
        }

        nvols = {k: g["volunteers"]["n"] for k, g in groups.items()}
        vols_of = nvols.__getitem__

        small = [k for k in ks if 0 < vols_of(k) < MIN_GROUP_VOLUNTEERS]
        shown = [k for k in ks if k not in small]
        merged = None
        while small:
            merged = slice_(
                lambda r, kind=kind, small=small: bool(
                    set(small) & set(group_of(kind, r))
                )
            )
            candidates = [k for k in shown if vols_of(k) > 0]
            if merged["volunteers"]["n"] >= MIN_GROUP_VOLUNTEERS or not candidates:
                break
            # Overall minus the shown rows would rebuild the merged one: fold in the
            # smallest shown row until the remainder is no longer a handful of people.
            smallest = min(candidates, key=vols_of)
            small.append(smallest)
            shown.remove(smallest)
        out[kind] = {k: groups[k] for k in ks if k in shown}
        if merged and merged["volunteers"]["n"] >= MIN_GROUP_VOLUNTEERS:
            out[kind][OTHER] = merged
        if kind != "cause":
            _withhold_complements(out[kind], out["overall"])
    return out


def _withhold_complements(rows, overall):
    """The rows of a partition add up to overall, so one hidden rate beside shown rows and
    a shown overall could be subtracted back. Hide the smallest shown row of that metric too."""
    for metric in RATES:
        cells = [g[metric] for g in rows.values() if g[metric]["d"]]
        hidden = [c for c in cells if too_few(metric, c)]
        shown = [c for c in cells if not too_few(metric, c)]
        if len(hidden) == 1 and shown and not too_few(metric, overall[metric]):
            min(shown, key=lambda c: c["d"])["suppressed"] = True


def _urgent(rows):
    """The URGENT figures, each a cell like _group's."""
    calls = rows.get("urgent_calls", [])
    ended = [c for c in calls if c["status"] != "open"]
    filled = [c for c in ended if c["status"] == "filled" and c["ended_at"]]
    minutes = [_hours(c["created_at"], c["ended_at"]) * 60 for c in filled]

    def no_show(urgent):
        marked = [
            s
            for s in rows["shifts"]
            if bool(s.get("urgent_call_id")) == urgent
            and s["status"] in ("completed", "no_show")
        ]
        cell = _rate(sum(s["status"] == "no_show" for s in marked), len(marked))
        cell["volunteers"] = len({s["volunteer_id"] for s in marked})
        return cell

    out = {
        "urgent_posted": _count(len(calls)),
        "urgent_filled_rate": _rate(
            sum(c["status"] == "filled" for c in ended), len(ended)
        ),
        "urgent_median_fill_min": {
            "n": len(minutes),
            "d": None,
            "value": median(minutes) if minutes else None,
        },
        "urgent_no_show_rate": no_show(True),
        "normal_no_show_rate": no_show(False),
    }
    pair = ("urgent_no_show_rate", "normal_no_show_rate")
    if any(
        withheld(k, out[k]) or 0 < out[k]["volunteers"] < MIN_GROUP_VOLUNTEERS
        for k in pair
    ):
        for k in pair:
            out[k]["suppressed"] = bool(out[k]["d"])
    return out


def label(metrics, kind, key):
    return metrics["labels"].get(key, key) if kind == "cause" else key


def too_few(metric, cell):
    """A rate over fewer than MIN_DENOM, or a median over fewer than MIN_DENOM matches."""
    if metric in MEDIANS:
        return 0 < cell["n"] < MIN_DENOM
    return metric in RATES | URGENT_RATES and bool(cell["d"]) and cell["d"] < MIN_DENOM


def withheld(metric, cell):
    """too_few, or hidden so that a hidden neighbour can't be subtracted back."""
    return too_few(metric, cell) or bool(cell.get("suppressed"))


def fmt(metric, cell):
    """The figure as the page shows it."""
    v = cell["value"]
    if too_few(metric, cell):
        return f"too few (n<{MIN_DENOM})"
    if withheld(metric, cell):
        return "withheld"
    if v is None:
        return "—"
    if metric in MEDIANS:
        return f"{v:.1f}" if metric == "median_reply_h" else f"{v:.0f}"
    if metric in RATES | URGENT_RATES:
        return f"{v * 100:.0f}%"
    return str(v)


def counts(metric, cell):
    """The counts shown beneath the figure."""
    if metric in MEDIANS:
        return f"n={cell['n']}"
    if metric in RATES | URGENT_RATES:
        if not cell["d"]:
            return ""
        return (
            f"n={cell['d']}" if withheld(metric, cell) else f"{cell['n']}/{cell['d']}"
        )
    return "" if cell["d"] is None else f"of {cell['d']}"


def _safe(text):
    """A spreadsheet must not read a cell as a formula."""
    return "'" + text if text and text[0] in "=+-@\t\r" else text


def csv_rows(metrics):
    """Definition comments, then (breakdown, group, metric, n, numerator, denominator, value).

    n is the sample size: a rate's denominator, the median's matches, a count's count.
    numerator and value are empty where a rate or median is too few or withheld.
    """
    for metric, text in {**DEFINITIONS, **URGENT_DEFINITIONS}.items():
        yield (f"# {metric}: {text}",)
    yield ("breakdown", "group", "metric", "n", "numerator", "denominator", "value")
    for kind in BREAKDOWNS:
        groups = {"all": metrics["overall"]} if kind == "overall" else metrics[kind]
        for key, cells in groups.items():
            for metric in METRICS:
                c = cells[metric]
                hide = withheld(metric, c)
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
    for metric in URGENT:
        c = metrics["urgent"][metric]
        hide = withheld(metric, c)
        is_rate = metric in URGENT_RATES
        yield (
            "urgent",
            "all",
            metric,
            c["d"] if is_rate else c["n"],
            "" if hide or not is_rate else c["n"],
            "" if c["d"] is None else c["d"],
            "" if hide or c["value"] is None else round(c["value"], 4),
        )
