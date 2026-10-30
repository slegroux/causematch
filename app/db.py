import json
from datetime import UTC, datetime, timedelta

from fastlite import database

from app import migrations
from app.config import db_path, is_dev
from app.match import LA

DB_PATH = db_path()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
db = database(DB_PATH)
# An early build installed this trigger. It names the shift table, so any later transform
# of that table fails half-way and strands its rows. Must run before the first create.
db.execute("DROP TRIGGER IF EXISTS shift_with_match")
# Before the tables below: a migration (a rename, say) must reshape an existing table before
# transform=True compares it with its class.
migrations.migrate(db.conn)


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
    deactivated_at: str  # UTC; an admin closed this org after a report
    city: str  # ProPublica record, set by scripts/import_orgs.py
    zip: str  # five digits
    ntee_code: str
    logo_key: str  # Photo (photos.process "logo"): a square badge on its own background
    photo_key: str  # Photo ("card"): a picture of the org's work, the cards' fallback
    website: str  # https only (forms.https_url)


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
    # Kind of work, all optional ("" or NULL: not said)
    setting: str  # indoor|outdoor
    effort: str  # light|moderate|active
    group_size: str  # solo|small|big
    first_timer_ok: bool
    languages: str  # JSON list of choices.LANGUAGES keys that help
    photo_key: str  # Photo ("card"), shown on the swipe card before the org's
    event_url: str  # https only (forms.https_url): the event as the org advertises it
    duration_minutes: (
        int  # how long a shift lasts (choices.SHIFT_LENGTHS); NULL: not said
    )


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
    # JSON list of day slots, "mon_morning" ... "sun_evening"
    availability: str
    frequency: str
    heard_from: str  # personal|second_degree|public|other
    other_causes: str
    onboarded_at: str
    created_at: str
    deleted_at: str
    suspended_at: str  # UTC; an admin suspended this account: it cannot sign in
    sms_opted_out: bool  # the number replied STOP: never text it again
    # Match preferences (app/prefs.py), private: nonprofits never see them.
    cause_prefs: str  # JSON {slug: love|open|no}; `causes` keeps love + open
    free_dates: str  # JSON ["YYYY-MM-DD"], up to 10, future only (pruned when read)
    horizon_days: int  # 7|14|31; 0 or NULL: any time
    commitment: str  # one_time|recurring|either (NULL: either)
    setting: str  # indoor|outdoor; NULL or "either": no preference
    effort: str  # light|moderate|active; NULL or "any"
    group_size: str  # solo|small|big; NULL or "any"
    first_timer: bool  # prefers first-timer friendly openings
    orientation_ok: bool  # False hides openings with a separate orientation; NULL: True
    languages: str  # JSON list of choices.LANGUAGES keys
    dealbreakers: (
        str  # JSON subset of choices.DEALBREAKERS: these filter; NULL: distance
    )
    surprise: bool  # about 1 card in 8 from outside the loved causes
    photo_key: str  # volunteers: their Photo, shown as app/routes/photos.py allows
    last_name: (
        str  # volunteers, optional: nonprofits see its initial, all of it once accepted
    )
    pronouns: str  # optional, free text (choices.PRONOUNS are suggestions)
    bio: str  # optional, up to choices.MAX_BIO characters
    interests: str  # JSON list of choices.INTERESTS values
    photo_nudge_dismissed_at: str  # UTC; "Not now" on the deck's "Add a photo"
    matches_seen_at: str  # UTC; their last look at Matches (the tab's dot: news since)
    match_moment_seen_at: str  # UTC; decided_at of the newest match its sheet showed
    # Last-minute calls, private (app/prefs.py urgent): off unless turned on
    urgent_on: bool
    urgent_window: str  # today|48h (NULL: 48h)
    urgent_quiet_from: int  # local hour no notice is sent from (NULL: 21)
    urgent_quiet_to: int  # ... until (NULL: 8); from > to wraps past midnight
    urgent_per_week: int  # 1..3 (NULL: 3)
    # Up to this many shifts a week (app/prefs.py weekly_limit), 1..5; NULL or 0: off
    weekly_limit: int


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


class ClaimRequest:
    id: int
    ein: str  # nine digits as typed; may match no org
    org_name: str  # as typed
    name: str
    email: str
    phone: str  # E.164 or empty
    status: str  # pending|approved|rejected
    org_id: int  # set on approval
    created_at: str
    decided_at: str
    decided_by: int


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
    org_blind: bool  # liked after the org blocked this volunteer: the org never sees it
    urgent_call_id: (
        int  # a like on an open UrgentCall's card: it counts toward that call
    )


class Message:
    id: int
    match_id: int
    sender_id: int
    body: str  # plain text, 1-2000 characters
    created_at: str


