"""The demo's data: invented or real nonprofits with sample openings, and the people a demo
signs in as. scripts/demo.py and scripts/screenshots.py seed a local throwaway database
with it, and a preview (config.is_preview) seeds itself at startup (seed_preview).

Imports nothing from app at module level: scripts/demo.py sets the environment app.db
reads after importing this.

A seed file is a list of {name, ein, ntee_code, cause, website, mission, neighborhood,
zip, lat, lon, volunteer_url, logo_url, logo_file, photo_file, openings: [{title, about,
skills, typical_time, recurrence, orientation_required, source_url, photo_file}]}. Their
openings are samples: each is titled "Sample: ...". logo_file names a file in logos-raw/
and photo_file one in photos-raw/, both next to the seed file; each opening links to its
source_url, else the org's volunteer_url.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("causematch")

LA = ZoneInfo("America/Los_Angeles")
ME_PHONE = "(310) 555-0100"  # Sam, with matches, a chat and shifts (demo_volunteer)
NO_PHOTO_PHONE = "(310) 555-0103"  # Priya, who has no photo yet
COORDINATOR = ("pat@demo.causematch.test", "Pat Doe, volunteer lead")
# the stand-in coordinators of the other orgs (data.DEMO_TEAM_DOMAIN), by turns
TEAM_NAMES = ("Rosa", "Alex", "Jamie", "Noor", "Luis", "Kim", "Theo", "Dana")
ADMIN = ("ada@demo.causematch.test", "Ada (Causematch admin)")
SOON_TIMES = ("10:00", "13:00", "16:00", "18:30")  # tomorrow's shifts (_soon_shifts)
# the seed file's words -> the opening form's values (app/routes/org.py)
RECURRENCE = {
    "one-time": "one_time",
    "one_time": "one_time",
    "weekly": "weekly",
    "monthly": "monthly",
    "flexible": "flexible",
}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# A time, maybe the start of a range: "9:30am", "5-7pm", "10am-noon", "12-2pm". The hour
# is not part of a decimal ("1.5-2 hour slots"), and a range followed by a unit is a
# duration ("2-3 hours"), not a time.
TIME = re.compile(
    r"(?<![\d.:])(\d{1,2})(?::(\d{2}))?(?![\d.])\s*(am|pm)?"
    r"(?:\s*[-–]\s*(?:(\d{1,2})(?::\d{2})?\s*(am|pm)?|(noon)))?"
    r"(?!\s*(?:hours?|hrs?|minutes?|mins?)\b)(?!-?\s*hour)"
)


def _clock(text):
    """(hour, minute) a free-text time starts at ("9:30am-noon" -> (9, 30)), or None.

    A start without am/pm takes the range end's: "7-9pm" -> 19:00, "11-1pm" -> 11:00. A
    bare range with neither ("5-7") reads 1 to 6 as afternoon: volunteer shifts at 5 are
    in the evening. A lone number with neither am/pm nor minutes is not a time."""
    for m in TIME.finditer(text):
        hour, minute, own, end, end_m, noon = (
            int(m[1]),
            int(m[2] or 0),
            m[3],
            m[4],
            m[5],
            m[6],
        )
        if not (own or m[2] or end or noon):
            continue
        if own:
            hour = hour % 12 + (12 if own == "pm" else 0)
        elif noon or end_m == "pm":
            end_hour = 12 if noon else int(end) % 12 + 12
            if hour < 12 and hour + 12 <= end_hour:
                hour += 12
        elif not end_m and 1 <= hour <= 6:
            hour += 12
        return (hour, minute) if hour < 24 and minute < 60 else None
    return None


def first_start(typical_time, i=0, today=None):
    """The next shift's start, "YYYY-MM-DDTHH:MM" in LA time, from a free-text time like
    "Saturdays 9:30am-noon", at least 2 days out; a weekend morning when it names no day
    and no time. i spreads the openings that fall back to that over the next weekends."""
    text = (typical_time or "").lower()
    today = today or datetime.now(LA).date()
    day = re.search(
        r"\b(mon|tues?|weds?|thu|thurs?|fri|sat|sun)(?:[a-z]*day)?s?\b", text
    )
    clock = _clock(text)
    if day:
        weekday = DAYS.index(day[1][:3])
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


# A sample opening's kind of work, guessed from its cause (the seed file doesn't say):
# (setting, effort, group size). A demo stand-in; real nonprofits fill these in.
WORK_BY_CAUSE = {
    "environment-parks": ("outdoor", "active", "big"),
    "food-insecurity": ("indoor", "moderate", "small"),
    "animal-shelters": ("", "moderate", "small"),
    "homelessness-services": ("indoor", "moderate", "big"),
}


def _sample_work(cause, i):
    """The sample work fields of the i-th opening, whose cause is `cause`."""
    setting, effort, group = WORK_BY_CAUSE.get(cause, ("", "", ""))
    return {
        "setting": setting,
        "effort": effort,
        "group_size": group,
        "first_timer_ok": i % 2 == 0,
        "languages": json.dumps(["es"] if i % 3 == 0 else []),
    }


def picture(folder, name, kind):
    """photos.process of folder/name (one of the seed's downloaded files), or None.

    Those folders hold untrusted downloads: a file is only ever read as bytes and decoded
    by Pillow, which re-encodes it. An SVG (Pillow can't read one) or anything else it
    can't read is skipped, and the card falls back to the logo or the cause art."""
    from app import photos

    if not (folder and name):
        return None
    path = folder / Path(name).name
    if not path.is_file() or path.is_symlink():
        print(f"  skipped {path.name}: no such file", file=sys.stderr)
        return None
    try:
        return photos.process(path.read_bytes(), kind)
    except photos.BadPhoto:
        print(f"  skipped {path.name}: not a picture Pillow reads", file=sys.stderr)
        return None


def load_orgs(orgs, media=None):
    """Insert the seed file's nonprofits as verified orgs with sample openings, with
    their logos and photos when `media` (the seed file's folder) holds them."""
    from app import data
    from app.choices import SKILLS
    from app.db import Opening, Org, db, now, openings
    from app.db import orgs as org_table
    from app.forms import https_url

    logos = media / "logos-raw" if media else None
    shots = media / "photos-raw" if media else None
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
                    website=https_url(o.get("website")),
                )
            )
            for column, owner, kind, folder, name in (
                ("logo_key", "org_logo", "logo", logos, o.get("logo_file")),
                ("photo_key", "org_photo", "card", shots, o.get("photo_file")),
            ):
                if p := picture(folder, name, kind):
                    key = data.store_photo(owner, org.id, p)
                    org_table.update(id=org.id, **{column: key})
            for x in o.get("openings", []):
                p = picture(shots, x.get("photo_file"), "card")
                opening = openings.insert(
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
                        **_sample_work(o["cause"], i),
                        event_url=https_url(x.get("source_url"))
                        or https_url(o.get("volunteer_url")),
                    )
                )
                if p:
                    key = data.store_photo("opening", opening.id, p)
                    openings.update(id=opening.id, photo_key=key)
                i += 1


