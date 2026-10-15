"""Run Causematch on a throwaway database full of demo data, for showing it to people.

uv run python scripts/demo.py [--port 5077] [--db PATH] [--seed-file PATH]

Local only: it refuses CAUSEMATCH_ENV=production and Cloud Run (K_SERVICE), runs in
development mode with the fake SMS (every code is 000000) and the fake emailer, and uses a
new database in a temporary folder unless --db names a path that doesn't exist yet.

Without --seed-file the orgs are the invented ones in app/db.py. --seed-file loads
nonprofits from a JSON file kept outside this repo (the repo is public: real names never
go in it): a list of {name, ein, ntee_code, cause, website, mission, neighborhood, zip,
lat, lon, volunteer_url, logo_url, logo_file, openings: [{title, about, skills,
typical_time, recurrence, orientation_required, source_url}]}. Their openings are samples:
each is titled "Sample: ...", and every page carries a banner saying so.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LA = ZoneInfo("America/Los_Angeles")
ME_PHONE = "(310) 555-0199"  # the volunteer with a match, a chat and shifts
COORDINATOR = ("pat@demo.causematch.test", "Pat Doe, volunteer lead")
ADMIN = ("ada@demo.causematch.test", "Ada (Causematch admin)")
# the seed file's words -> the opening form's values (app/routes/org.py)
RECURRENCE = {
    "one-time": "one_time",
    "one_time": "one_time",
    "weekly": "weekly",
    "monthly": "monthly",
    "flexible": "flexible",
}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def refuse_production(env=os.environ):
    if env.get("CAUSEMATCH_ENV") == "production" or "K_SERVICE" in env:
        sys.exit(
            "demo.py is for local demos only: refusing to run with "
            "CAUSEMATCH_ENV=production or on Cloud Run (K_SERVICE)"
        )


def configure(port, db=None, seed_file=None):
    """The demo's environment. Runs before anything imports app, whose modules read it."""
    refuse_production()
    if seed_file and Path(seed_file).resolve().is_relative_to(ROOT):
        sys.exit(
            "--seed-file must live outside the repo: real org data never enters git"
        )
    if db and Path(db).exists():
        sys.exit(f"{db} exists: demo.py only ever fills a new database")
    db = (
        Path(db)
        if db
        else Path(tempfile.mkdtemp(prefix="causematch-demo-")) / "demo.db"
    )
    os.environ.update(
        CAUSEMATCH_ENV="development",
        CAUSEMATCH_DB=str(db),
        CAUSEMATCH_FAKE_SMS="1",
        CAUSEMATCH_FAKE_EMAIL="1",
        PUBLIC_BASE_URL=f"http://127.0.0.1:{port}",
    )
    os.environ.pop("CAUSEMATCH_SEED_DEMO", None)
    if seed_file:
        os.environ["CAUSEMATCH_DEMO_BANNER"] = "1"
    else:
        os.environ.pop("CAUSEMATCH_DEMO_BANNER", None)
    return db


def _clock(text):
    """(hour, minute) a free-text time starts at ("9:30am-noon" -> (9, 30)), or None."""
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|(?=\s*[-–]\s*\d))", text)
    if not m:
        return None
    hour, minute = int(m[1]), int(m[2] or 0)
    if m[3] == "pm" and hour < 12:
        hour += 12
    elif m[3] == "am" and hour == 12:
        hour = 0
    elif (
        not m[3] and 1 <= hour <= 6
    ):  # "5-7pm": a volunteer shift at 5 is in the evening
        hour += 12
    return (hour, minute) if hour < 24 and minute < 60 else None


