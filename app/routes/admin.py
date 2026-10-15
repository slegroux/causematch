import csv
import io

from fasthtml.common import (
    H2,
    A,
    APIRouter,
    Div,
    Form,
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
from app.metrics import BREAKDOWNS, METRICS, csv_rows, fmt, pilot_metrics

ar = APIRouter()


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
        cls="max-w-6xl pb-8",
    )


def cell(metric, c):
    counts = "" if c["d"] is None else f"{c['n']}/{c['d']}"
    if metric == "median_reply_h":
        counts = f"n={c['n']}"
    return Td(
        Div(fmt(metric, c), cls="font-semibold"),
        Div(counts, cls=TextPresets.muted_sm) if counts else None,
        cls="px-3 py-2",
    )


def MetricsTable(title, groups):
    return Div(
        H2(title, cls="text-lg font-semibold mt-6 mb-2"),
        Table(
            Thead(
                Tr(
                    Th("", cls="px-3 py-2 text-left"),
                    *[
                        Th(label, cls="px-3 py-2 text-left")
                        for label in METRICS.values()
                    ],
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


@ar("/admin", methods=["GET"])
def admin_home():
    return AdminPage(
        "Admin",
        P(A("Pilot metrics", href="/admin/metrics"), cls="py-2"),
    )


@ar("/admin/metrics", methods=["GET"])
def admin_metrics():
    m = pilot_metrics(data.metrics_rows())
    return AdminPage(
        "Pilot metrics",
        P(
            "Aggregates only. Each figure shows its counts beneath it, so a small "
            "denominator is easy to see. A dash means no data yet.",
            cls=TextPresets.muted_sm,
        ),
        *[
            MetricsTable(title, {"All": m["overall"]} if kind == "overall" else m[kind])
            for kind, title in BREAKDOWNS.items()
        ],
    )


@ar("/admin/metrics.csv", methods=["GET"])
def admin_metrics_csv():
    out = io.StringIO()
    csv.writer(out).writerows(csv_rows(pilot_metrics(data.metrics_rows())))
    return Response(
        out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="pilot-metrics.csv"'},
    )