def _volunteer(phone, name, hood, causes, skills, frequency):
    from app import data
    from app.auth import normalize_phone

    uid = data.get_or_create_volunteer(normalize_phone(phone))
    return _onboard(uid, name, hood, causes, skills, frequency)


def _onboard(uid, name, hood, causes, skills, frequency):
    from app import data

    data.save_onboarding(
        uid,
        display_name=name,
        neighborhood=hood,
        lat=34.03,
        lon=-118.42,
        radius_km=60,
        causes=causes,
        # day slots and a Home place, as onboarding saves them
        availability=["sat_morning", "sun_morning", "tue_evening", "thu_evening"],
        skills=skills,
        frequency=frequency,
        heard_from="personal",
        place={
            "label": "Home",
            "lat": 34.03,
            "lon": -118.42,
            "area": hood,
            "mode": "car",
            "max_minutes": 0,  # any distance: every demo opening is in reach
        },
    )
    return uid


def _fill_invented_openings():
    """The invented seed's openings, given what real ones carry: kind of work, a sample
    event link (example.org) and a language now and then, so their cards show it all."""
    from app.db import openings

    for i, o in enumerate(openings(order_by="id")):
        cause = (json.loads(o.causes or "[]") or [""])[0]
        openings.update(
            id=o.id,
            **_sample_work(cause, i),
            event_url=f"https://example.org/volunteer/{o.id}",
        )