def first_start(typical_time, i=0, today=None):
    """The next shift's start, "YYYY-MM-DDTHH:MM" in LA time, from a free-text time like
    "Saturdays 9:30am-noon", at least 2 days out; a weekend morning when it names no day
    and no time. i spreads the openings that fall back to that over the next weekends."""
    text = (typical_time or "").lower()
    today = today or datetime.now(LA).date()
    day = re.search(rf"\b({'|'.join(DAYS)})(?:[a-z]*day)?s?\b", text)
    clock = _clock(text)
    if day:
        weekday = DAYS.index(day[1])
        ahead = (weekday - today.weekday()) % 7 or 7
        date = today + timedelta(days=ahead + (7 if ahead < 2 else 0))
    elif "daily" in text:
        date = today + timedelta(days=2 + i % 4)
    else:
        weekday = 5 + i % 2  # Saturday, then Sunday
        ahead = (weekday - today.weekday()) % 7 or 7
        date = today + timedelta(
            days=ahead + (7 if ahead < 2 else 0) + 7 * (i // 2 % 3)
        )
    hour, minute = clock or (9 + i % 2, 0)
    return f"{date}T{hour:02d}:{minute:02d}"


def load_orgs(orgs):
    """Insert the seed file's nonprofits as verified orgs with sample openings."""
    from app.choices import SKILLS
    from app.db import Opening, Org, db, now, openings
    from app.db import orgs as org_table

    skills = {k for k, _ in SKILLS}
    i = 0
    with db.conn:
        for o in orgs:
            org = org_table.insert(
                Org(
                    name=o["name"],
                    ein=o.get("ein") or None,
                    verified_at=now(),
                    causes=json.dumps([o["cause"]]),
                    lat=o["lat"],
                    lon=o["lon"],
                    about=o.get("mission") or "",
                    intro="cold",
                    created_at=now(),
                    city="Los Angeles",
                    zip=o.get("zip") or "",
                    ntee_code=o.get("ntee_code") or "",
                )
            )
            for x in o.get("openings", []):
                openings.insert(
                    Opening(
                        org_id=org.id,
                        title=f"Sample: {x['title']}",
                        about=x.get("about") or "",
                        causes=json.dumps([o["cause"]]),
                        skills=json.dumps(sorted(set(x.get("skills", [])) & skills)),
                        lat=o["lat"],
                        lon=o["lon"],
                        neighborhood=o.get("neighborhood") or "",
                        remote=False,
                        starts_at=first_start(x.get("typical_time"), i),
                        recurrence=RECURRENCE.get(x.get("recurrence"), "flexible"),
                        slots=4 + i % 5,
                        orientation_required=x.get("orientation_required") or "no",
                        status="open",
                        notes="",
                        reply_channel="email",
                        adult_confirmed=True,
                        created_at=now(),
                    )
                )
                i += 1


def _volunteer(phone, name, hood, causes, skills, frequency):
    from app import data
    from app.auth import normalize_phone

    uid = data.get_or_create_volunteer(normalize_phone(phone))
    data.save_onboarding(
        uid,
        display_name=name,
        neighborhood=hood,
        lat=34.03,
        lon=-118.42,
        radius_km=60,
        causes=causes,
        availability=["weekend_morning", "weekday_evening"],
        skills=skills,
        frequency=frequency,
        heard_from="personal",
    )
    return uid


def _past_shift(match_id, coordinator, days_ago, status):
    from app.db import Shift, now, shifts

    when = (datetime.now(LA) - timedelta(days=days_ago)).strftime("%Y-%m-%dT09:00")
    marked = status != "scheduled"
    shifts.insert(
        Shift(
            match_id=match_id,
            starts_at=when,
            status=status,
            kind="added",
            created_by=coordinator,
            created_at=now(),
            marked_at=datetime.now(UTC).isoformat(timespec="seconds")
            if marked
            else None,
            marked_by=coordinator if marked else None,
        )
    )


def seed(seed_file=None):
    """Orgs and openings, then people: a coordinator and an admin, volunteers who liked
    the coordinator's opening, and ME_PHONE's volunteer with an accepted match (chat,
    upcoming and past shifts) and a pending like. Returns what a demo needs to sign in."""
    from app import data
    from app.db import ensure_causes, matches, openings, orgs, seed_demo

    ensure_causes()
    if seed_file:
        load_orgs(json.loads(Path(seed_file).read_text()))
    else:
        seed_demo()
    org = orgs()[0]
    coord = data.add_coordinator(COORDINATOR[0], org.id, name=COORDINATOR[1])
    data.add_admin(*ADMIN)
    first = next(
        o for o in openings(order_by="id") if o.org_id == org.id and o.status == "open"
    )

    others = [
        _volunteer(f"31055501{n:02d}", name, hood, [cause], skills, freq)
        for n, (name, hood, cause, skills, freq) in enumerate(
            [
                ("Maya", "Palms", "food-insecurity", ["physical"], "weekly"),
                ("Jordan", "Culver City", "environment-parks", ["car"], "monthly"),
                ("Priya", "Mar Vista", "animal-shelters", ["food_handling"], "once"),
            ]
        )
    ]
    for uid in others:
        data.swipe(uid, first.id, True)
    data.decide(
        coord, matches(where="volunteer_id = ?", where_args=[others[0]])[0].id, True
    )

    me = _volunteer(
        ME_PHONE,
        "Sam",
        "Sawtelle",
        ["food-insecurity", "animal-shelters", "environment-parks"],
        ["physical", "car"],
        "weekly",
    )
    m = data.swipe(me, first.id, True)
    data.decide(coord, m.id, True)
    data.send_message(
        coord, m.id, "Hi Sam! Thanks for signing up. We start at 9, side door."
    )
    data.send_message(me, m.id, "Great, see you there. Anything I should bring?")
    data.send_message(coord, m.id, "Closed-toe shoes. We have gloves and coffee.")
    _past_shift(m.id, coord, 14, "completed")
    _past_shift(m.id, coord, 2, "scheduled")  # for the coordinator to mark
    pending = next(
        (
            o
            for o in openings(order_by="id")
            if "animal-shelters" in o.causes and o.org_id != org.id
        ),
        None,
    )
    if pending:
        data.swipe(me, pending.id, True)
    return {"org": org.name, "match_id": m.id, "coordinator_id": coord}


def sign_in_link(email):
    """A fresh emailed sign-in link (the fake emailer keeps it): one use, 15 minutes."""
    from app import data, notify

    uid = data.coordinator_by_email(email) or data.admin_by_email(email)
    notify.send_login_link(email, data.create_login_token(uid))
    return re.search(r"https?://\S+", notify.get_emailer().sent[-1][2])[0]


def serve(port):
    import uvicorn

    from main import UVICORN, app

    uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", **UVICORN)
    ).run()


def main(argv=None):
    refuse_production()
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=5077)
    p.add_argument(
        "--db", help="a new database file (default: one in a temporary folder)"
    )
    p.add_argument("--seed-file", help="nonprofits JSON, kept outside the repo")
    a = p.parse_args(argv)
    db = configure(a.port, a.db, a.seed_file)
    info = seed(a.seed_file)
    import main as _app  # noqa: F401  after seeding, so its startup seeds nothing more

    url = os.environ["PUBLIC_BASE_URL"]
    print(f"Causematch demo on {url}  (database {db})")
    print(f"  Volunteer: {url}/login  phone {ME_PHONE}, code 000000 (any number works)")
    for role, (email, _) in (("Coordinator", COORDINATOR), ("Admin", ADMIN)):
        print(f"  {role} ({email}): {sign_in_link(email)}")
    print(
        "  Links work once, for 15 minutes; for a new one, sign in at /org/login with"
    )
    print(f"  the address and the link prints below. Coordinator's org: {info['org']}")
    if a.seed_file:
        print("  Banner on: sample openings, not affiliated with these organizations")
    sys.stdout.flush()
    serve(a.port)


if __name__ == "__main__":
    main()