class Shift:
    id: int
    match_id: int
    # America/Los_Angeles wall clock, like Opening.starts_at. In the fall-back hour the
    # strings are ambiguous and not monotonic; a 01:30 shift may read "not started" for 15
    # minutes after it first did.
    starts_at: str
    status: str  # scheduled|completed|no_show|cancelled
    kind: str  # accept (made when the match was accepted) | added (by a coordinator)
    created_by: int  # the coordinator who accepted or added it
    marked_at: str  # UTC; when a coordinator first marked it
    marked_by: int
    corrected_at: str  # UTC; the one correction allowed within 7 days of marked_at
    corrected_by: int
    cancelled_at: str  # UTC; set when a coordinator or the volunteer cancels it (a withdraw leaves it null)
    cancelled_by: int  # the coordinator, or the volunteer ("Can't make this one")
    created_at: str
    confirmed_at: str  # UTC; the volunteer answered "Still coming?" with yes
    duration_minutes: int  # copied from the opening; NULL: no length known
    # The hours a coordinator confirmed when marking it attended (choices.SHIFT_HOURS);
    # NULL: not attended, or attended with the hours unknown
    confirmed_minutes: int


class Notification:
    id: int
    kind: str  # accepted|message|likes|reminder|urgent|auto|cancel|withdrawn
    key: str  # unique per logical event, so a repeat or a throttled send is a no-op
    recipient_id: int  # the volunteer's profile id; for "likes", "auto", "cancel" and "withdrawn", the org id
    sent_at: str  # UTC; when this stream's last send was claimed
    status: str  # pending|sent|failed|skipped|cancelled|deferred (the SMS ceiling)|unknown (a timeout: may have been delivered)
    attempts: (
        int  # tries of the current send, capped so a dead number is not retried forever
    )


class Report:
    id: int
    reporter_id: int
    target_user_id: int  # set when a coordinator reported a volunteer
    target_org_id: int  # set when a volunteer reported an org
    match_id: int
    reason: str  # harassment|no_show_pattern|unsafe|inappropriate_profile|spam|other
    details: str  # up to 1000 characters
    status: str  # open|dismissed|actioned
    created_at: str
    resolved_by: int
    resolved_at: str


class Block:
    id: int
    blocker_id: int  # the volunteer, or the coordinator who blocked for their org
    blocked_user_id: int  # set when a coordinator blocked a volunteer
    blocked_org_id: int  # the org blocked by a volunteer; for a coordinator's block, the org it covers
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
matches.create_index(
    ["volunteer_id", "status"], if_not_exists=True
)  # a volunteer's own
messages = db.create(Message, pk="id", transform=True)
messages.create_index(["match_id", "created_at"], if_not_exists=True)
shifts = db.create(Shift, pk="id", transform=True)
notifications = db.create(Notification, pk="id", transform=True)
notifications.create_index(["key"], unique=True, if_not_exists=True)


class RetainedPhoneHash:
    """A keyed hash of a deleted account's phone, kept only when a block, a report or a
    suspension must survive deleting the account. Never the number itself."""

    hash: str  # HMAC-SHA256(PHONE_HASH_KEY, E.164), hex
    profile_id: int  # the deleted profile this was taken from
    reason: str
    blocks_json: str  # JSON [[blocker_id, org_id], ...] to re-apply on sign-up
    suspended: bool
    created_at: str


reports = db.create(Report, pk="id", transform=True)
retained_phones = db.create(RetainedPhoneHash, pk="hash", transform=True)
blocks = db.create(Block, pk="id", transform=True)
claim_requests = db.create(ClaimRequest, pk="id", transform=True)


class RateEvent:
    """One counted action (a code sent, a link sent, ...) for app/security.py's limits."""

    id: int
    key: str  # limit name + HMAC of the phone, email or IP: never the value itself
    at: float  # unix seconds


rate_events = db.create(RateEvent, pk="id", transform=True)
rate_events.create_index(["key", "at"], if_not_exists=True)


class DeckEmpty:
    """One view of an empty deck, for the pilot metrics (app/metrics_rows.py)."""

    id: int
    volunteer_id: int
    today: bool  # with "Free in the next 48h" on
    at: str  # UTC


deck_empties = db.create(DeckEmpty, pk="id", transform=True)


class Place:
    """Where a volunteer helps from (up to 2), private: never shown to anyone else. A ZIP or
    neighborhood centroid, or a device location rounded to 2 decimals; never an address."""

    id: int
    user_id: int
    label: str  # Home | Work | the volunteer's own, up to 20 characters
    lat: float
    lon: float
    area: str  # shown back to the volunteer only: "Mar Vista", "90066", "My location"
    mode: str  # car|transit|bike|walk (app/match.py MODE_KMH)
    max_minutes: int  # 0: any distance


places = db.create(Place, pk="id", transform=True)
places.create_index(["user_id"], if_not_exists=True)


class Photo:
    """A picture app/photos.py re-encoded. Bytes live here, so Litestream's copy of the
    database is their backup too (about 70 KB a volunteer)."""

    key: str  # 22 random URL-safe characters (photos.new_key); its URL
    owner_kind: str  # volunteer | org_logo | org_photo | opening
    owner_id: int
    full: bytes  # JPEG with no metadata, sized by its kind (photos.process)
    thumb: bytes  # JPEG, 160 px square
    sha256: str  # of full: the ETag
    created_at: str


