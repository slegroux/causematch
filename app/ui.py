from fasthtml.common import H1, A, Button, Div, Form, Option, P, Select
from monsterui.all import (
    AT,
    Alert,
    AlertT,
    Container,
    FormLabel,
    LabelCheckboxX,
    TextPresets,
)


def Page(title, *content, error=None, intro=None):
    return Container(
        H1(title, cls="text-2xl font-bold my-4"),
        P(intro, cls=TextPresets.muted_sm + " mb-4") if intro else None,
        Alert(error, cls=AlertT.error + " mb-4") if error else None,
        *content,
        cls="max-w-md pb-8",
    )


def Field(label, *inputs):
    return Div(FormLabel(label), *inputs, cls="space-y-2")


def Checks(name, options, checked=()):
    return Div(
        *[
            LabelCheckboxX(label, name=name, value=value, checked=value in checked)
            for value, label in options
        ],
        cls="space-y-1",
    )


def Choice(name, options, selected="", placeholder=None):
    return Select(
        Option(placeholder or "Choose one", value="", selected=not selected),
        *[
            Option(label, value=value, selected=value == selected)
            for value, label in options
        ],
        name=name,
        cls="uk-select h-10",
    )


def AddToHomeScreen():
    """Hidden until static/a2hs.js decides: iPhone Safari, in a tab, not dismissed."""
    return Div(
        P(
            "Add Causematch to your Home Screen: tap Share, then Add to Home Screen.",
            cls=TextPresets.muted_sm,
        ),
        Button("Dismiss", type="button", id="a2hs-close", cls="uk-btn uk-btn-default"),
        id="a2hs",
        hidden=True,
        cls="py-3",
    )


def VolunteerNav():
    return Div(
        AddToHomeScreen(),
        Div(
            A("Openings", href="/deck", cls=AT.muted),
            A("Matches", href="/matches", cls=AT.muted),
            Form(
                Button("Sign out", type="submit", cls=AT.muted + " bg-transparent p-0"),
                action="/logout",
                method="post",
            ),
            cls="flex gap-4 py-3 text-sm",
        ),
    )
