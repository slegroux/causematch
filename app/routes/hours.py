from datetime import datetime

from fasthtml.common import H1, H2, Div, Li, P, Span, Ul
from monsterui.all import TextPresets

from app import data
from app.choices import minutes_label
from app.ui import CARD, ORG_SECTION, TITLE, Router, VolunteerShell, when

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
                        f"{s['title']} - {minutes_label(s['minutes'])}",
                        cls="font-bold leading-snug text-ink",
                    ),
                    P(f"{when(s['starts_at'])} - {s['org']}", cls=TextPresets.muted_sm),
                    P(f"Confirmed by {s['by']}", cls=TextPresets.muted_sm),
                    cls="space-y-1 py-3",
                )
                for s in m["shifts"]
            ),
            cls="divide-y divide-line",
        ),
        cls="w-full space-y-2 p-4 " + CARD,
    )


@ar("/hours", methods=["GET"])
def hours_page(req):
    mine = data.my_hours(req.scope["uid"])
    left = mine["unconfirmed"]
    return VolunteerShell(
        "me",
        H1("My hours", cls=TITLE),
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
            H2("Hours not confirmed", cls=ORG_SECTION),
            P(
                f"{len(left)} earlier shift{' has' if len(left) == 1 else 's have'} no "
                "confirmed hours, so not counted above:",
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
