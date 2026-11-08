"""identity.authoritative: whether the identity's email is the mailbox's current holder's.

A Google or Apple address counts only where the provider hosts the mailbox (as
oauth.authoritative judges a staff sign-in); an emailed link's always does. A sign-in by one
way joins the volunteer who holds that address by another only through such an identity
(data.volunteer_for_identity). Runs before app/db.py declares the tables, so it adds the
column itself. Existing rows are judged by their address alone: a Workspace account's hd
was never stored, so its rows stay NULL, and an address that never joins is the safe side.
"""

GOOGLE = ("gmail.com", "googlemail.com")
APPLE = ("icloud.com", "me.com", "mac.com")


def _hosted(provider, domains):
    ends = " OR ".join("lower(email) LIKE ?" for _ in domains)
    return f"(provider = '{provider}' AND ({ends}))", [f"%@{d}" for d in domains]


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(identity)")}
    if cols and "authoritative" not in cols:
        conn.execute("ALTER TABLE identity ADD COLUMN authoritative INTEGER")
        google, g_args = _hosted("google", GOOGLE)
        apple, a_args = _hosted("apple", APPLE)
        conn.execute(
            f"UPDATE identity SET authoritative = 1 "
            f"WHERE provider = 'email' OR {google} OR {apple}",
            g_args + a_args,
        )
