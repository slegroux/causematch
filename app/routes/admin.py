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
    Li,
    P,
    Redirect,
    Response,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Tr,
    Ul,
)
from fasthtml.common import Button as PlainButton
from monsterui.all import (
    Alert,
    AlertT,
    ButtonT,
    Container,
    LabelCheckboxX,
    TextPresets,
)
from starlette.concurrency import run_in_threadpool

from app import data, notify, propublica, security
from app.config import demo_address, is_preview
from app.forms import query, str_form
from app.match import LA
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
from app.org_import import NoCause, import_ein
from app.ui import (
    CARD,
    ORG_SECTION,
    ORG_TITLE,
    SMALL_LINK,
    Choice,
    Refusal,
    Router,
    TextInput,
    TopNav,
)

ar = Router()
# "Reset demo data": main.py adds these routes, and sets reset_demo to the demo seed's
# reset_preview, on a preview only (no route imports the demo seed)
preview_ar = Router()
reset_demo = None
RESET_CARD = ("Reset demo data", "Back to the sample data", "/admin/reset")
OUTBOX_CARD = ("Emails", "What this preview emailed, or would have", "/admin/outbox")
OUTBOX_SHOWN = 100  # the most recent fake emails /admin/outbox lists
# what /admin/outbox says an email to a tester is, instead of its subject (notify.KINDS)
KIND_LABELS = {
    "staff": "Sign-in link",
    "sign-in": "Sign-in link",
    "sign-up": "Sign-up link",
    "staff-notice": "New sign-in notice",
    "claim": "Claim decision",
    "urgent": "Last-minute call",
}
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
                    *([] if _no_reset() else [RESET_CARD]),
                    *([OUTBOX_CARD] if _outbox() is not None else []),
                )
            ],
            cls="grid gap-4 sm:grid-cols-2",
        ),
    )


def _no_reset():
    """Off a preview the reset doesn't exist, even where its routes were added."""
    return reset_demo is None or not is_preview()


def _outbox():
    """The fake emailer's record, on a preview with it only (notify.staging_links), else
    None: production can't start with the fake, so it never records or shows mail."""
    return notify.get_emailer().outbox if notify.staging_links() else None


def _OutboxCard(at, to, subject, body, sent, kind):
    tester = not demo_address(to)  # a real inbox: masked, and nothing of what it says
    return Div(
        P(
            KIND_LABELS.get(kind, "Notice") if tester else subject,
            cls="font-bold text-ink break-words",
        ),
        P(
            f"To {notify.mask_email(to) if tester else to} · "
            + at.astimezone(LA).strftime("%a %b %-d, %-I:%M %p")
            + (" · sent" if sent else " · not sent"),
            cls=TextPresets.muted_sm + " break-all",
        ),
        P(
            "A tester's address: not shown here." if tester else body,
            cls="whitespace-pre-wrap break-words text-ink",
        ),
        cls="space-y-2 p-5 " + CARD,
    )


@preview_ar("/admin/outbox", methods=["GET"])
def admin_outbox():
    """Staging: the emails a real deploy would have sent, newest first, each marked sent
    (really, with SMTP: notify.StagingEmailer) or not sent (fake). A demo address's in
    full, its body as plain text (escaped), sign-in links included: the same disclosure
    as the link the sign-in pages show for it. Any other address's masked, with its kind
    instead of its subject and no body, sent or not: anyone here can be an admin, and it
    may hold a tester's sign-in link or say what they do."""
    kept = _outbox()
    if kept is None:
        return Response("Not found", status_code=404)
    return (
        AdminPage(
            "admin",
            "Emails",
            P(
                "Staging: demo accounts' email is never sent. Every email, newest first "
                f"(the last {OUTBOX_SHOWN}), until a reset or a restart.",
                cls=TextPresets.muted_sm,
            ),
            *[_OutboxCard(*row) for row in reversed(kept[-OUTBOX_SHOWN:])]
            or [P("Nothing yet.", cls="text-ink-muted")],
        ),
        NO_STORE_HEADER,
    )