def _preferences(uid):
    """ME_PHONE's match preferences: two places (Home in Sawtelle by car, Work Downtown by
    transit), three-state causes, a day grid and a free date, so /me/preferences and the
    cards show them."""
    from app import data
    from app.geo import neighborhood_latlon, zip_latlon

    home = zip_latlon("90025")  # Sawtelle
    work = neighborhood_latlon("Downtown")[1]
    data.save_preferences(
        uid,
        causes={
            "food-insecurity": "love",
            "animal-shelters": "love",
            "environment-parks": "open",
        },
        availability=[
            "tue_evening",
            "thu_evening",
            "sat_morning",
            "sat_afternoon",
            "sun_morning",
        ],
        # tomorrow too: free for the urgent call (_urgent_call)
        free_dates=[
            (datetime.now(LA) + timedelta(days=d)).date().isoformat() for d in (1, 10)
        ],
        horizon_days=31,
        commitment="either",
        setting="either",
        effort="active",
        first_timer=True,
        languages=["es"],
        skills=["physical", "car"],
        frequency="weekly",
        # quiet only 2 to 5 am, so a demo at any usual hour shows the wave
        urgent={"on": True, "window": "48h", "quiet_from": 2, "quiet_to": 5,
                "per_week": 3},
        places=[
            {"label": "Home", "lat": home[0], "lon": home[1], "area": "Sawtelle",
             "mode": "car", "max_minutes": 45},
            {"label": "Work", "lat": work[0], "lon": work[1], "area": "Downtown",
             "mode": "transit", "max_minutes": 30},
        ],
    )  # fmt: skip


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
            marked_at=now() if marked else None,
            marked_by=coordinator if marked else None,
        )
    )


def drawn_photo(seed=0):
    """A JPEG drawn here (a sun over hills), for demo profiles: never a real person."""
    import io

    from PIL import Image, ImageDraw

    sky = [(255, 214, 170), (190, 225, 240), (250, 200, 210)][seed % 3]
    im = Image.new("RGB", (900, 900), sky)
    d = ImageDraw.Draw(im)
    d.ellipse((520, 140, 760, 380), fill=(255, 170, 60))
    d.ellipse((-200, 520, 700, 1300), fill=(90, 160, 100))
    d.ellipse((300, 600, 1200, 1400), fill=(60, 130, 80))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def seed(seed_file=None):
    """Orgs and openings, then people: a coordinator and an admin, a stand-in coordinator
    for every other org (_teams), volunteers who liked the coordinator's opening, and
    ME_PHONE's volunteer with an accepted match (chat, upcoming and past shifts), another
    upcoming shift and a waiting like; then the coordinator's urgent call for tomorrow
    9 am, the first card in ME_PHONE's deck, and its first wave (the fake SMS log shows
    ME_PHONE's text). Returns what a demo needs to sign in.

    With config.demo_matches on, ME_PHONE signs each visitor in as a fresh copy of Sam
    instead (demo_volunteer); this one stays for the coordinator's inbox and chat."""
    from app import data, photos
    from app.auth import normalize_phone
    from app.db import ensure_causes, matches, now, openings, orgs, seed_demo

    ensure_causes()
    if seed_file:
        path = Path(seed_file)
        load_orgs(json.loads(path.read_text()), path.resolve().parent)
    else:
        seed_demo()
        for o in orgs():  # app/db.py verifies them in development only; a preview's too
            orgs.update(id=o.id, verified_at=o.verified_at or now())
        _fill_invented_openings()
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
            ],
            start=1,  # (310) 555-0101 to 0103: 0100 is ME_PHONE
        )
    ]
    for n, (uid, last, pronouns, bio, interests) in enumerate(
        zip(
            others,
            ("Okafor", "Reyes", ""),
            ("she/her", "", "he/him"),
            (
                "Nurse on night shifts, free most mornings. I love a busy kitchen.",
                "Retired teacher. Happy to drive and to garden.",
                "",
            ),
            (["cooking", "kids"], ["gardening", "teaching"], ["animals"]),
            strict=True,
        )
    ):
        if n < 2:  # Priya stays without a photo: the initial shows instead
            data.set_volunteer_photo(uid, photos.process(drawn_photo(n + 1)))
        data.save_profile_details(
            uid,
            first_name=data.get_profile(uid).display_name,
            last_name=last,
            pronouns=pronouns,
            bio=bio,
            interests=interests,
        )
    for uid in others:
        data.swipe(uid, first.id, True)
    data.decide(
        coord, matches(where="volunteer_id = ?", where_args=[others[0]])[0].id, True
    )
    data.report_volunteer(  # an open report with a photo, for the admin's queue
        coord,
        matches(where="volunteer_id = ?", where_args=[others[1]])[0].id,
        "inappropriate_profile",
        "Demo report: is this landscape really the volunteer?",
    )

    _teams(org.id)
    me = _sam(data.get_or_create_volunteer(normalize_phone(ME_PHONE)))
    m = data.swipe(me, first.id, True)
    data.decide(coord, m.id, True)
    data.send_message(
        coord, m.id, "Hi Sam! Thanks for signing up. We start at 9, side door."
    )
    data.send_message(me, m.id, "Great, see you there. Anything I should bring?")
    data.send_message(coord, m.id, "Closed-toe shoes. We have gloves and coffee.")
    _past_shift(m.id, coord, 14, "completed")
    _past_shift(m.id, coord, 2, "scheduled")  # for the coordinator to mark
    _preferences(me)
    soon = _soon_shifts(me, org.id)
    urgent = _urgent_call(coord, org, first, sample=bool(seed_file))
    # the match with the coordinator holds a chat and shifts already
    _matches(me, org.id, ("calendar", "waiting"))
    return {
        "org": org.name,
        "match_id": m.id,
        "coordinator_id": coord,
        "opening_id": first.id,
        "urgent_opening_id": urgent,
        "soon_opening_ids": soon,
    }


