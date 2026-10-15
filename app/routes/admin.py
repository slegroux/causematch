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
    P,
    Response,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Tr,
)
from fasthtml.common import Button as PlainButton
from monsterui.all import AT, Container, TextPresets

from app import data
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
