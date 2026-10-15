from datetime import datetime

from fasthtml.common import H1, APIRouter, Div, P, Redirect
from monsterui.all import (
    Card,
    CardTitle,
    Container,
    DivFullySpaced,
    DivLAligned,
    Label,
    TextPresets,
)

from app import data

ar = APIRouter()


def when(starts_at):
    return datetime.fromisoformat(starts_at).strftime("%a %b %-d, %-I:%M %p")


def OpeningCard(c):
    return Card(
        DivLAligned(*[Label(name) for name in c["causes"]], cls="flex-wrap gap-1"),
        P(c["org"], cls=TextPresets.muted_sm),
        DivFullySpaced(P(when(c["starts_at"])), P(c["neighborhood"])),
        P(f"{c['slots']} spots", cls=TextPresets.muted_sm),
        header=CardTitle(c["title"]),
        cls="w-full",
    )


@ar("/")
def index():
    return Redirect("/deck")


@ar("/deck")
def deck():
    return Container(
        H1("Openings near you", cls="text-2xl font-bold my-4"),
        Div(*[OpeningCard(c) for c in data.deck()], cls="flex flex-col gap-4 pb-8"),
        cls="max-w-md",
    )
