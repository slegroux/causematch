import csv
import io
import logging
import re
from datetime import UTC, datetime

from fasthtml.common import (
    H1,
    H2,
    A,
    Dd,
    Div,
    Dl,
    Dt,
    Form,
    HttpHeader,
    Img,
    Input,
    P,
    Redirect,
    Response,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Tr,
)
from fasthtml.common import Button as PlainButton
from monsterui.all import Alert, AlertT, Container, LabelCheckboxX, TextPresets
from starlette.concurrency import run_in_threadpool

from app import data, notify, propublica, security
from app.forms import query, str_form
from app.metrics import (
    BREAKDOWNS,
    DEFINITIONS,
    METRICS,
    MIN_DENOM,
    MIN_GROUP_VOLUNTEERS,
    REPEAT_WINDOW_DAYS,
    URGENT,
    URGENT_DEFINITIONS,
    counts,
    csv_rows,
    fmt,
    label,
    pilot_metrics,
)
from app.org_import import import_ein
from app.ui import CARD, ORG_SECTION, ORG_TITLE, Refusal, Router, TextInput, TopNav

ar = Router()
log = logging.getLogger("causematch")
NO_STORE = {"Cache-Control": "no-store"}
NO_STORE_HEADER = HttpHeader("Cache-Control", "no-store")


def _now():
    return datetime.now(UTC)


# (key, label, href) of each section of the admin's top nav
ADMIN_TABS = (
    ("admin", "Admin", "/admin"),
    ("metrics", "Metrics", "/admin/metrics"),
    ("reports", "Reports", "/admin/reports"),
    ("orgs", "Orgs", "/admin/orgs"),
    ("claims", "Claims", "/admin/claims"),
    ("blocks", "Blocks", "/admin/blocks"),
)
BOX = "p-4 " + CARD  # one report, block, org or claim


def AdminPage(active, title, *content, action=None):
    """The shared staff header, then the title (with one action beside it) and content."""
    return Div(
        TopNav(ADMIN_TABS, active, "Admin", "/admin"),
        Container(
            Div(
                H1(title, cls=ORG_TITLE),
                action,
                cls="flex flex-wrap items-center justify-between gap-3 pb-4",
            ),
            *content,
            cls="max-w-5xl pt-6 pb-12",
        ),
    )


def cell(metric, c):
    small = counts(metric, c)
    return Td(
        Div(fmt(metric, c), cls="font-semibold"),
        Div(small, cls=TextPresets.muted_sm) if small else None,
        cls="px-3 py-2",
    )


def MetricsTable(title, groups, columns=METRICS):
    """columns: {metric: label}, the pilot's METRICS or the urgent calls' URGENT."""
    return Div(
        H2(title, cls=ORG_SECTION + " mt-6 mb-2"),
        Table(
            Thead(
                Tr(
                    Th("", cls="px-3 py-2 text-left"),
                    *[Th(name, cls="px-3 py-2 text-left") for name in columns.values()],
                )
            ),
            Tbody(
                *[
                    Tr(
                        Th(name, cls="px-3 py-2 text-left"),
                        *[cell(m, c[m]) for m in columns],
                    )
                    for name, c in groups.items()
                ]
            ),
            cls="uk-table uk-table-divider uk-table-small w-full bg-white",
        ),
        cls="overflow-x-auto",
    )


def Definitions():
    return Div(
        H2("Definitions", cls=ORG_SECTION + " mt-8 mb-2"),
        Dl(
            *[
                x
                for metric, text in {**DEFINITIONS, **URGENT_DEFINITIONS}.items()
                for x in (
                    Dt({**METRICS, **URGENT}[metric], cls="font-semibold"),
                    Dd(text, cls=TextPresets.muted_sm + " mb-2"),
                )
            ]
        ),
    )


