import csv
import io
from datetime import UTC, datetime

from fasthtml.common import (
    H2,
    A,
    APIRouter,
    Dd,
    Div,
    Dl,
    Dt,
    Form,
    HttpHeader,
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
from monsterui.all import AT, Alert, AlertT, Container, TextPresets
from starlette.concurrency import run_in_threadpool

from app import data, propublica
from app.forms import bad_request, query, str_form
from app.metrics import (
    BREAKDOWNS,
    DEFINITIONS,
    METRICS,
    MIN_DENOM,
    MIN_GROUP_VOLUNTEERS,
    REPEAT_WINDOW_DAYS,
    counts,
    csv_rows,
    fmt,
    label,
    pilot_metrics,
)

ar = APIRouter()
NO_STORE = {"Cache-Control": "no-store"}
NO_STORE_HEADER = HttpHeader("Cache-Control", "no-store")


def _now():
    return datetime.now(UTC)


def AdminPage(title, *content):
    return Container(
        Div(
            A("Admin", href="/admin", cls=AT.muted),
            A("Metrics", href="/admin/metrics", cls=AT.muted),
            A("Reports", href="/admin/reports", cls=AT.muted),
            A("Orgs", href="/admin/orgs", cls=AT.muted),
            A("Blocks", href="/admin/blocks", cls=AT.muted),
            A("Download CSV", href="/admin/metrics.csv", cls=AT.muted),
            Form(
                PlainButton(
                    "Sign out", type="submit", cls=AT.muted + " bg-transparent p-0"
                ),
                action="/logout",
                method="post",
            ),
            cls="flex gap-4 py-3 text-sm",
        ),
        Div(title, cls="text-2xl font-bold my-4"),
        *content,
        cls="max-w-7xl pb-8",
    )


def cell(metric, c):
    small = counts(metric, c)
    return Td(
        Div(fmt(metric, c), cls="font-semibold"),
        Div(small, cls=TextPresets.muted_sm) if small else None,
        cls="px-3 py-2",
    )


def MetricsTable(title, groups):
    return Div(
        H2(title, cls="text-lg font-semibold mt-6 mb-2"),
        Table(
            Thead(
                Tr(
                    Th("", cls="px-3 py-2 text-left"),
                    *[Th(name, cls="px-3 py-2 text-left") for name in METRICS.values()],
                )
            ),
            Tbody(
                *[
                    Tr(
                        Th(name, cls="px-3 py-2 text-left"),
                        *[cell(m, c[m]) for m in METRICS],
                    )
                    for name, c in groups.items()
                ]
            ),
            cls="uk-table uk-table-divider uk-table-small w-full",
        ),
        cls="overflow-x-auto",
    )


def Definitions():
    return Div(
        H2("Definitions", cls="text-lg font-semibold mt-8 mb-2"),
        Dl(
            *[
                x
                for metric, text in DEFINITIONS.items()
                for x in (
                    Dt(METRICS[metric], cls="font-semibold"),
                    Dd(text, cls=TextPresets.muted_sm + " mb-2"),
                )
            ]
        ),
    )


@ar("/admin", methods=["GET"])
def admin_home():
    return AdminPage(
        "Admin",
        P(A("Pilot metrics", href="/admin/metrics"), cls="py-2"),
        P(A("Reports", href="/admin/reports"), cls="py-2"),
        P(A("Organizations", href="/admin/orgs"), cls="py-2"),
    )


@ar("/admin/metrics", methods=["GET"])
def admin_metrics():
    m = pilot_metrics(data.metrics_rows(), _now())
    return (
        AdminPage(
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
            *[
                MetricsTable(
                    title,
                    {"All": m["overall"]}
                    if kind == "overall"
                    else {label(m, kind, k): g for k, g in m[kind].items()},
                )
                for kind, title in BREAKDOWNS.items()
            ],
            Definitions(),
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
    subject = (
        f"{r['reporter']} (volunteer) reported {r['target']}"
        if r["by"] == "volunteer"
        else f"{r['reporter']} (coordinator) reported {r['target']}"
    )
    return Div(
        P(subject, cls="font-semibold"),
        P(
            f"{r['reason']} - match {r['match_id']}, {r['opening']} ({r['org']})",
            cls=TextPresets.muted_sm,
        ),
        P(r["details"] or "No details.", cls="whitespace-pre-wrap break-words"),
        Div(
            _post(f"/admin/reports/{r['id']}/dismiss", "Dismiss"),
            _post(f"/admin/reports/{r['id']}/suspend", "Suspend user")
            if r["target_user_id"]
            else _post(f"/admin/reports/{r['id']}/deactivate", "Deactivate org"),
            cls="flex gap-3 pt-2",
        ),
        cls="border rounded-lg p-3",
    )


@ar("/admin/reports", methods=["GET"])
def admin_reports(req):
    rows = data.admin_reports(req.scope["uid"])
    return (
        AdminPage(
            "Open reports",
            Div(
                *([ReportRow(r) for r in rows] or [P("No open reports.")]),
                cls="flex flex-col gap-3 max-w-2xl",
            ),
        ),
        NO_STORE_HEADER,
    )


def _resolve_with(fn):
    def run(req, report_id):
        try:
            fn(req.scope["uid"], report_id)
        except LookupError:
            pass  # already resolved by someone else
        except (ValueError, PermissionError):
            return bad_request()
        return Redirect("/admin/reports")

    return run


@ar("/admin/reports/{report_id}/dismiss", methods=["POST"])
def admin_dismiss(req, report_id: int):
    return _resolve_with(data.dismiss_report)(req, report_id)


@ar("/admin/reports/{report_id}/suspend", methods=["POST"])
def admin_suspend(req, report_id: int):
    return _resolve_with(data.suspend_target)(req, report_id)


@ar("/admin/reports/{report_id}/deactivate", methods=["POST"])
def admin_deactivate(req, report_id: int):
    return _resolve_with(data.deactivate_target_org)(req, report_id)


@ar("/admin/blocks", methods=["GET"])
def admin_blocks(req):
    rows = data.admin_blocks(req.scope["uid"])
    return (
        AdminPage(
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
                            cls="border rounded-lg p-3",
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
            Input(
                name="ein",
                value=o["ein"],
                placeholder="53-0196605",
                aria_label="EIN",
                required=True,
                cls="uk-input",
            ),
            PlainButton("Look up", type="submit", cls="uk-btn uk-btn-default"),
            action=f"/admin/orgs/{o['id']}/lookup",
            method="get",
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
        cls="border rounded-lg p-3",
    )


def OrgsPage(rows, only=None, found=None, error=None):
    return (
        AdminPage(
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
def admin_orgs():
    return OrgsPage(data.admin_orgs())


@ar("/admin/orgs/{org_id}/lookup", methods=["GET"])
async def admin_org_lookup(req, org_id: int):
    rows = data.admin_orgs()
    if org_id not in {o["id"] for o in rows}:
        return Response("Not found", status_code=404)
    try:
        found = await run_in_threadpool(propublica.lookup, query(req, "ein"))
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
        return bad_request()
    try:
        data.verify_org(org_id, ein)
    except LookupError:
        return Response("Not found", status_code=404)
    except ValueError as e:
        return OrgsPage(data.admin_orgs(), org_id, error=str(e))
    return Redirect("/admin/orgs")
