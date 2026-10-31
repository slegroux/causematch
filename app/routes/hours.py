import re
from datetime import datetime

from fasthtml.common import H1, H2, Div, Li, P, Redirect, Response, Span, Ul
from fasthtml.common import Form as PlainForm
from monsterui.all import Alert, AlertT, Button, ButtonT, TextPresets
from starlette.concurrency import run_in_threadpool

from app import data
from app.choices import minutes_label
from app.forms import query, str_form
from app.ui import (
    CARD,
    ORG_SECTION,
    TITLE,
    BackLink,
    Choice,
    Router,
    VolunteerShell,
    when,
)

ar = Router()

NOT_FOUND = "Not found"

NOTICE = {
    "limit": "You can issue 20 certificates a day. Try again tomorrow.",
    "period": "Pick a month, or all your hours there.",
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


def month_name(period):
    return datetime.strptime(period, "%Y-%m").strftime("%B %Y")


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
    except ValueError:
        return Redirect("/hours?notice=period")
    except (LookupError, PermissionError):
        return Response(NOT_FOUND, status_code=404)
    return Redirect(f"/hours/certificate/{done['id']}")


def ein_label(ein):
    digits = re.sub(r"\D", "", ein or "")
    return f"{digits[:2]}-{digits[2:]}" if len(digits) == 9 else (ein or "")


def long_date(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %-d, %Y")


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
            Span(minutes_label(c["minutes"]), cls="text-lg font-extrabold text-ink"),
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


@ar("/hours/certificate/{cert_id}", methods=["GET"])
def certificate_page(req, cert_id: str):
    uid = req.scope["uid"]
    try:
        c = data.my_certificate(uid, cert_id)
    except (LookupError, PermissionError):
        return Response(NOT_FOUND, status_code=404)
    return VolunteerShell(
        "me",
        Div(BackLink("/hours", "My hours"), cls="cm-noprint pb-2"),
        Certificate(c),
        P(
            "To keep a copy, print this page or save it as a PDF from your browser.",
            cls=TextPresets.muted_sm + " cm-noprint pt-3",
        ),
        uid=uid,
    )
