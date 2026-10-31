"""The certificate table: a volunteer's signed record of the hours one nonprofit confirmed.

Runs before app/db.py declares the tables, so it creates the table itself (the same columns
as db.Certificate) and the index issue_certificate's daily limit reads.
"""


def up(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS certificate (id TEXT PRIMARY KEY, "
        "volunteer_id INTEGER, org_id INTEGER, period TEXT, minutes INTEGER, "
        "issued_at TEXT, withdrawn_at TEXT, snapshot TEXT, signature TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_certificate_volunteer_id_issued_at "
        "ON certificate (volunteer_id, issued_at)"
    )