@ar("/admin", methods=["GET"])
def admin_home():
    return AdminPage(
        "admin",
        "Admin",
        Div(
            *[
                A(
                    P(name, cls=ORG_SECTION),
                    P(what, cls=TextPresets.muted_sm),
                    href=href,
                    cls="block p-5 hover:border-brand " + CARD,
                )
                for name, what, href in (
                    ("Pilot metrics", "Rates and counts, by group", "/admin/metrics"),
                    ("Reports", "What people reported", "/admin/reports"),
                    ("Organizations", "Verify nonprofits", "/admin/orgs"),
                    ("Claims", "Requests to manage a nonprofit", "/admin/claims"),
                )
            ],
            cls="grid gap-4 sm:grid-cols-2",
        ),
    )


@ar("/admin/metrics", methods=["GET"])
def admin_metrics():
    m = pilot_metrics(data.metrics_rows(), _now())
    return (
        AdminPage(
            "metrics",
            "Pilot metrics",
            P(
                "Aggregates only; the counts sit beneath each figure. A dash means no data "
                f'yet. A rate over fewer than {MIN_DENOM} observations reads "too few". '
                f"A row with fewer than {MIN_GROUP_VOLUNTEERS} volunteers is merged into "
                '"other (small groups)". "Withheld" hides a rate beside a "too few" one, '
                "so it can't be subtracted back from the totals. Repeat rates cover "
                "volunteers whose first "
                f"completed shift was {REPEAT_WINDOW_DAYS}+ days ago. Only the first shift is "
                "scheduled automatically, so repeats also reflect how diligently "
                "coordinators add shifts.",
                cls=TextPresets.muted_sm,
            ),
            P(
                f"Empty decks: {m['empty_deck']['views']} views by "
                f"{m['empty_deck']['volunteers']} volunteers (filters that left nothing "
                "to swipe).",
                cls="mt-4 font-semibold text-ink",
            ),
            *[
                MetricsTable(
                    title,
                    {"All": m["overall"]}
                    if kind == "overall"
                    else {label(m, kind, k): g for k, g in m[kind].items()},
                )
                for kind, title in BREAKDOWNS.items()
            ],
            MetricsTable("Last-minute calls", {"All": m["urgent"]}, URGENT),
            Definitions(),
            action=A(
                "Download CSV",
                href="/admin/metrics.csv",
                cls="uk-btn uk-btn-default",
            ),
        ),
        NO_STORE_HEADER,
    )


@ar("/admin/metrics.csv", methods=["GET"])
def admin_metrics_csv():
    out = io.StringIO()
    csv.writer(out).writerows(csv_rows(pilot_metrics(data.metrics_rows(), _now())))
    return Response(
        out.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="pilot-metrics.csv"',
            **NO_STORE,
        },
    )


def _post(action, label, cls="uk-btn uk-btn-default"):
    return Form(
        PlainButton(label, type="submit", cls=cls), action=action, method="post"
    )


def ReportRow(r):
    subject = f"{r['reporter']} ({r['by']}) reported {r['target']}"
    return Div(
        P(subject, cls="font-semibold"),
        P(
            f"{r['reason']} - match {r['match_id']}, {r['opening']} ({r['org']})",
            cls=TextPresets.muted_sm,
        ),
        P(r["details"] or "No details.", cls="whitespace-pre-wrap break-words"),
        Img(
            src=f"/photos/thumb/{r['photo_key']}.jpg",
            alt=f"{r['target']}'s photo",
            cls="h-20 w-20 rounded-lg object-cover",
        )
        if r["photo_key"]
        else None,
        P("Already suspended.", cls="text-red-600") if r["suspended"] else None,
        P("Already deactivated.", cls="text-red-600") if r["deactivated"] else None,
        Div(
            _post(f"/admin/reports/{r['id']}/dismiss", "Dismiss"),
            _post(f"/admin/reports/{r['id']}/suspend", "Suspend user")
            if r["target_user_id"]
            else _post(f"/admin/reports/{r['id']}/deactivate", "Deactivate org"),
            _post(f"/admin/reports/{r['id']}/remove-photo", "Remove photo")
            if r["photo_key"]
            else None,
            cls="flex gap-3 pt-2",
        ),
        cls=BOX,
    )


