import re
from datetime import datetime

from fasthtml.common import (
    H1,
    H2,
    A,
    Div,
    FtResponse,
    Li,
    Meta,
    P,
    Redirect,
    Response,
    Span,
    Ul,
)
from fasthtml.common import Form as PlainForm
from monsterui.all import Alert, AlertT, Button, ButtonT, TextPresets
from starlette.concurrency import run_in_threadpool

from app import data, security
from app.certificate import (
    build_pdf,
    ein_label,
    long_date,
    month_name,
    pdf_filename,
)
from app.choices import minutes_label
from app.config import public_base_url
from app.forms import query, str_form
from app.ui import (
    CARD,
    ORG_SECTION,
    SMALL_LINK,
    TITLE,
    BackLink,
    Choice,
    Router,
    SimplePage,
    VolunteerShell,
    when,
)

ar = Router()

NOT_FOUND = "Not found"

NOTICE = {
    "limit": (
        f"You can issue {data.CERTIFICATES_PER_DAY} certificates a day. "
        "Try again tomorrow."
    ),
    "period": "Pick a month, or all your hours there.",
    "renamed": (
        "Hours confirmed before you changed your name can't go on a certificate. "
        "Pick a later month."
    ),
    "name": "Add your last name on your profile first: a certificate carries your full name.",
    "withdrawn": "Certificate withdrawn. Its check link now says so.",
}


def MonthCard(m):
    month = datetime.strptime(m["month"], "%Y-%m").strftime("%B %Y")
    return Div(
        Div(
            H2(month, cls=ORG_SECTION),
            Span(minutes_label(m["minutes"]), cls="font-bold text-ink"),
            cls="flex items-baseline justify-between gap-3",
        ),
        Ul(
            *(
                Li(
                    P(
                        f"{s['title']} - {minutes_label(s['minutes'])}",
                        cls="font-bold leading-snug text-ink",
                    ),
                    P(
                        f"{when(s['starts_at'], s['duration_minutes'])} - {s['org']}",
                        cls=TextPresets.muted_sm,
                    ),
                    P(f"Confirmed by {s['by']}", cls=TextPresets.muted_sm),
                    cls="space-y-1 py-3",
                )
                for s in m["shifts"]
            ),
            cls="divide-y divide-line",
        ),
        cls="w-full space-y-2 p-4 " + CARD,
    )


def CertificateCard(o):
    """One nonprofit's block: a period to choose and the button that issues the certificate."""
    period = Choice(
        "period",
        [
            *((m["period"], month_name(m["period"])) for m in o["months"]),
            ("all", "All my hours here"),
        ],
        placeholder="Which hours?",
        required=True,
    )
    period.attrs["aria-label"] = f"Hours to certify at {o['org']}"
    return Div(
        Div(
            H2(o["org"], cls=ORG_SECTION),
            Span(minutes_label(o["minutes"]), cls="font-bold text-ink"),
            cls="flex items-baseline justify-between gap-3",
        ),
        PlainForm(
            period,
            Button("Get a certificate", cls=ButtonT.default),
            action=f"/hours/orgs/{o['org_id']}/certificate",
            method="post",
            cls="space-y-3",
        ),
        cls="w-full space-y-3 p-4 " + CARD,
    )


@ar("/hours", methods=["GET"])
def hours_page(req):
    notice = query(req, "notice")
    mine = data.my_hours(req.scope["uid"])
    options = data.certificate_options(req.scope["uid"])
    left = mine["unconfirmed"]
    return VolunteerShell(
        "me",
        H1("My hours", cls=TITLE),
        Alert(NOTICE[notice], cls=AlertT.warning + " mb-4")
        if notice in NOTICE
        else None,
        P(
            "Hours a nonprofit confirmed when it marked you as attended.",
            cls=TextPresets.muted_sm + " pb-4",
        ),
        Div(
            *(MonthCard(m) for m in mine["months"]),
            *(
                []
                if mine["months"]
                else [P("No confirmed hours yet.", cls=TextPresets.muted_sm)]
            ),
            cls="flex flex-col gap-3 pb-6",
        ),
        Div(
            H2("Certificates", cls=ORG_SECTION),
            P(
                "A certificate lists the hours one nonprofit confirmed for you, with a "
                "signature code. You choose whether to share it.",
                cls=TextPresets.muted_sm,
            ),
            *(CertificateCard(o) for o in options),
            cls="flex flex-col gap-3 pb-6",
        )
        if options
        else None,
        Div(
            H2("Hours not confirmed", cls=ORG_SECTION),
            P(
                f"{len(left)} earlier shift{' has' if len(left) == 1 else 's have'} no "
                "confirmed hours, so they count toward nothing:",
                cls=TextPresets.muted_sm,
            ),
            Ul(
                *(
                    Li(f"{s['title']} - {s['org']}, {when(s['starts_at'])}")
                    for s in left
                ),
                cls="uk-list uk-list-disc text-sm text-ink-muted",
            ),
            cls="space-y-2 pb-6",
        )
        if left
        else None,
        uid=req.scope["uid"],
    )