@preview_ar("/admin/reset", methods=["GET"])
def admin_reset_confirm():
    if _no_reset():
        return Response("Not found", status_code=404)
    return (
        AdminPage(
            "admin",
            "Reset demo data?",
            Div(
                Ul(
                    Li(
                        "Everything on this preview goes back to the sample data it "
                        "started with: likes, matches, chats, shifts, hours, claims and "
                        "every account made here."
                    ),
                    Li("You are signed out. This can't be undone."),
                    Li("The preview can be reset once every 15 minutes."),
                    cls="uk-list uk-list-disc",
                ),
                Form(
                    PlainButton(
                        "Reset demo data",
                        type="submit",
                        cls="uk-btn " + ButtonT.destructive,
                    ),
                    A("Keep it", href="/admin", cls=SMALL_LINK + " ml-4"),
                    action="/admin/reset",
                    method="post",
                    cls="flex items-center pt-2",
                ),
                cls="max-w-2xl space-y-3 p-5 " + CARD,
            ),
        ),
        NO_STORE_HEADER,
    )


@preview_ar("/admin/reset", methods=["POST"])
def admin_reset(req, sess):
    """The admin's own profile is among the rows deleted: they sign in again, on the page
    that offers the seeded admin in one tap. Under RESET_ALL and RESET_PER_IP: past
    either, the 429 page and nothing reset."""
    if _no_reset():
        return Response("Not found", status_code=404)
    ip = security.ip_subject(req)
    if not security.allow((security.RESET_ALL, "all"), (security.RESET_PER_IP, ip)):
        return security.too_many(retry_after=security.RESET_ALL[0][2])
    reset_demo()
    if (kept := _outbox()) is not None:
        kept.clear()
        if isinstance(emailer := notify.get_emailer(), notify.StagingEmailer):
            emailer.proven.clear()  # the accounts that proved them are gone
    # hashed as ip_subject's own log line, never the address
    log.info("preview reset (client key %s)", security._key("client", ip)[-12:])
    sess.clear()
    return Redirect("/org/login")


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
        P(f"{o['name']} · {status}", cls="font-semibold"),
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