@ar("/admin/reports", methods=["GET"])
def admin_reports(req):
    rows = data.admin_reports(req.scope["uid"])
    return (
        AdminPage(
            "reports",
            "Open reports",
            Div(
                *([ReportRow(r) for r in rows] or [P("No open reports.")]),
                cls="flex flex-col gap-3 max-w-2xl",
            ),
        ),
        NO_STORE_HEADER,
    )


def _resolve(fn, req, report_id):
    try:
        fn(req.scope["uid"], report_id)
    except LookupError:
        pass  # already resolved by someone else
    except (ValueError, PermissionError):
        return Refusal(400)
    return Redirect("/admin/reports")


@ar("/admin/reports/{report_id}/dismiss", methods=["POST"])
def admin_dismiss(req, report_id: int):
    return _resolve(data.dismiss_report, req, report_id)


@ar("/admin/reports/{report_id}/suspend", methods=["POST"])
def admin_suspend(req, report_id: int):
    return _resolve(data.suspend_target, req, report_id)


@ar("/admin/reports/{report_id}/deactivate", methods=["POST"])
def admin_deactivate(req, report_id: int):
    return _resolve(data.deactivate_target_org, req, report_id)


@ar("/admin/reports/{report_id}/remove-photo", methods=["POST"])
def admin_remove_photo(req, report_id: int):
    return _resolve(data.remove_reported_photo, req, report_id)


@ar("/admin/blocks", methods=["GET"])
def admin_blocks(req):
    rows = data.admin_blocks(req.scope["uid"])
    return (
        AdminPage(
            "blocks",
            "Blocks",
            Div(
                *(
                    [
                        Div(
                            P(
                                f"{b['volunteer']} (volunteer) blocked {b['org']}"
                                if b["kind"] == "volunteer"
                                else f"{b['org']} blocked {b['volunteer']}",
                                cls="font-semibold",
                            ),
                            _post(f"/admin/blocks/{b['id']}/unblock", "Unblock"),
                            cls=BOX,
                        )
                        for b in rows
                    ]
                    or [P("No blocks.")]
                ),
                cls="flex flex-col gap-3 max-w-2xl",
            ),
        ),
        NO_STORE_HEADER,
    )


@ar("/admin/blocks/{block_id}/unblock", methods=["POST"])
def admin_unblock(req, block_id: int):
    try:
        data.unblock(req.scope["uid"], block_id)
    except LookupError:
        pass
    return Redirect("/admin/blocks")


def OrgRow(o, found=None, error=None):
    status = (
        "Deactivated"
        if o["deactivated"]
        else "Verified"
        if o["verified"]
        else "Not verified"
    )
    return Div(
        P(f"{o['name']} - {status}", cls="font-semibold"),
        P(f"EIN {o['ein']}" if o["ein"] else "No EIN yet", cls=TextPresets.muted_sm),
        Alert(error, cls=AlertT.error) if error else None,
        Form(
            TextInput(
                name="ein",
                value=o["ein"],
                placeholder="53-0196605",
                aria_label="EIN",
                required=True,
            ),
            PlainButton("Look up", type="submit", cls="uk-btn uk-btn-default"),
            action=f"/admin/orgs/{o['id']}/lookup",
            method="post",  # it calls ProPublica: a GET would let a link spend the limit
            cls="flex gap-2 pt-2",
        ),
        Div(
            P(
                f"ProPublica: {found['name']}, {found['city']}, {found['state']} "
                f"(EIN {found['ein']})"
            ),
            Form(
                Input(type="hidden", name="ein", value=found["ein"]),
                PlainButton("Approve", type="submit", cls="uk-btn uk-btn-primary"),
                action=f"/admin/orgs/{o['id']}/verify",
                method="post",
            ),
            cls="pt-2",
        )
        if found
        else None,
        cls=BOX,
    )


def OrgsPage(rows, only=None, found=None, error=None):
    return (
        AdminPage(
            "orgs",
            "Organizations",
            Div(
                *[
                    OrgRow(o, found, error) if o["id"] == only else OrgRow(o)
                    for o in rows
                ],
                cls="flex flex-col gap-3 max-w-2xl",
            ),
        ),
        NO_STORE_HEADER,
    )