@ar("/hours/orgs/{org_id}/certificate", methods=["POST"])
async def issue_certificate(req, org_id: int):
    form = await str_form(req)
    period = form.get("period", "") if form else ""
    if not re.fullmatch(r"all|\d{4}-\d{2}", period):
        return Redirect("/hours?notice=period")
    return await run_in_threadpool(_sync_issue, req.scope["uid"], org_id, period)


def _sync_issue(uid, org_id, period):
    try:
        done = data.issue_certificate(uid, org_id, period)
    except data.CertificateLimit:
        return Redirect("/hours?notice=limit")
    except data.NameChanged:
        return Redirect("/hours?notice=renamed")
    except data.NameRequired:
        return Redirect("/hours?notice=name")
    except ValueError:
        return Redirect("/hours?notice=period")
    except (LookupError, PermissionError):
        return Response(NOT_FOUND, status_code=404)
    return Redirect(f"/hours/certificate/{done['id']}")


def Certificate(c):
    """The printable record, from the snapshot frozen when it was issued."""
    snap = c["snapshot"]
    org = snap["org"]
    period = "All hours" if snap["period"] == "all" else month_name(snap["period"])
    return Div(
        P("Causematch", cls="text-sm font-semibold text-ink-muted"),
        H1("Volunteer hours certificate", cls=TITLE + " mt-1"),
        P(
            Span(snap["volunteer"], cls="font-bold"),
            " volunteered with ",
            Span(org["name"], cls="font-bold"),
            ". The nonprofit confirmed these hours.",
            cls="text-ink",
        ),
        P(
            *([f"EIN {ein_label(org['ein'])}"] if org["ein"] else []),
            *([" - " if org["ein"] else "", org["website"]] if org["website"] else []),
            cls=TextPresets.muted_sm,
        )
        if org["ein"] or org["website"]
        else None,
        P(period, cls="pt-2 font-bold text-ink"),
        Ul(
            *(
                Li(
                    Div(
                        Span(long_date(s["date"]), cls="text-ink"),
                        Span(minutes_label(s["minutes"]), cls="font-bold text-ink"),
                        cls="flex items-baseline justify-between gap-3",
                    ),
                    P(s["role"], cls=TextPresets.muted_sm),
                    cls="py-2",
                )
                for s in snap["shifts"]
            ),
            cls="divide-y divide-line",
        ),
        Div(
            Span("Total", cls="font-bold text-ink"),
            Span(minutes_label(snap["minutes"]), cls="text-lg font-extrabold text-ink"),
            cls="flex items-baseline justify-between gap-3 border-t border-line pt-3",
        ),
        P(
            "Hours confirmed by " + ", ".join(snap["coordinators"]) + ".",
            cls=TextPresets.muted_sm + " pt-3",
        ),
        P(f"Issued {long_date(snap['issued'])}", cls=TextPresets.muted_sm),
        P(
            "Signature code: ",
            Span(c["code"], cls="font-mono font-bold tracking-wide text-ink"),
            cls="pt-3 text-ink",
        ),
        cls="w-full space-y-2 p-5 " + CARD,
    )


@ar("/hours/certificate/{cert_id}.pdf", methods=["GET"])
async def certificate_pdf(req, cert_id: str):
    return await run_in_threadpool(_sync_pdf, req.scope["uid"], cert_id)


def _sync_pdf(uid, cert_id):
    """The owner's certificate as a PDF, built from the stored snapshot. A snapshot that
    no longer matches its signature is not offered."""
    try:
        c = data.my_certificate(uid, cert_id)
    except (LookupError, PermissionError):
        return Response(NOT_FOUND, status_code=404)
    if not c["genuine"]:
        return Response(NOT_FOUND, status_code=404)
    pdf = build_pdf(c["snapshot"], c["id"], f"{public_base_url()}/check/{c['id']}")
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{pdf_filename(c["snapshot"])}"',
            "Cache-Control": "private, no-store",
        },
    )


