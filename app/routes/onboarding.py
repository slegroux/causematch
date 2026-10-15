import re

from fasthtml.common import H1, A, Datalist, Div, Form, Option, P, Redirect, Script
from monsterui.all import AT, Alert, AlertT, Container, LabelCheckboxX
from starlette.concurrency import run_in_threadpool

from app import data, match, prefs
from app.choices import ANY_DISTANCE_KM, SHARED_WITH_NONPROFIT
from app.forms import HIDDEN_CHARS_MESSAGE, bad_request, hidden_chars, str_form
from app.geo import neighborhoods, round_point, zip_latlon
from app.routes.prefs import SLOTS, CauseRow, DayGrid, PlaceFields, Section
from app.ui import TITLE, BigButton, Choice, Field, Router, TextInput

ar = Router()

ADULTS_ONLY = "Causematch is for adults 18+"
LA_ONLY = "Causematch is only in Los Angeles for now"
FIVE_DIGITS = prefs.FIVE_DIGITS
NO_PLACE = (
    "Tell us where you'll help from: a neighborhood, a ZIP code or your location."
)
UNRECOGNIZED = "Something in that form wasn't recognized. Please try again."

VOLUNTEERED = [("yes", "Yes"), ("no", "No")]
# Longest accepted value per free-text field.
LIMITS = {
    "first_name": 80,
    "email": 254,
    "neighborhood": 80,
    "other_causes": 300,
}
HEARD_FROM = [
    ("personal", "I know the founders personally"),
    ("second_degree", "A friend sent it to me"),
    ("public", "Social media, a group or a flyer"),
    ("other", "Other"),
]
CONSENT = (
    "I agree to the privacy policy. Causematch uses my answers to match me with "
    f"openings. {SHARED_WITH_NONPROFIT} I can delete my account at any time."
)
INTRO = (
    "Tell us what you care about, where and when, then swipe through openings near "
    "you. When you like one and the nonprofit says yes too, you're matched. The rest "
    "(skills, kind of work, a second place) you can set later in Me."
)
# The Home place as onboarding starts it: by car, within 30 minutes
HOME = {
    "label": "Home",
    "where": "",
    "lat": "",
    "lon": "",
    "mode": "car",
    "minutes": "30",
}


def OnboardingForm(f=None, error=None):
    f = f or {}
    return Container(
        H1("Welcome to Causematch", cls=TITLE),
        P(INTRO, cls="-mt-2 mb-4 text-sm text-ink-muted"),
        Alert(error, cls=AlertT.error + " mb-4") if error else None,
        Form(
            Section(
                "About you",
                None,
                Field(
                    "First name",
                    TextInput(
                        name="first_name",
                        value=f.get("first_name", ""),
                        autocomplete="given-name",
                        required=True,
                    ),
                ),
                LabelCheckboxX(
                    "I'm 18 or older",
                    name="adult",
                    value="1",
                    checked=bool(f.get("adult")),
                ),
                Field(
                    "Email (optional)",
                    TextInput(
                        name="email",
                        type="email",
                        value=f.get("email", ""),
                        autocomplete="email",
                    ),
                ),
            ),
            Section(
                "Where",
                "Where you'll help from, and how far you'd go. Only you see it.",
                PlaceFields(0, f.get("place", HOME), named=False),
                Datalist(*[Option(value=n) for n in neighborhoods()], id="la-areas"),
                Field(
                    "Neighborhood nonprofits see (optional, e.g. Silver Lake)",
                    TextInput(name="neighborhood", value=f.get("neighborhood", "")),
                ),
            ),
            Section(
                "What",
                "Love ranks first. Not for me hides every opening for that cause.",
                *[
                    CauseRow(c, f.get("causes", {}).get(c.slug))
                    for c in data.active_causes()
                ],
                Field(
                    "Any other cause you'd like to see here? (optional)",
                    TextInput(name="other_causes", value=f.get("other_causes", "")),
                ),
            ),
            Section(
                "When",
                "Tick the times you're usually free (optional).",
                DayGrid(f.get("availability", ())),
            ),
            Section(
                "A little more",
                None,
                Field(
                    "Have you volunteered in the last 12 months?",
                    Choice(
                        "volunteered_last_year",
                        VOLUNTEERED,
                        f.get("volunteered_last_year", ""),
                    ),
                ),
                Field(
                    "How did you hear about Causematch?",
                    Choice("heard_from", HEARD_FROM, f.get("heard_from", "")),
                ),
                Div(
                    LabelCheckboxX(
                        "I agree",
                        name="consent",
                        value="1",
                        checked=bool(f.get("consent")),
                    ),
                    P(
                        CONSENT,
                        " ",
                        A("Read the privacy policy", href="/privacy", cls=AT.muted),
                        ".",
                        cls="text-sm text-ink-muted",
                    ),
                    cls="space-y-1",
                ),
            ),
            Div(BigButton("Start swiping"), cls="cm-savebar z-20 -mx-1 py-3"),
            action="/onboarding",
            method="post",
            cls="space-y-4",
        ),
        Script(src="/static/geo.js", defer=True),
        cls="max-w-md pb-8",
    )


