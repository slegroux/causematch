"""identity.authoritative: whether the identity's email may hold its account for a join.

1 (data.HOLDER): the emailed link's address, or a Google or Apple one on a mailbox the
provider runs itself (Gmail, iCloud). A sign-in by another way with that exact address
joins the volunteer (data.volunteer_for_identity). 2 (data.JOINER), set from then on, is
a Workspace account's (hd): it proves the mailbox at sign-in but holds nothing, since a
Workspace may give the address to someone else later. Runs before app/db.py declares
the tables, so it adds the column itself. Existing rows are judged by their address
alone; a Workspace row's hd was never stored, and it stays NULL, which holds nothing,
as a JOINER would. Every later sign-in by the identity sets the column anew.
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
