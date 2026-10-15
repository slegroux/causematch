import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastlite import database

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(
    os.environ.get("CAUSEMATCH_DB") or ROOT / "data" / "causematch.db"
).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
db = database(DB_PATH)


class Cause:
    slug: str
    label: str
    icon: str
    sort: int
    active: bool


class Org:
    id: int
    name: str
    ein: str
    verified_at: str
    causes: str  # JSON list of cause slugs
    lat: float
    lon: float
    about: str
    intro: str  # warm|cold
    created_at: str


class Opening:
    id: int
    org_id: int
    title: str
    about: str
    causes: str  # JSON list of cause slugs
    skills: str  # JSON list
    lat: float
    lon: float
    neighborhood: str
    remote: bool
    starts_at: (
        str  # America/Los_Angeles wall clock, naive ISO minutes (created_at is UTC)
    )
    recurrence: str
    slots: int
    orientation_required: str
    status: str  # open|filled|closed
    notes: str
    reply_channel: str  # email|text|either
    adult_confirmed: bool  # 18+ and no one-to-one contact with vulnerable people
    created_at: str


class Profile:
    id: int
    role: str  # volunteer|coordinator|admin
    phone: str  # E.164
    email: str
    display_name: str  # volunteer first name, or a coordinator's name and role
    contact_phone: str  # coordinators only, E.164; never a sign-in phone
    adult_attested: bool
    session_version: int  # bump to sign every device out
    lat: float  # private: never rendered
    lon: float  # private: never rendered
    neighborhood: str
    radius_km: int
    causes: str  # JSON list of cause slugs
    skills: str  # JSON list
    language_other: str
    volunteered_last_year: str  # yes|no
    availability: str  # JSON list
    frequency: str
    heard_from: str  # personal|second_degree|public|other
    other_causes: str
    onboarded_at: str
    created_at: str
    deleted_at: str


class OrgMember:
    id: int
    org_id: int
    user_id: int
    role: str  # owner|coordinator


class LoginToken:
    token_hash: str  # SHA-256 of the emailed token; the token itself is never stored
    user_id: int
    expires_at: str
    used_at: str


class Swipe:
    volunteer_id: int
    opening_id: int
    liked: bool
    created_at: str


class Match:
    id: int
    opening_id: int
    volunteer_id: int
    status: str  # pending|accepted|declined|withdrawn
    created_at: str
    decided_at: str
    decided_by: int
    withdrawn_at: str


class Message:
    id: int
    match_id: int
    sender_id: int
    body: str  # plain text, 1-2000 characters
    created_at: str


class Shift:
    id: int
    match_id: int
    starts_at: str  # America/Los_Angeles wall clock, like Opening.starts_at
    status: str  # scheduled|completed|no_show|cancelled
    marked_at: str  # UTC; when a coordinator first marked it
    marked_by: int
    corrected_at: str  # UTC; the one correction allowed within 7 days of marked_at
    created_at: str


causes = db.create(Cause, pk="slug", transform=True)
orgs = db.create(Org, pk="id", transform=True)
orgs.create_index(["ein"], unique=True, if_not_exists=True)  # NULLs allowed
openings = db.create(Opening, pk="id", transform=True)
profiles = db.create(Profile, pk="id", transform=True)
org_members = db.create(OrgMember, pk="id", transform=True)
org_members.create_index(["org_id", "user_id"], unique=True, if_not_exists=True)
login_tokens = db.create(LoginToken, pk="token_hash", transform=True)
swipes = db.create(Swipe, pk=["volunteer_id", "opening_id"], transform=True)
matches = db.create(Match, pk="id", transform=True)
matches.create_index(["opening_id", "volunteer_id"], unique=True, if_not_exists=True)
messages = db.create(Message, pk="id", transform=True)
messages.create_index(["match_id", "created_at"], if_not_exists=True)
shifts = db.create(Shift, pk="id", transform=True)
shifts.create_index(["match_id", "starts_at"], unique=True, if_not_exists=True)
# A shift never outlives its match (and a reused match id never inherits old shifts).
db.execute(
    "CREATE TRIGGER IF NOT EXISTS shift_with_match AFTER DELETE ON match "
    "BEGIN DELETE FROM shift WHERE match_id = OLD.id; END"
)
# One live profile per phone; a soft-deleted profile frees its number.
db.execute("DROP INDEX IF EXISTS idx_profile_phone")
db.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS profile_phone_live ON profile(phone) WHERE deleted_at IS NULL"
)
# One live coordinator per email, however the address is cased.
db.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS coordinator_email_live ON profile(lower(email)) "
    "WHERE role = 'coordinator' AND deleted_at IS NULL"
)