def _sam(uid):
    """Profile uid becomes the demo volunteer Sam: onboarded, with a drawn photo and a
    profile (their match preferences come after, from _preferences)."""
    from app import data, photos

    _onboard(
        uid,
        "Sam",
        "Sawtelle",
        [
            "food-insecurity",
            "animal-shelters",
            "environment-parks",
            "homelessness-services",
        ],
        ["physical", "car"],
        "weekly",
    )
    data.set_volunteer_photo(uid, photos.process(drawn_photo()))
    data.save_profile_details(
        uid,
        first_name="Sam",
        last_name="Lee",
        pronouns="they/them",
        bio="Weekend hiker and home cook. Happy to lift boxes or walk dogs.",
        interests=["cooking", "hiking", "animals"],
    )
    return uid


def _teams(coordinator_org_id):
    """A stand-in coordinator for every org but the demo coordinator's, so their openings
    can be matched (and, with config.demo_matches, say yes by themselves)."""
    from app import data
    from app.db import orgs

    for n, o in enumerate(x for x in orgs(order_by="id") if x.id != coordinator_org_id):
        name = TEAM_NAMES[n % len(TEAM_NAMES)]
        data.add_coordinator(
            f"team{o.id}@{data.DEMO_TEAM_DOMAIN}", o.id, name=f"{name}, volunteer lead"
        )


# A demo volunteer's matches, in the order they are made: accepted with an upcoming shift
# (Add to calendar), waiting to hear back, accepted with the team's unanswered welcome, and
# accepted with a past shift marked done.
MATCH_KINDS = ("calendar", "waiting", "message", "done")