@ar("/admin/orgs", methods=["GET"])
def admin_orgs(req):
    return OrgsPage(data.admin_orgs(req.scope["uid"]))


@ar("/admin/orgs/{org_id}/lookup", methods=["POST"])
async def admin_org_lookup(req, org_id: int):
    form = await str_form(req)
    if form is None:
        return Refusal(400)
    uid, ein = req.scope["uid"], form.get("ein", "")
    rows = await run_in_threadpool(data.admin_orgs, uid)
    if org_id not in {o["id"] for o in rows}:
        return Response("Not found", status_code=404)
    if not propublica.normalize_ein(ein):  # a bad EIN spends no lookup
        return OrgsPage(rows, org_id, error="An EIN is nine digits, like 53-0196605.")
    if not await run_in_threadpool(security.allow, (security.PROPUBLICA, "all")):
        return security.too_many()
    try:
        found = await run_in_threadpool(propublica.lookup, ein)
    except ValueError as e:
        return OrgsPage(rows, org_id, error=str(e))
    except propublica.LookupFailed as e:
        return OrgsPage(rows, org_id, error=f"{e}. Try again in a minute.")
    if found is None:
        return OrgsPage(
            rows, org_id, error="ProPublica has no nonprofit with that EIN."
        )
    return OrgsPage(rows, org_id, found)


@ar("/admin/orgs/{org_id}/verify", methods=["POST"])
async def admin_org_verify(req, org_id: int):
    form = await str_form(req)
    ein = propublica.normalize_ein(form.get("ein", "")) if form else None
    if not ein:
        return Refusal(400)
    return await run_in_threadpool(_sync_org_verify, req.scope["uid"], org_id, ein)


def _sync_org_verify(uid, org_id, ein):
    try:
        data.verify_org_as(uid, org_id, ein)
    except LookupError:
        return Response("Not found", status_code=404)
    except ValueError as e:
        return OrgsPage(data.admin_orgs(uid), org_id, error=str(e))
    return Redirect("/admin/orgs")


def ClaimCard(c, error=None):
    """One pending claim for a non-technical admin: the org, the person, Approve, Decline."""
    org = c["org"]
    ein = f"EIN {c['ein'][:2]}-{c['ein'][2:]}"
    members = ", ".join(f"{m['name']} ({m['role']})" for m in c["members"])
    if org is None:
        note = P("New to Causematch: approving adds it from IRS records.")
    elif org["deactivated"]:
        note = P("This organization is deactivated: it can't be approved.")
    else:
        note = None
    return Div(
        P(org["name"] if org else c["org_name"], cls=ORG_SECTION),
        P(f"{org['city']} · {ein}" if org and org["city"] else ein, cls="text-ink"),
        P(
            f"{c['name']} · {c['email']}" + (f" · {c['phone']}" if c["phone"] else ""),
            cls="text-ink break-words",
        ),
        P(f"They typed: {c['org_name']}", cls=TextPresets.muted_sm)
        if org and c["org_name"] != org["name"]
        else None,
        note,
        P(f"Already claimed by {members}.", cls="font-semibold") if members else None,
        Alert(error, cls=AlertT.error) if error else None,
        Div(
            None
            if org and org["deactivated"]
            else Form(
                LabelCheckboxX(
                    "Yes, add them too (the people above can already sign in)",
                    name="confirm",
                    value="1",
                )
                if members
                else None,
                PlainButton("Approve", type="submit", cls="uk-btn uk-btn-primary"),
                action=f"/admin/claims/{c['id']}/approve",
                method="post",
                cls="flex flex-wrap gap-3 items-center",
            ),
            _post(f"/admin/claims/{c['id']}/reject", "Decline"),
            cls="flex flex-wrap gap-3 pt-2",
        ),
        cls=BOX + " space-y-1",
    )


# ?done= after a decision: the one-line confirmation (no name or address in the URL)
DONE = {
    "approved": "Approved. We emailed them a link to sign in.",
    "approved-unsent": "Approved, but the email didn't send: they can sign in at "
    "/org/login with their email address.",
    "declined": "Declined. We let them know by email.",
    "declined-unsent": "Declined, but the email to them didn't send.",
}