@ar("/hours/certificate/{cert_id}", methods=["GET"])
def certificate_page(req, cert_id: str):
    uid = req.scope["uid"]
    try:
        c = data.my_certificate(uid, cert_id)
    except (LookupError, PermissionError):
        return Response(NOT_FOUND, status_code=404)
    if not c["genuine"]:  # a signature that no longer matches: not shown, as in the PDF
        return Response(NOT_FOUND, status_code=404)
    return VolunteerShell(
        "me",
        Div(BackLink("/hours", "My hours"), cls="cm-noprint pb-2"),
        Certificate(c),
        Div(
            P("Anyone with this link can check this certificate:", cls="text-ink"),
            P(
                f"{public_base_url()}/check/{c['id']}",
                cls="break-words font-mono text-sm font-bold text-ink",
            ),
            cls="space-y-1 pt-3",
        ),
        A(
            "Download PDF",
            href=f"/hours/certificate/{c['id']}.pdf",
            cls="uk-btn uk-btn-primary cm-noprint mt-3 inline-flex h-12 items-center",
        ),
        P(
            "To keep a copy, print this page or save it as a PDF from your browser.",
            cls=TextPresets.muted_sm + " cm-noprint pt-3",
        ),
        A(
            "Withdraw this certificate",
            href=f"/hours/certificate/{c['id']}/withdraw",
            cls=SMALL_LINK + " cm-noprint mt-3 inline-flex min-h-11 items-center",
        ),
        uid=uid,
    )


@ar("/hours/certificate/{cert_id}/withdraw", methods=["GET"])
def withdraw_certificate_confirm(req, cert_id: str):
    uid = req.scope["uid"]
    try:
        data.my_certificate(uid, cert_id)
    except (LookupError, PermissionError):
        return Response(NOT_FOUND, status_code=404)
    return VolunteerShell(
        "me",
        H1("Withdraw this certificate?", cls=TITLE),
        Div(
            P(
                "Its check link will say the certificate was withdrawn, and nothing "
                "else. Copies you already gave out stop being checkable. You can issue "
                "a new one from My hours.",
                cls="text-ink",
            ),
            PlainForm(
                Button("Withdraw", cls=ButtonT.destructive),
                A(
                    "Keep it",
                    href=f"/hours/certificate/{cert_id}",
                    cls=SMALL_LINK + " ml-4",
                ),
                action=f"/hours/certificate/{cert_id}/withdraw",
                method="post",
                cls="flex items-center pt-2",
            ),
            cls="space-y-3 p-5 " + CARD,
        ),
        uid=uid,
    )


@ar("/hours/certificate/{cert_id}/withdraw", methods=["POST"])
def withdraw_certificate(req, cert_id: str):
    try:
        data.withdraw_certificate(req.scope["uid"], cert_id)
    except PermissionError:
        return Response(NOT_FOUND, status_code=404)
    return Redirect("/hours?notice=withdrawn")


# --- the public check link ----------------------------------------------------------------


def CheckRow(label, value):
    return Div(
        Span(label, cls=TextPresets.muted_sm),
        Span(value, cls="text-right font-bold text-ink"),
        cls="flex items-baseline justify-between gap-4 py-2",
    )


def CheckPage(r):
    """What the check link says for data.check_certificate's result; (page, status)."""
    state = r["state"]
    if state == "unknown":
        return SimplePage(
            "Not a Causematch record",
            P("This is not a Causematch record.", cls="mt-2 text-ink"),
        ), 404
    if state == "withdrawn":
        return SimplePage(
            "Withdrawn", P("This certificate was withdrawn.", cls="mt-2 text-ink")
        ), 200
    period = "All hours" if r["period"] == "all" else month_name(r["period"])
    changed = state == "changed"
    return SimplePage(
        "Hours have changed" if changed else "Genuine certificate",
        P(
            "This is a genuine Causematch certificate as it was issued, but "
            f"{r['org']} has corrected the hours it confirmed since then. "
            "The figures below are as issued."
            if changed
            else "This is a genuine Causematch certificate, unchanged since it was issued.",
            cls="mt-2 text-ink",
        ),
        Div(
            CheckRow("Volunteer", r["volunteer"]),
            CheckRow("Nonprofit", r["org"]),
            *([CheckRow("EIN", ein_label(r["ein"]))] if r["ein"] else []),
            CheckRow("Period", period),
            CheckRow(
                "Hours when issued" if changed else "Total hours",
                minutes_label(r["minutes"]),
            ),
            CheckRow("Issued", long_date(r["issued"])),
            CheckRow("Signature code", Span(r["code"], cls="font-mono")),
            cls="mt-4 divide-y divide-line",
        ),
    ), 200


@ar("/check/{code}", methods=["GET"])
async def check_link(req, code: str):
    return await run_in_threadpool(_sync_check, req, code)


def _sync_check(req, code):
    if not security.allow((security.CHECK_PER_IP, security.ip_subject(req))):
        return security.too_many()
    page, status = CheckPage(data.check_certificate(code))
    return FtResponse(
        (Meta(name="robots", content="noindex"), *page),
        status_code=status,
        headers={
            "X-Robots-Tag": "noindex, nofollow",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )
