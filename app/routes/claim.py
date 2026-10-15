import re

from fasthtml.common import APIRouter, Input, P
from monsterui.all import Button, ButtonT, Form, LabelCheckboxX, TextPresets

from app import data, propublica
from app.auth import BAD_PHONE, normalize_phone
from app.forms import bad_request, str_form
from app.ui import Field, Page

ar = APIRouter()

# The same words for every EIN: known, unknown or already claimed.
RECEIVED = (
    "We'll review your request. If it is approved, a sign-in link will be emailed to "
    "the address you gave."
)
AUTHORIZED = "I'm authorized to represent this organization"


def ClaimForm(values=None, error=None):
    v = values or {}
    return Page(
        "Claim your organization",
        Form(
            Field(
                "Organization's EIN",
                Input(
                    name="ein",
                    value=v.get("ein", ""),
                    placeholder="53-0196605",
                    maxlength="20",
                    required=True,
                ),
            ),
            Field(
                "Organization name",
                Input(
                    name="org_name",
                    value=v.get("org_name", ""),
                    maxlength=str(data.CLAIM_LIMITS["org_name"]),
                    required=True,
                ),
            ),
            Field(
                "Your name",
                Input(
                    name="name",
                    value=v.get("name", ""),
                    maxlength=str(data.CLAIM_LIMITS["name"]),
                    autocomplete="name",
                    required=True,
                ),
            ),
            Field(
                "Your email",
                Input(
                    name="email",
                    value=v.get("email", ""),
                    type="email",
                    inputmode="email",
                    autocomplete="email",
                    maxlength=str(data.CLAIM_LIMITS["email"]),
                    required=True,
                ),
            ),
            Field(
                "Phone (optional)",
                Input(
                    name="phone",
                    value=v.get("phone", ""),
                    type="tel",
                    inputmode="tel",
                    autocomplete="tel",
                ),
            ),
            LabelCheckboxX(AUTHORIZED, name="authorized", value="1"),
            Button("Request access", cls=ButtonT.primary + " w-full"),
            P(
                "An admin reviews every request before anyone is added.",
                cls=TextPresets.muted_sm,
            ),
            action="/org/claim",
            method="post",
            cls="space-y-3",
        ),
        error=error,
        intro="Ask to manage your nonprofit's volunteer openings.",
    )


@ar("/org/claim", methods=["GET"])
def claim_page():
    return ClaimForm()


@ar("/org/claim", methods=["POST"])
async def claim_submit(req):
    form = await str_form(req)
    if form is None:
        return bad_request()
    text = lambda k: form.get(k, "").strip()
    v = {k: text(k) for k in ("ein", "org_name", "name", "email", "phone")}
    # Only these keys are read: a posted status or org_id goes nowhere.
    ein = propublica.normalize_ein(v["ein"])
    email = v["email"].lower()
    phone = normalize_phone(v["phone"]) if v["phone"] else ""
    limits = data.CLAIM_LIMITS
    if not ein:
        error = "An EIN is nine digits, like 53-0196605."
    elif not 0 < len(v["org_name"]) <= limits["org_name"]:
        error = "Enter the organization's name."
    elif not 0 < len(v["name"]) <= limits["name"]:
        error = "Enter your name."
    elif len(email) > limits["email"] or not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+", email
    ):
        error = "Enter your email address."
    elif v["phone"] and not phone:
        error = BAD_PHONE
    elif form.get("authorized") != "1":
        error = "Please confirm that you're authorized to represent this organization."
    else:
        data.submit_claim(ein, v["org_name"], v["name"], email, phone)
        return Page("Request received", P(RECEIVED))
    return ClaimForm(v, error)