def ClaimsPage(uid, only=None, error=None, done=None):
    rows = data.admin_claims(uid)
    return (
        AdminPage(
            "claims",
            "Claim requests",
            Alert(DONE[done], cls=AlertT.success + " mb-4 max-w-2xl")
            if done in DONE
            else None,
            Div(
                *(
                    [ClaimCard(c, error if c["id"] == only else None) for c in rows]
                    or [P("No pending requests.")]
                ),
                cls="flex flex-col gap-3 max-w-2xl",
            ),
        ),
        NO_STORE_HEADER,
    )


@ar("/admin/claims", methods=["GET"])
def admin_claims(req):
    return ClaimsPage(req.scope["uid"], done=query(req, "done"))


def _tell(send, *args):
    """Send a claim notice; False when it failed. The decision stands either way."""
    try:
        send(*args)
    except Exception as e:  # noqa: BLE001  a failed notice is not an error for the admin
        log.warning("sending a claim notice failed: %s", type(e).__name__)
        return False
    return True


@ar("/admin/claims/{claim_id}/approve", methods=["POST"])
async def admin_claim_approve(req, claim_id: int):
    form = await str_form(req)
    if form is None:
        return Refusal(400)
    return await run_in_threadpool(_sync_admin_claim_approve, req, form, claim_id)


def _same_name(name):
    """A name for comparing: casefolded, punctuation and whitespace runs as one space."""
    return re.sub(r"[\W_]+", " ", name.casefold()).strip()


def _sync_admin_claim_approve(req, form, claim_id: int):
    """One tap: an org no one has added yet is imported from ProPublica (network first,
    as scripts/import_orgs.py --ein does), then the claim is approved."""
    uid = req.scope["uid"]
    try:
        missing = data.claim_ein_to_import(uid, claim_id)
    except LookupError:
        return Redirect("/admin/claims")  # already decided
    if missing:
        ein, typed = missing
        if not security.allow((security.PROPUBLICA, "all")):
            return ClaimsPage(
                uid, claim_id, "Too many IRS lookups this hour. Try again later."
            )
        try:
            org = import_ein(ein)
        except propublica.LookupFailed:
            return ClaimsPage(
                uid,
                claim_id,
                "We couldn't reach the IRS records (ProPublica), so nothing changed. "
                "Tap Approve again in a minute.",
            )
        except ValueError as e:  # NotImportable: worded for an admin
            return ClaimsPage(
                uid,
                claim_id,
                f"Can't add this organization: {e}. Decline it, or ask for help "
                "adding it.",
            )
        if _same_name(org["name"]) != _same_name(typed):
            # someone could type a harmless name beside a real nonprofit's EIN: the admin
            # sees the IRS name first, and the card (the org exists now) shows both
            where = ", ".join(x for x in (org["name"], org["city"]) if x)
            return ClaimsPage(
                uid,
                claim_id,
                f"IRS records name this EIN '{where}', not what they typed. "
                "Check it's right, then tap Approve again.",
            )
    try:
        _, email, org_name = data.approve_claim(
            uid, claim_id, form.get("confirm") == "1"
        )
    except LookupError:
        return Redirect("/admin/claims")
    except ValueError as e:
        return ClaimsPage(uid, claim_id, str(e))
    sent = _tell(notify.send_claim_approved, email, org_name)
    return Redirect(f"/admin/claims?done={'approved' if sent else 'approved-unsent'}")


@ar("/admin/claims/{claim_id}/reject", methods=["POST"])
async def admin_claim_reject(req, claim_id: int):
    return await run_in_threadpool(_sync_admin_claim_reject, req, claim_id)


def _sync_admin_claim_reject(req, claim_id: int):
    try:
        email = data.reject_claim(req.scope["uid"], claim_id)
    except LookupError:
        return Redirect("/admin/claims")
    sent = _tell(notify.send_claim_rejected, email)
    return Redirect(f"/admin/claims?done={'declined' if sent else 'declined-unsent'}")
