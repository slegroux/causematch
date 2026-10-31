from datetime import datetime

from fasthtml.common import H2, Div, Li, P, Redirect, Span, Ul
from monsterui.all import TextPresets

from app import data
from app.choices import minutes_label
from app.ui import CARD, ORG_SECTION, Router, when

ar = Router()


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
                        f"{s['title']} · {minutes_label(s['minutes'])}",
                        cls="font-bold leading-snug text-ink",
                    ),
                    P(
                        f"{when(s['starts_at'], s['duration_minutes'])} · {s['org']}",
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


def HoursSection(uid):
    """The Hours part of /history: confirmed hours by month, then earlier shifts
    with none."""
    mine = data.my_hours(uid)
    left = mine["unconfirmed"]
    return Div(
        H2("Hours", cls=ORG_SECTION),
        P(
            "Hours a nonprofit confirmed when it marked you as attended.",
            cls=TextPresets.muted_sm,
        ),
        *(MonthCard(m) for m in mine["months"]),
        *(
            []
            if mine["months"]
            else [P("No confirmed hours yet.", cls=TextPresets.muted_sm)]
        ),
        Div(
            H2("Hours not confirmed", cls=ORG_SECTION),
            P(
                f"{len(left)} earlier shift{' has' if len(left) == 1 else 's have'} no "
                "confirmed hours, so they count toward nothing:",
                cls=TextPresets.muted_sm,
            ),
            Ul(
                *(
                    Li(f"{s['title']} · {s['org']}, {when(s['starts_at'])}")
                    for s in left
                ),
                cls="uk-list uk-list-disc text-sm text-ink-muted",
            ),
            cls="space-y-2 pt-3",
        )
        if left
        else None,
        cls="flex flex-col gap-3 pb-6",
    )


@ar("/hours", methods=["GET"])
def hours_page(req):
    """Old links and texted links: the hours moved into /history."""
    return Redirect("/history")