CAUSES = [
    Cause("food-insecurity", "Food insecurity", "utensils", 1, True),
    Cause("environment-parks", "Environment & parks", "tree-pine", 2, True),
    Cause("animal-shelters", "Animal shelters", "paw-print", 3, True),
    Cause("homelessness-services", "Homelessness services", "house", 4, False),
]

LA = ZoneInfo("America/Los_Angeles")


def _soon(days, hhmm):
    """Local wall-clock start `days` from today, so demo openings never go stale."""
    return f"{(datetime.now(LA) + timedelta(days=days)).date()}T{hhmm}"


# All names, places and openings below are invented.
ORGS = [
    ("Westside Pantry Collective", ["food-insecurity"], 34.0522, -118.4437),
    (
        "Downtown Harvest Kitchen",
        ["food-insecurity", "homelessness-services"],
        34.0407,
        -118.2468,
    ),
    ("Arroyo Trail Keepers", ["environment-parks"], 34.1478, -118.1445),
    ("Eastside Paws Rescue", ["animal-shelters"], 34.0195, -118.1900),
    ("Valley Street Outreach", ["homelessness-services"], 34.1867, -118.4486),
    (
        "Baldwin Green Spaces",
        ["environment-parks", "food-insecurity"],
        34.0100,
        -118.3500,
    ),
]

# (org index, title, causes, skills, neighborhood, lat, lon, starts_at, slots, status)
OPENINGS = [
    (
        0,
        "Sort and pack pantry boxes",
        ["food-insecurity"],
        ["physical"],
        "Sawtelle",
        34.0386,
        -118.4457,
        _soon(7, "09:00"),
        6,
        "open",
    ),
    (
        1,
        "Serve a weekend lunch line",
        ["food-insecurity"],
        ["food_handling"],
        "Downtown LA",
        34.0407,
        -118.2468,
        _soon(8, "11:00"),
        4,
        "open",
    ),
    (
        2,
        "Trail cleanup along the arroyo",
        ["environment-parks"],
        ["physical"],
        "Highland Park",
        34.1114,
        -118.1925,
        _soon(14, "08:30"),
        10,
        "open",
    ),
    (
        3,
        "Walk dogs at the shelter",
        ["animal-shelters"],
        ["animals"],
        "Boyle Heights",
        34.0335,
        -118.2087,
        _soon(9, "10:00"),
        5,
        "open",
    ),
    (
        5,
        "Community garden planting day",
        ["environment-parks", "food-insecurity"],
        ["physical"],
        "Baldwin Hills",
        34.0100,
        -118.3500,
        _soon(15, "09:30"),
        8,
        "open",
    ),
    (
        3,
        "Foster-home photo day for adoptable cats",
        ["animal-shelters"],
        ["media"],
        "Echo Park",
        34.0782,
        -118.2606,
        _soon(21, "13:00"),
        2,
        "open",
    ),
    (
        4,
        "Pack hygiene kits for outreach",
        ["homelessness-services"],
        ["physical"],
        "Van Nuys",
        34.1867,
        -118.4486,
        _soon(10, "14:00"),
        6,
        "open",
    ),
    (
        0,
        "Holiday pantry drive (past sign-up deadline)",
        ["food-insecurity"],
        [],
        "Sawtelle",
        34.0386,
        -118.4457,
        _soon(1, "09:00"),
        0,
        "closed",
    ),
]


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def ensure_causes():
    """Insert any cause slug the table lacks; never overwrites an edited row."""
    have = {c.slug for c in causes()}
    for c in CAUSES:
        if c.slug not in have:
            causes.insert(c)


def seed_demo():
    """Invented orgs and openings, in one transaction; only into an empty DB."""
    if orgs.count or openings.count:
        return
    with db.conn:
        _seed_demo()


def _seed_demo():
    ids = []
    for name, cs, lat, lon in ORGS:
        org = orgs.insert(
            Org(
                name=name,
                causes=json.dumps(cs),
                lat=lat,
                lon=lon,
                about="An invented organization.",
                intro="cold",
                created_at=now(),
            )
        )
        ids.append(org.id)
    for oi, title, cs, skills, hood, lat, lon, starts, slots, status in OPENINGS:
        openings.insert(
            Opening(
                org_id=ids[oi],
                title=title,
                about="",
                causes=json.dumps(cs),
                skills=json.dumps(skills),
                lat=lat,
                lon=lon,
                neighborhood=hood,
                remote=False,
                starts_at=starts,
                recurrence="",
                slots=slots,
                orientation_required="",
                status=status,
                created_at=now(),
            )
        )
