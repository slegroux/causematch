import json
import os
from datetime import UTC, datetime
from pathlib import Path

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
    starts_at: str
    recurrence: str
    slots: int
    orientation_required: str
    status: str  # open|filled|closed
    created_at: str


class Profile:
    id: int
    role: str  # volunteer|coordinator|admin
    phone: str  # E.164
    email: str
    display_name: str
    adult_attested: bool
    lat: float  # private: never rendered
    lon: float  # private: never rendered
    radius_km: int
    causes: str  # JSON list of cause slugs
    skills: str  # JSON list
    availability: str  # JSON list
    frequency: str
    heard_from: str  # personal|second_degree|public|other
    other_causes: str
    onboarded_at: str
    created_at: str
    deleted_at: str


causes = db.create(Cause, pk="slug", transform=True)
orgs = db.create(Org, pk="id", transform=True)
orgs.create_index(["ein"], unique=True, if_not_exists=True)  # NULLs allowed
openings = db.create(Opening, pk="id", transform=True)
profiles = db.create(Profile, pk="id", transform=True)
profiles.create_index(["phone"], unique=True, if_not_exists=True)

CAUSES = [
    Cause("food-insecurity", "Food insecurity", "utensils", 1, True),
    Cause("environment-parks", "Environment & parks", "tree-pine", 2, True),
    Cause("animal-shelters", "Animal shelters", "paw-print", 3, True),
    Cause("homelessness-services", "Homelessness services", "house", 4, False),
]

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
        ["lifting"],
        "Sawtelle",
        34.0386,
        -118.4457,
        "2026-11-07T09:00",
        6,
        "open",
    ),
    (
        1,
        "Serve a weekend lunch line",
        ["food-insecurity"],
        ["cooking"],
        "Downtown LA",
        34.0407,
        -118.2468,
        "2026-11-08T11:00",
        4,
        "open",
    ),
    (
        2,
        "Trail cleanup along the arroyo",
        ["environment-parks"],
        ["outdoors"],
        "Highland Park",
        34.1114,
        -118.1925,
        "2026-11-14T08:30",
        10,
        "open",
    ),
    (
        3,
        "Walk dogs at the shelter",
        ["animal-shelters"],
        ["animal care"],
        "Boyle Heights",
        34.0335,
        -118.2087,
        "2026-11-09T10:00",
        5,
        "open",
    ),
    (
        5,
        "Community garden planting day",
        ["environment-parks", "food-insecurity"],
        ["gardening"],
        "Baldwin Hills",
        34.0100,
        -118.3500,
        "2026-11-15T09:30",
        8,
        "open",
    ),
    (
        3,
        "Foster-home photo day for adoptable cats",
        ["animal-shelters"],
        ["photography"],
        "Echo Park",
        34.0782,
        -118.2606,
        "2026-11-21T13:00",
        2,
        "open",
    ),
    (
        4,
        "Pack hygiene kits for outreach",
        ["homelessness-services"],
        ["packing"],
        "Van Nuys",
        34.1867,
        -118.4486,
        "2026-11-10T14:00",
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
        "2026-11-01T09:00",
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
