from fasthtml.common import (
    H1,
    A,
    Datalist,
    Div,
    Form,
    Option,
    P,
    Redirect,
    Script,
    Span,
)
from monsterui.all import AT, Alert, AlertT, Button, ButtonT, Container, LabelCheckboxX
from starlette.concurrency import run_in_threadpool

from app import data, match, prefs
from app.choices import ANY_DISTANCE_KM
from app.forms import (
    HIDDEN_CHARS_MESSAGE,
    bad_request,
    hidden_chars,
    str_form,
)
from app.geo import neighborhoods, shown_point, zip_latlon
from app.routes.prefs import CauseRow, PlaceFields, Section
from app.ui import TITLE, BigButton, Choice, Field, Router, TextInput, VolunteerShell

ar = Router()

ADULTS_ONLY = "Causematch is for adults 18+"
LA_ONLY = "Causematch is only in Los Angeles County for now"
NO_PLACE = (
    "Tell us where you'll help from: a neighborhood, a ZIP code or your location."
)

VOLUNTEERED = [("yes", "Yes"), ("no", "No")]
# Longest accepted value per free-text field.
LIMITS = {"first_name": 80}
HEARD_FROM = [
    ("personal", "I know the founders personally"),
    ("second_degree", "A friend sent it to me"),
    ("public", "Social media, a group or a flyer"),
    ("other", "Other"),
]
INTRO = (
    "Tell us what you care about, where and when, then swipe through openings near "
    "you. When you like one and the nonprofit says yes too, you're matched. Your free "
    "times, email and the rest you can set later in Me."
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
                        autofocus=True,  # typing starts at once: no tap to focus it
                        required=True,
                    ),
                ),
                LabelCheckboxX(
                    "I'm 18 or older",
                    name="adult",
                    value="1",
                    checked=bool(f.get("adult")),
                ),
            ),
            Section(
                "Where",
                "Where you'll help from, and how far you'd go. Only you see it.",
                PlaceFields(0, f.get("place", HOME), named=False),
                Datalist(*[Option(value=n) for n in neighborhoods()], id="la-areas"),
            ),
            Section(
                "What",
                "Love ranks first. Not for me hides every opening for that cause.",
                *[
                    CauseRow(c, f.get("causes", {}).get(c.slug))
                    for c in data.active_causes()
                ],
            ),
            LabelCheckboxX(
                Span(
                    "I agree to the ",
                    A("privacy policy", href="/privacy", cls=AT.muted),
                ),
                name="consent",
                value="1",
                checked=bool(f.get("consent")),
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


def _sync_onboarding_save(req, form):
    if data.get_profile(req.scope["uid"]).onboarded_at:
        return Redirect("/deck")  # later edits go through /me/preferences, /me/profile
    text = lambda k: form.get(k, "").strip()
    active = [c.slug for c in data.active_causes()]
    posted = {k: form.get(f"place1_{k}", v) for k, v in HOME.items() if k != "label"}
    shown_lat, shown_lon = shown_point(posted["lat"], posted["lon"])
    f = {
        **{k: text(k) for k in LIMITS},
        "adult": form.get("adult"),
        "place": {**HOME, **posted, "lat": shown_lat, "lon": shown_lon},
        "causes": {s: form.get(f"cause_{s}") for s in active if form.get(f"cause_{s}")},
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
    where = " ".join(posted["where"].split())
    if not where:
        return fail(NO_PLACE)
    typed_zip = prefs.as_zip(where)
    if typed_zip == "":
        return fail(prefs.FIVE_DIGITS)
    if typed_zip and not zip_latlon(typed_zip):
        return fail(LA_ONLY)
    place, error = prefs.resolve_place({**posted, "label": "Home"}, None, 0)
    if error:
        return fail(error)
    if not set(f["causes"].values()) <= {v for v, _ in prefs.CAUSE_STATES}:
        return fail(prefs.UNRECOGNIZED)
    if not {prefs.LOVE, prefs.OPEN} & set(f["causes"].values()):
        return fail("Please pick at least one cause you love or are open to.")
    if not f["consent"]:
        return fail("Please agree to the privacy policy to continue.")
    reach = match.reach_km(place["mode"], place["max_minutes"])
    data.save_onboarding(
        req.scope["uid"],
        display_name=f["first_name"],
        # the Home place, also as the profile's point and radius (metrics, old readers)
        lat=place["lat"],
        lon=place["lon"],
        radius_km=round(reach) if reach else ANY_DISTANCE_KM,
        causes=prefs.picked(f["causes"]),
        cause_prefs=f["causes"],
        place=place,
    )
    return Redirect("/deck")


# --- the one optional card after the first Like ---------------------------------------------

SKIPPED = "skipped"  # asked and passed on: metrics count it as unknown, and we don't ask again


def MoreCard(error=None):
    return VolunteerShell(
        "deck",
        H1("Liked! Two quick questions", cls=TITLE),
        P(
            "Both optional. They help us see who Causematch reaches.",
            cls="-mt-2 text-sm text-ink-muted",
        ),
        Alert(error, cls=AlertT.error + " mb-4") if error else None,
        Form(
            Field(
                "Have you volunteered in the last 12 months?",
                Choice("volunteered_last_year", VOLUNTEERED),
            ),
            Field(
                "How did you hear about Causematch?", Choice("heard_from", HEARD_FROM)
            ),
            BigButton("Save"),
            Button("Skip", name="skip", value="1", cls=ButtonT.ghost + " w-full"),
            action="/onboarding/more",
            method="post",
            cls="space-y-4",
        ),
    )


@ar("/onboarding/more", methods=["GET"])
def more_page(req):
    if data.get_profile(req.scope["uid"]).heard_from:
        return Redirect("/deck")
    return MoreCard()


@ar("/onboarding/more", methods=["POST"])
async def more_save(req):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_more_save, req.scope["uid"], form)


def _sync_more_save(uid, form):
    heard, volunteered = (
        form.get("heard_from", ""),
        form.get("volunteered_last_year", ""),
    )
    if form.get("skip"):
        heard = volunteered = ""
    elif heard not in {v for v, _ in HEARD_FROM} | {""} or volunteered not in {
        v for v, _ in VOLUNTEERED
    } | {""}:
        return MoreCard(prefs.UNRECOGNIZED)
    data.save_about(uid, heard or SKIPPED, volunteered)
    return Redirect("/deck")
