from fasthtml.common import A, Div, P, Script, Ul
from monsterui.all import AT, Form, LabelCheckboxX, TextPresets
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app import data, propublica, security
from app.auth import BAD_PHONE, normalize_phone
from app.forms import EMAIL, HIDDEN_CHARS_MESSAGE, hidden_chars, query, str_form
from app.org_import import PLACE_NAMES
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
            # static/claim.js shows it: without the script, the EIN is typed as before
            Div(
                Field(
                    "Find your organization",
                    TextInput(
                        id="org-search",
                        type="search",
                        placeholder="Start typing its name",
                        maxlength=str(SEARCH_MAX),
                        autocomplete="off",
                    ),
                ),
                P(
                    "From IRS records. Pick yours to fill in the EIN and name below.",
                    cls=TextPresets.muted_sm,
                ),
                Ul(id="org-results", hidden=True, cls="space-y-2"),
                P(id="org-search-note", aria_live="polite", cls=TextPresets.muted_sm),
                id="org-search-field",
                hidden=True,
                cls="space-y-2",
            ),
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
        Script(src="/static/claim.js", defer=True),
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


SEARCH_MIN, SEARCH_MAX, SEARCH_SHOWN = 3, 100, 10


@ar("/org/claim/search", methods=["GET"])
async def claim_search(req):
    """{"results": [{"ein", "name", "city", "state", "outside_la"}]}: California
    501(c)(3)s in IRS records (ProPublica) whose name matches q, for static/claim.js, the
    ones mailing from a Los Angeles County place first. It reads ProPublica only, never the
    orgs table, so the answer is the same whether an org is on Causematch, claimed or
    neither. 403 for another site's request (nothing spent), 429 over the limit and 503
    when ProPublica fails: the EIN is typed instead."""
    site = req.headers.get("sec-fetch-site")
    if site and site != "same-origin":
        return JSONResponse({"results": []}, status_code=403)
    q = query(req, "q").strip()
    if not SEARCH_MIN <= len(q) <= SEARCH_MAX or hidden_chars(q):
        return JSONResponse({"results": []})  # spends nothing
    return await run_in_threadpool(_sync_claim_search, req, q)


def _sync_claim_search(req, q):
    if not security.allow(
        (security.CLAIM_SEARCH_PER_IP, security.ip_subject(req)),
        (security.CLAIM_SEARCH_ALL, "all"),
    ):
        return JSONResponse({"results": []}, status_code=429)
    try:
        hits, _, _ = propublica.search(q, "CA", None)
    except propublica.LookupFailed:
        return JSONResponse({"results": []}, status_code=503)
    for h in hits:
        h["outside_la"] = h["city"].lower() not in PLACE_NAMES
    hits.sort(key=lambda h: h["outside_la"])  # stable: ProPublica's order within each
    return JSONResponse(
        {
            "results": [
                {
                    "ein": f"{h['ein'][:2]}-{h['ein'][2:]}",
                    "name": h["name"],
                    "city": h["city"],
                    "state": h["state"],
                    "outside_la": h["outside_la"],
                }
                for h in hits[:SEARCH_SHOWN]
            ]
        }
    )


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
    elif len(email) > limits["email"] or not EMAIL.fullmatch(email):
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