def ClaimCard(c, error=None, choose_cause=False):
    """One pending claim for a non-technical admin: the org, the person, Approve, Decline.
    `choose_cause` adds the choice of cause for an org the IRS code places in none."""
    org = c["org"]
    ein = f"EIN {c['ein'][:2]}-{c['ein'][2:]}"
    members = ", ".join(f"{m['name']} ({m['role']})" for m in c["members"])
    mismatch = org is not None and not _names_match(org["name"], c["org_name"])
    if org is None:
        note = P("New to Causematch: approving adds it from IRS records.")
    elif org["deactivated"]:
        note = P("This organization is deactivated: it can't be approved.")
    else:
        note = None
    blocked = (org and org["deactivated"]) or c["suspended"]
    return Div(
        P(org["name"] if org else c["org_name"], cls=ORG_SECTION),
        P(f"{org['city']} · {ein}" if org and org["city"] else ein, cls="text-ink"),
        P(
            f"{c['name']} · {c['email']}",
            cls="text-ink break-words",
        ),
        P(f"They typed: {c['org_name']}", cls=TextPresets.muted_sm)
        if mismatch
        else None,
        note,
        P(f"Already claimed by {members}.", cls="font-semibold") if members else None,
        Alert(
            data.volunteer_address_warning("coordinator")
            + " Ask the nonprofit for a different address if you can.",
            cls=AlertT.warning,
        )
        if c["volunteer"] and not c["suspended"]
        else None,
        Alert(
            "This address belongs to a suspended volunteer: it can't be approved. "
            "Decline it.",
            cls=AlertT.error,
        )
        if c["suspended"]
        else None,
        Alert(error, cls=AlertT.error) if error else None,
        Div(
            None
            if blocked
            else Form(
                LabelCheckboxX(
                    "Yes, add them too (the people above can already sign in)",
                    name="confirm",
                    value="1",
                )
                if members
                else None,
                LabelCheckboxX(
                    f"Yes, approve for {org['name']}", name="name_checked", value="1"
                )
                if mismatch
                else None,
                LabelCheckboxX(
                    "Yes, make it a coordinator sign-in",
                    name="volunteer_ok",
                    value="1",
                )
                if c["volunteer"]
                else None,
                Choice(
                    "cause",
                    [(x.slug, x.label) for x in data.active_causes()],
                    placeholder="Choose a cause",
                    required=True,
                )
                if choose_cause
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
    "approved": "Approved. We emailed them where to sign in.",
    "approved-unsent": "Approved, but the email didn't send: they can sign in at "
    "/org/login with their email address.",
    "declined": "Declined. We let them know by email.",
    "declined-unsent": "Declined, but the email to them didn't send.",
}


def ClaimsPage(uid, only=None, error=None, done=None, choose_cause=False):
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
                    [
                        ClaimCard(
                            c,
                            error if c["id"] == only else None,
                            choose_cause and c["id"] == only,
                        )
                        for c in rows
                    ]
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
    return re.sub(r"[\W_]+", " ", (name or "").casefold()).strip()


def _names_match(irs_name, typed):
    """The typed name is the IRS one, give or take case and punctuation (never empty)."""
    return bool(_same_name(irs_name)) and _same_name(irs_name) == _same_name(typed)


def _sync_admin_claim_approve(req, form, claim_id: int):
    """One tap: an org no one has added yet is imported from ProPublica (network first,
    as scripts/import_orgs.py --ein does), then the claim is approved."""
    uid = req.scope["uid"]
    try:
        ein, typed, org = data.claim_for_approval(uid, claim_id)
    except LookupError:
        return Redirect("/admin/claims")  # already decided
    if org is None:
        chosen = form.get("cause", "")
        if chosen and chosen not in {c.slug for c in data.active_causes()}:
            return ClaimsPage(
                uid,
                claim_id,
                "That isn't one of our causes. Choose one from the list, then tap "
                "Approve again.",
                choose_cause=True,
            )
        if not security.allow((security.PROPUBLICA, "all")):
            return ClaimsPage(
                uid, claim_id, "Too many IRS lookups this hour. Try again later."
            )
        try:
            org = import_ein(ein, fallback=chosen or None)
        except propublica.LookupFailed:
            return ClaimsPage(
                uid,
                claim_id,
                "We couldn't reach the IRS records (ProPublica), so nothing changed. "
                "Tap Approve again in a minute.",
            )
        except NoCause as e:
            code = f"the code {e.ntee}" if e.ntee else "no code"
            return ClaimsPage(
                uid,
                claim_id,
                f"IRS records list {code} for this organization, which matches none of "
                "our causes. Choose one, then tap Approve again.",
                choose_cause=True,
            )
        except ValueError as e:  # NotImportable: worded for an admin
            return ClaimsPage(
                uid,
                claim_id,
                f"Can't add this organization: {e}. Decline it, or ask for help "
                "adding it.",
            )
    if not _names_match(org["name"], typed) and form.get("name_checked") != "1":
        # someone could type a harmless name beside a real nonprofit's EIN: approving
        # needs the box the card shows beside the IRS name (a double tap or a stale tab
        # never had it)
        where = ", ".join(x for x in (org["name"], org["city"]) if x)
        return ClaimsPage(
            uid,
            claim_id,
            f"IRS records name this EIN '{where}', not what they typed. Check it's "
            f'right, tick "Yes, approve for {org["name"]}", then tap Approve again.',
        )
    try:
        _, email, org_name = data.approve_claim(
            uid,
            claim_id,
            form.get("confirm") == "1",
            form.get("volunteer_ok") == "1",
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