@ar("/onboarding", methods=["GET"])
def onboarding_page(req):
    if data.get_profile(req.scope["uid"]).onboarded_at:
        return Redirect("/deck")
    return OnboardingForm()


@ar("/onboarding", methods=["POST"])
async def onboarding_save(req):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_onboarding_save, req, form)


def _shown_point(lat, lon):
    """A posted device location as the page may show it again: rounded, or nothing."""
    point = round_point(lat, lon) if lat or lon else None
    return (f"{point[0]:.2f}", f"{point[1]:.2f}") if point else ("", "")


def _sync_onboarding_save(req, form):
    if data.get_profile(req.scope["uid"]).onboarded_at:
        return Redirect("/deck")  # later changes go through /me/preferences
    text = lambda k: form.get(k, "").strip()
    active = [c.slug for c in data.active_causes()]
    posted = {k: form.get(f"place1_{k}", v) for k, v in HOME.items() if k != "label"}
    shown_lat, shown_lon = _shown_point(posted["lat"], posted["lon"])
    f = {
        **{k: text(k) for k in LIMITS},
        "adult": form.get("adult"),
        "place": {**HOME, **posted, "lat": shown_lat, "lon": shown_lon},
        "causes": {s: form.get(f"cause_{s}") for s in active if form.get(f"cause_{s}")},
        "availability": form.getlist("availability"),
        "volunteered_last_year": form.get("volunteered_last_year", ""),
        "heard_from": form.get("heard_from", ""),
        "consent": form.get("consent"),
    }

    def fail(msg):
        return OnboardingForm(f, msg)

    if f["adult"] != "1":
        return fail(ADULTS_ONLY)
    if any(len(f[k]) > n for k, n in LIMITS.items()):
        return fail("One of your answers is too long. Please shorten it.")
    if any(hidden_chars(f[k]) for k in LIMITS):
        return fail(HIDDEN_CHARS_MESSAGE)
    if not f["first_name"]:
        return fail("Please enter your first name.")
    if f["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", f["email"]):
        return fail("That email doesn't look right. Leave it blank to skip.")
    where = " ".join(posted["where"].split())
    if not where:
        return fail(NO_PLACE)
    typed_zip = prefs.as_zip(where)
    if typed_zip == "":
        return fail(FIVE_DIGITS)
    if typed_zip and not zip_latlon(typed_zip):
        return fail(LA_ONLY)
    place, error = prefs.resolve_place({**posted, "label": "Home"}, None, 0)
    if error:
        return fail(error)
    if not set(f["causes"].values()) <= {v for v, _ in prefs.CAUSE_STATES}:
        return fail(UNRECOGNIZED)
    if not {prefs.LOVE, prefs.OPEN} & set(f["causes"].values()):
        return fail("Please pick at least one cause you love or are open to.")
    if not set(f["availability"]) <= SLOTS:
        return fail(UNRECOGNIZED)
    if f["volunteered_last_year"] not in {v for v, _ in VOLUNTEERED} | {""}:
        return fail(UNRECOGNIZED)
    if f["heard_from"] not in {v for v, _ in HEARD_FROM}:
        return fail("Please tell us how you heard about Causematch.")
    if not f["consent"]:
        return fail("Please agree to the privacy policy to continue.")
    reach = match.reach_km(place["mode"], place["max_minutes"])
    data.save_onboarding(
        req.scope["uid"],
        display_name=f["first_name"],
        email=f["email"],
        neighborhood=f["neighborhood"],
        volunteered_last_year=f["volunteered_last_year"],
        # the Home place, also as the profile's point and radius (metrics, old readers)
        lat=place["lat"],
        lon=place["lon"],
        radius_km=round(reach) if reach else ANY_DISTANCE_KM,
        causes=prefs.picked(f["causes"]),
        cause_prefs=f["causes"],
        other_causes=f["other_causes"],
        availability=match.day_slots(f["availability"]),
        heard_from=f["heard_from"],
        place=place,
    )
    return Redirect("/deck")
