import re

from fasthtml.common import APIRouter, Div, Input, P, Redirect, Response
from monsterui.all import Button, ButtonT, Form, LabelCheckboxX, TextPresets

from app import data
from app.choices import AVAILABILITY, FREQUENCY, SHARED_WITH_NONPROFIT
from app.geo import zip_latlon
from app.ui import Checks, Choice, Field, Page

ar = APIRouter()

ADULTS_ONLY = "Causematch is for adults 18+"
LA_ONLY = "Causematch is only in Los Angeles for now"

TRAVEL = [  # (value, label, radius_km)
    ("15", "Up to 15 min", 8),
    ("30", "Up to 30 min", 16),
    ("45", "Up to 45 min", 25),
    ("any", "Any distance", 60),
]
SKILLS = [
    ("physical", "Physical work / lifting"),
    ("car", "A car, happy to drive"),
    ("food_handling", "Cooking or food handling"),
    ("animals", "Experience with animals"),
    ("language", "A language other than English"),
    ("admin", "Admin, data or spreadsheets"),
    ("media", "Photos, video or social media"),
]
VOLUNTEERED = [("yes", "Yes"), ("no", "No")]
# Longest accepted value per free-text field.
LIMITS = {
    "first_name": 80,
    "email": 254,
    "neighborhood": 80,
    "other_causes": 300,
    "language_other": 80,
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
    "Tell us what you care about and when you're free, then swipe through openings "
    "near you. When you like one and the nonprofit says yes too, you're matched."
)


def OnboardingForm(f=None, error=None):
    f = f or {}
    return Page(
        "Welcome to Causematch",
        Form(
            Field(
                "First name",
                Input(
                    name="first_name",
                    value=f.get("first_name", ""),
                    autocomplete="given-name",
                    required=True,
                ),
            ),
            LabelCheckboxX(
                "I'm 18 or older", name="adult", value="1", checked=bool(f.get("adult"))
            ),
            Field(
                "Email (optional)",
                Input(
                    name="email",
                    type="email",
                    value=f.get("email", ""),
                    autocomplete="email",
                ),
            ),
            Field(
                "Your ZIP code",
                Input(
                    name="zip",
                    value=f.get("zip", ""),
                    inputmode="numeric",
                    maxlength="5",
                    autocomplete="postal-code",
                    required=True,
                ),
            ),
            Field(
                "Neighborhood (e.g. Silver Lake, Mar Vista, Koreatown)",
                Input(name="neighborhood", value=f.get("neighborhood", "")),
            ),
            Field(
                "How far would you travel for a shift?",
                Choice(
                    "travel",
                    [(v, label) for v, label, _ in TRAVEL],
                    f.get("travel", ""),
                ),
            ),
            Field(
                "Which causes do you want to help with? Pick any.",
                Checks(
                    "causes",
                    [(c.slug, c.label) for c in data.active_causes()],
                    f.get("causes", ()),
                ),
            ),
            Field(
                "Any other cause you'd like to see here? (optional)",
                Input(name="other_causes", value=f.get("other_causes", "")),
            ),
            Field(
                "When are you usually free?",
                Checks("availability", AVAILABILITY, f.get("availability", ())),
            ),
            Field(
                "Anything you bring? (optional)",
                Checks("skills", SKILLS, f.get("skills", ())),
            ),
            Field(
                "How often would you like to volunteer?",
                Choice("frequency", FREQUENCY, f.get("frequency", "")),
            ),
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
                    "I agree", name="consent", value="1", checked=bool(f.get("consent"))
                ),
                P(CONSENT, cls=TextPresets.muted_sm),
                cls="space-y-1",
            ),
            Button("Start swiping", cls=ButtonT.primary + " w-full"),
            action="/onboarding",
            method="post",
        ),
        error=error,
        intro=INTRO,
    )


@ar("/onboarding", methods=["GET"])
def onboarding_page(req):
    if data.get_profile(req.scope["uid"]).onboarded_at:
        return Redirect("/deck")
    return OnboardingForm()


@ar("/onboarding", methods=["POST"])
async def onboarding_save(req):
    form = await req.form()
    if any(not isinstance(v, str) for _, v in form.multi_items()):
        return Response("Bad request", status_code=400)
    text = lambda k: form.get(k, "").strip()
    f = {
        **{k: text(k) for k in LIMITS},
        "adult": form.get("adult"),
        "zip": text("zip"),
        "travel": form.get("travel", ""),
        "causes": form.getlist("causes"),
        "availability": form.getlist("availability"),
        "skills": form.getlist("skills"),
        "frequency": form.get("frequency", ""),
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
    if not f["first_name"]:
        return fail("Please enter your first name.")
    if f["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", f["email"]):
        return fail("That email doesn't look right. Leave it blank to skip.")
    latlon = zip_latlon(f["zip"])
    if not latlon:
        return fail(LA_ONLY)
    radius = {v: km for v, _, km in TRAVEL}.get(f["travel"])
    if not radius:
        return fail("Please choose how far you would travel.")
    active = {c.slug for c in data.active_causes()}
    if not f["causes"] or not set(f["causes"]) <= active:
        return fail("Please pick at least one cause from the list.")
    if not set(f["availability"]) <= {v for v, _ in AVAILABILITY} or not set(
        f["skills"]
    ) <= {v for v, _ in SKILLS}:
        return fail("Something in that form wasn't recognized. Please try again.")
    if f["frequency"] not in {v for v, _ in FREQUENCY} | {""}:
        return fail("Something in that form wasn't recognized. Please try again.")
    if f["volunteered_last_year"] not in {v for v, _ in VOLUNTEERED} | {""}:
        return fail("Something in that form wasn't recognized. Please try again.")
    if f["heard_from"] not in {v for v, _ in HEARD_FROM}:
        return fail("Please tell us how you heard about Causematch.")
    if not f["consent"]:
        return fail("Please agree to the privacy policy to continue.")
    data.save_onboarding(
        req.scope["uid"],
        display_name=f["first_name"],
        email=f["email"],
        neighborhood=f["neighborhood"],
        language_other=f["language_other"],
        volunteered_last_year=f["volunteered_last_year"],
        lat=latlon[0],
        lon=latlon[1],
        radius_km=radius,
        causes=f["causes"],
        other_causes=f["other_causes"],
        availability=f["availability"],
        skills=f["skills"],
        frequency=f["frequency"],
        heard_from=f["heard_from"],
    )
    return Redirect("/deck")
