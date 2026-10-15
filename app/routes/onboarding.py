import re

from fasthtml.common import APIRouter, Div, Input, Option, P, Redirect, Select
from monsterui.all import Button, ButtonT, Form, FormLabel, LabelCheckboxX, TextPresets

from app import data
from app.geo import zip_latlon
from app.ui import Page

ar = APIRouter()

ADULTS_ONLY = "Causematch is for adults 18+"
LA_ONLY = "Causematch is only in Los Angeles for now"

TRAVEL = [  # (value, label, radius_km)
    ("15", "Up to 15 min", 8),
    ("30", "Up to 30 min", 16),
    ("45", "Up to 45 min", 25),
    ("any", "Any distance", 60),
]
AVAILABILITY = [
    ("weekday_morning", "Weekday mornings"),
    ("weekday_afternoon", "Weekday afternoons"),
    ("weekday_evening", "Weekday evenings"),
    ("weekend_morning", "Weekend mornings"),
    ("weekend_afternoon", "Weekend afternoons"),
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
FREQUENCY = [
    ("once", "Once, to try it"),
    ("monthly", "About monthly"),
    ("weekly", "About weekly"),
]
HEARD_FROM = [
    ("personal", "I know the founders personally"),
    ("second_degree", "A friend sent it to me"),
    ("public", "Social media, a group or a flyer"),
    ("other", "Other"),
]
CONSENT = (
    "I agree to the privacy policy. Causematch uses my answers to match me with "
    "openings, and shares my first name only with a nonprofit after I like their "
    "opening. My exact location is never shared. I can delete my account at any time."
)
INTRO = (
    "Tell us what you care about and when you're free, then swipe through openings "
    "near you. When you like one and the nonprofit says yes too, you're matched."
)


def Field(label, *inputs):
    return Div(FormLabel(label), *inputs, cls="space-y-2")


def Checks(name, options, checked=()):
    return Div(
        *[
            LabelCheckboxX(label, name=name, value=value, checked=value in checked)
            for value, label in options
        ],
        cls="space-y-1",
    )


def Choice(name, options, selected="", placeholder=None):
    return Select(
        Option(placeholder or "Choose one", value="", selected=not selected),
        *[
            Option(label, value=value, selected=value == selected)
            for value, label in options
        ],
        name=name,
        cls="uk-select h-10",
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
    f = {
        "first_name": form.get("first_name", "").strip(),
        "email": form.get("email", "").strip(),
        "adult": form.get("adult"),
        "zip": form.get("zip", "").strip(),
        "neighborhood": form.get("neighborhood", "").strip(),
        "travel": form.get("travel", ""),
        "causes": form.getlist("causes"),
        "other_causes": form.get("other_causes", "").strip(),
        "availability": form.getlist("availability"),
        "skills": form.getlist("skills"),
        "frequency": form.get("frequency", ""),
        "heard_from": form.get("heard_from", ""),
        "consent": form.get("consent"),
    }

    def fail(msg):
        return OnboardingForm(f, msg)

    if not f["adult"]:
        return fail(ADULTS_ONLY)
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
    if f["heard_from"] not in {v for v, _ in HEARD_FROM}:
        return fail("Please tell us how you heard about Causematch.")
    if not f["consent"]:
        return fail("Please agree to the privacy policy to continue.")
    data.save_onboarding(
        req.scope["uid"],
        display_name=f["first_name"],
        email=f["email"],
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
