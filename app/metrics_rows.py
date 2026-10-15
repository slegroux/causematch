"""The queries behind the pilot metrics, over any `q(sql) -> [dict]`.

Kept apart from app.data so scripts/stats.py can read a database through a read-only
connection without importing app.db (which creates and migrates the schema it opens).
No names, phones or emails are selected.
"""

import json

_TAG = (
    "o.causes AS causes, coalesce(g.intro, '') AS intro, "
    "coalesce(p.heard_from, '') AS heard_from"
)
_JOIN = (
    "JOIN opening o ON o.id = {t}.opening_id JOIN org g ON g.id = o.org_id "
    "JOIN profile p ON p.id = {t}.volunteer_id"
)


def _causes(rows):
    return [{**r, "causes": json.loads(r["causes"] or "[]")} for r in rows]


def _empty_decks(q):
    """Views of an empty deck; none for a database from before they were counted."""
    if not q(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'deck_empty'"
    ):
        return []
    return q("SELECT volunteer_id, today FROM deck_empty")


def _has(q, table, column=None):
    """The table (and column) exist: a database from before urgent calls has neither."""
    if column is None:
        sql = (
            f"SELECT name FROM sqlite_master WHERE type = 'table' AND name = '{table}'"
        )
    else:
        sql = f"SELECT name FROM pragma_table_info('{table}') WHERE name = '{column}'"
    return bool(q(sql))


def _urgent_calls(q):
    """Every urgent call: status, how many it needed, when it was posted and ended."""
    if not _has(q, "urgent_call"):
        return []
    return q("SELECT id, org_id, status, needed, created_at, ended_at FROM urgent_call")


def fetch_rows(q):
    # a like on an urgent call (Match.urgent_call_id), else NULL
    urgent = (
        "m.urgent_call_id AS urgent_call_id"
        if _has(q, "match", "urgent_call_id")
        else "NULL AS urgent_call_id"
    )
    return {
        "empty_decks": _empty_decks(q),
        "urgent_calls": _urgent_calls(q),
        "causes": [
            {
                "slug": c["slug"],
                "label": c["label"],
                "active": bool(c["active"]),
                "sort": c["sort"],
            }
            for c in q("SELECT slug, label, active, sort FROM cause ORDER BY sort")
        ],
        "swipes": _causes(
            q(
                "SELECT s.volunteer_id AS volunteer_id, s.liked AS liked, "
                f"{_TAG} FROM swipe s "
                + _JOIN.format(t="s")
                + " WHERE NOT EXISTS (SELECT 1 FROM match x WHERE x.org_blind "
                "AND x.volunteer_id = s.volunteer_id AND x.opening_id = s.opening_id)"
            )
        ),
        "matches": _causes(
            q(
                "SELECT m.id AS id, m.volunteer_id AS volunteer_id, m.status AS status, "
                "m.created_at AS created_at, m.decided_at AS decided_at, "
                f"m.withdrawn_at AS withdrawn_at, {_TAG} FROM match m "
                + _JOIN.format(t="m")
                + " WHERE coalesce(m.org_blind, 0) = 0"
            )
        ),
        "shifts": _causes(
            q(
                "SELECT h.match_id AS match_id, m.volunteer_id AS volunteer_id, "
                "o.org_id AS org_id, h.starts_at AS starts_at, h.status AS status, "
                f"{urgent}, {_TAG} FROM shift h JOIN match m ON m.id = h.match_id "
                "JOIN opening o ON o.id = m.opening_id JOIN org g ON g.id = o.org_id "
                "JOIN profile p ON p.id = m.volunteer_id"
            )
        ),
    }