def _matches(uid, coordinator_org_id, kinds=MATCH_KINDS):
    """One match per kind, each from another org a stand-in team runs and, while it can, in
    another cause; logos first, and the openings with the most room, so copies spread out.
    The coordinator's org gets none (its inbox shows the seed's people, not every copy),
    and an opening in the next 48 hours only when nothing else is left: those are the
    "I'm free today" cards."""
    from app import data
    from app.db import Shift, matches, now, openings, shifts

    soon = (datetime.now(LA) + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M")
    found = []
    for card in data.deck(uid, 200):
        o = openings[card["id"]]
        team = data.demo_team(o.org_id)
        if card["call_id"] or o.org_id == coordinator_org_id or team is None:
            continue
        rank = (o.starts_at < soon, not card["logo"], -data.open_spots(team, o.id))
        found.append((rank, o, team, (json.loads(o.causes) or [""])[0]))
    found.sort(key=lambda f: f[0])
    orgs_used, causes_used = set(), set()
    for kind in kinds:
        pick = next(
            (
                f
                for f in found
                if f[1].org_id not in orgs_used and f[3] not in causes_used
            ),
            None,
        ) or next((f for f in found if f[1].org_id not in orgs_used), None)
        if pick is None:
            return
        _, o, team, cause = pick
        orgs_used.add(o.org_id)
        causes_used.add(cause)
        m = data.swipe(uid, o.id, True)
        if kind == "waiting":  # liked two days ago: the demo's auto-accept leaves it
            two_days = datetime.now(UTC) - timedelta(days=2)
            matches.update(id=m.id, created_at=two_days.isoformat(timespec="seconds"))
            continue
        data.decide(team, m.id, True)
        if kind == "message":
            data.send_message(team, m.id, data.DEMO_WELCOME.format("Sam"))
        elif kind == "done":  # its shift was two weeks ago, and they came
            past = (datetime.now(LA) - timedelta(days=14)).strftime("%Y-%m-%dT09:00")
            for sh in shifts(where="match_id = ?", where_args=[m.id]):
                shifts.delete(sh.id)
            shifts.insert(
                Shift(
                    match_id=m.id,
                    starts_at=past,
                    status="completed",
                    kind="accept",
                    created_by=team,
                    created_at=now(),
                    marked_at=now(),
                    marked_by=team,
                )
            )


def demo_volunteer(phone):
    """Signing in with ME_PHONE while config.demo_matches is on (the auth route's hook,
    set by main.py): a fresh Sam for this visitor alone, with their preferences, photo,
    matches, chat and shifts, and a full deck whose first card is the urgent call. Its
    profile id; None for any other number."""
    from app import data
    from app.config import DEMO_PHONE  # ME_PHONE, in E.164

    if phone != DEMO_PHONE:
        return None
    uid = _sam(data.new_demo_volunteer())
    _preferences(uid)
    coord = data.coordinator_by_email(COORDINATOR[0])
    _matches(uid, data.coordinator_org_ids(coord)[0] if coord else None)
    return uid


def _soon_shifts(me, coordinator_org_id):
    """Moves up to len(SOON_TIMES) openings of the volunteer's deck to tomorrow, one per
    cause, so "I'm free today" (shifts in the next 48 hours) has cards: first_start() puts
    every opening at least 2 days out. The coordinator's org is skipped: its openings hold
    the match and the urgent call. The moved ids."""
    from app import data
    from app.db import openings

    tomorrow = (datetime.now(LA) + timedelta(days=1)).date()
    causes, moved = set(), []
    for card in data.deck(me, 200):
        o = openings[card["id"]]
        cause = json.loads(o.causes or "[]")[:1]
        if o.org_id == coordinator_org_id or not cause or cause[0] in causes:
            continue
        causes.add(cause[0])
        openings.update(id=o.id, starts_at=f"{tomorrow}T{SOON_TIMES[len(moved)]}")
        moved.append(o.id)
        if len(moved) == len(SOON_TIMES):
            break
    return moved


def _urgent_call(coord, org, first, sample=False):
    """An urgent call, "needs 2 for tomorrow 9 am", auto-accepting, on the coordinator's
    other open opening (when the org has only one, it adds one: titled "Sample: ..." with
    a seed file's real nonprofits, like their other openings), and its first wave of
    texts. The opening's id."""
    from app import data, notify
    from app.db import openings

    other = next(
        (
            o
            for o in openings(order_by="id")
            if o.org_id == org.id and o.status == "open" and o.id != first.id
        ),
        None,
    )
    oid = (
        other.id
        if other
        else data.create_opening(
            coord,
            org.id,
            title=("Sample: " if sample else "") + "Restock the community fridge",
            about="Unload donated produce and restock the fridge outside the pantry.",
            cause=json.loads(org.causes)[0],
            lat=org.lat,
            lon=org.lon,
            neighborhood="Sawtelle",
            starts_at=(datetime.now(LA) + timedelta(days=12)).strftime(
                "%Y-%m-%dT10:00"
            ),
            recurrence="weekly",
            slots=4,
            orientation_required="no",
            notes="",
            reply_channel="email",
        )
    )
    tomorrow = (datetime.now(LA) + timedelta(days=1)).strftime("%Y-%m-%dT09:00")
    needed = min(2, data.open_spots(coord, oid))
    if needed:  # a fully booked opening takes no call
        data.post_urgent_call(coord, oid, tomorrow, needed, True)
        notify.send_urgent_waves()
    return oid


def sign_in_link(email):
    """A fresh emailed sign-in link (the fake emailer keeps it): one use, 15 minutes."""
    from app import data, notify

    uid = data.coordinator_by_email(email) or data.admin_by_email(email)
    return notify.send_login_link(email, data.create_login_token(uid))


def seed_preview(seed_dir=None):
    """A preview's startup (main.py): into an empty database only, seed() from seed_dir's
    orgs.json (with logos-raw/ and photos-raw/ beside it), else the invented orgs. The
    coordinator's and the admin's sign-in links go to the log, which Cloud Run keeps;
    /org/login shows a new coordinator link on the page in a preview."""
    from app.config import is_preview
    from app.db import orgs

    if not is_preview():
        raise RuntimeError(
            "seed_preview runs in a preview only (CAUSEMATCH_ENV=preview)"
        )
    if orgs.count:
        return
    info = seed(Path(seed_dir) / "orgs.json" if seed_dir else None)
    log.info("preview seeded %d orgs; the coordinator's is %s", orgs.count, info["org"])
    for role, (email, _) in (("coordinator", COORDINATOR), ("admin", ADMIN)):
        link = sign_in_link(email)
        log.info("preview %s %s (once, 15 minutes): %s", role, email, link)
