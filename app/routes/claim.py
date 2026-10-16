import re

from fasthtml.common import A, P
from monsterui.all import AT, Form, LabelCheckboxX, TextPresets
from starlette.concurrency import run_in_threadpool

from app import data, propublica, security
from app.auth import BAD_PHONE, normalize_phone
from app.forms import HIDDEN_CHARS_MESSAGE, hidden_chars, str_form
from app.ui import BigButton, Field, Refusal, Router, SignInPage, TextInput

ar = Router()

# The same words for every EIN: known, unknown or already claimed.
RECEIVED = (
    "We'll review your request. If it is approved, a sign-in link will be emailed to "
    "the address you gave."
)
AUTHORIZED = "I'm authorized to represent this organization"


def ClaimForm(values=None, error=None):
    v = values or {}
    return SignInPage(
        "Claim your organization",
        Form(
            Field(
                "Organization's EIN",
                TextInput(
                    name="ein",
                    value=v.get("ein", ""),
                    placeholder="53-0196605",
                    maxlength="20",
                    required=True,
                ),
            ),
            Field(
                "Organization name",
                TextInput(
                    name="org_name",
                    value=v.get("org_name", ""),
                    maxlength=str(data.CLAIM_LIMITS["org_name"]),
                    required=True,
                ),
            ),
            Field(
                "Your name",
                TextInput(
                    name="name",
                    value=v.get("name", ""),
                    maxlength=str(data.CLAIM_LIMITS["name"]),
                    autocomplete="name",
                    required=True,
                ),
            ),
            Field(
                "Your email",
                TextInput(
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
                TextInput(
                    name="phone",
                    value=v.get("phone", ""),
                    type="tel",
                    inputmode="tel",
                    autocomplete="tel",
                ),
            ),
            LabelCheckboxX(AUTHORIZED, name="authorized", value="1"),
            BigButton("Request access"),
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
        aside=P(
            A(
                "Already have access? Sign in",
                href="/org/login",
                cls=AT.muted + " inline-flex min-h-11 items-center",
            ),
            cls="pt-6 text-center text-sm",
        ),
    )


@ar("/org/claim", methods=["GET"])
def claim_page():
    return ClaimForm()


@ar("/org/claim", methods=["POST"])
async def claim_submit(req):
    form = await str_form(req)
    if form is None:
        return Refusal(400)
    return await run_in_threadpool(_sync_claim_submit, req, form)


def _sync_claim_submit(req, form):
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
    elif hidden_chars(v["org_name"]) or hidden_chars(v["name"]):
        error = HIDDEN_CHARS_MESSAGE
    elif len(email) > limits["email"] or not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+", email
    ):
        error = "Enter your email address."
    elif v["phone"] and not phone:
        error = BAD_PHONE
    elif form.get("authorized") != "1":
        error = "Please confirm that you're authorized to represent this organization."
    elif not security.allow(
        (security.CLAIM_PER_IP, security.ip_subject(req)),
        (security.CLAIM_PER_EMAIL, email),
    ):
        return security.too_many()
    else:
        data.submit_claim(ein, v["org_name"], v["name"], email, phone)
        return SignInPage("Request received", P(RECEIVED, cls="text-ink"))
    return ClaimForm(v, error)