photos = db.create(Photo, pk="key", transform=True)


class UrgentCall:
    """A nonprofit short of people for a shift in the next 48 hours: "need 2 more for
    tomorrow 9 am" on one of its openings (app/data.py post_urgent_call). Likes made while
    it is open carry its id (Match.urgent_call_id); it fills at `needed` accepted ones."""

    id: int
    opening_id: int
    org_id: int
    starts_at: (
        str  # the shift, LA wall clock, naive ISO minutes (like Opening.starts_at)
    )
    needed: int  # how many more people
    auto_accept: bool  # the first `needed` likes are accepted without a decision
    status: (
        str  # open|filled|closed (closed by the org, expired, or its opening closed)
    )
    created_by: int  # the coordinator
    created_at: str  # UTC
    ended_at: str  # UTC, when it stopped being open
    waves: int  # notification waves sent (app/notify.py send_urgent_waves), up to 2
    wave_at: str  # UTC, when the last wave went out
    refill: (
        bool  # posted by a volunteer's late cancel (app/data.py _refill), not the org
    )


urgent_calls = db.create(UrgentCall, pk="id", transform=True)
urgent_calls.create_index(["org_id", "created_at"], if_not_exists=True)
# One volunteer-side block per volunteer and org; one coordinator-side block per volunteer and
# org however many coordinators of it pressed the button.
db.execute("DROP INDEX IF EXISTS block_once")
db.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS block_once_v2 ON block"
    "(coalesce(blocked_user_id, 0), coalesce(blocked_org_id, 0), "
    "CASE WHEN blocked_user_id IS NULL THEN blocker_id ELSE 0 END)"
)
# One live shift per match and start; a cancelled one frees its time.
db.execute("DROP INDEX IF EXISTS idx_shift_match_id_starts_at")
db.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS shift_match_start_live "
    "ON shift(match_id, starts_at) WHERE status != 'cancelled'"
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
db.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS admin_email_live ON profile(lower(email)) "
    "WHERE role = 'admin' AND deleted_at IS NULL"
)

CAUSES = [
    Cause("food-insecurity", "Food insecurity", "utensils", 1, True),
    Cause("environment-parks", "Environment & parks", "tree-pine", 2, True),
    Cause("animal-shelters", "Animal shelters", "paw-print", 3, True),
    Cause("homelessness-services", "Homelessness services", "house", 4, True),
]


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

# (org index, title, causes, skills, neighborhood, lat, lon, starts_at, slots, minutes, status)
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
        180,
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
        180,
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
        240,
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
        120,
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
        240,
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
        120,
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
        180,
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
        180,
        "closed",
    ),
]

# title -> (about, notes) of each invented opening: what the card and its sheet show
ABOUT = {
    "Sort and pack pantry boxes": (
        (
            "Sort donated cans and fresh produce, then pack boxes for families picking up "
            "that afternoon."
        ),
        "Closed-toe shoes. We have gloves and coffee.",
    ),
    "Serve a weekend lunch line": (
        (
            "Plate hot lunches and hand them out to about 200 guests. A cook shows you the "
            "line first."
        ),
        "Hair tied back; we give you an apron and a hairnet.",
    ),
    "Trail cleanup along the arroyo": (
        "Pick up litter and pull invasive weeds along a mile of trail, in small teams.",
        "Bring water and a hat. Parking at the Avenue 43 lot.",
    ),
    "Walk dogs at the shelter": (
        "Take shelter dogs on 20-minute walks so they are calmer when families visit.",
        "A 15-minute safety talk the first time.",
    ),
    "Community garden planting day": (
        "Turn the beds, plant winter vegetables and mulch the paths with neighbors.",
        "",
    ),
    "Foster-home photo day for adoptable cats": (
        (
            "Take bright, friendly photos of cats in foster homes for their adoption pages. "
            "A phone camera is enough."
        ),
        "",
    ),
    "Pack hygiene kits for outreach": (
        (
            "Pack soap, socks and toothbrushes into kits the outreach team hands out that "
            "week."
        ),
        "",
    ),
    "Holiday pantry drive (past sign-up deadline)": (
        "Collect and sort holiday food donations at the pantry.",
        "",
    ),
}


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
                verified_at=now() if is_dev() else None,
            )
        )
        ids.append(org.id)
    for (
        oi,
        title,
        cs,
        skills,
        hood,
        lat,
        lon,
        starts,
        slots,
        minutes,
        status,
    ) in OPENINGS:
        openings.insert(
            Opening(
                org_id=ids[oi],
                title=title,
                about=ABOUT[title][0],
                notes=ABOUT[title][1],
                causes=json.dumps(cs),
                skills=json.dumps(skills),
                lat=lat,
                lon=lon,
                neighborhood=hood,
                remote=False,
                starts_at=starts,
                duration_minutes=minutes,
                recurrence="",
                slots=slots,
                orientation_required="",
                status=status,
                created_at=now(),
            )
        )
