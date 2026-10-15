from fasthtml.common import H1, Div, Option, P, Select
from monsterui.all import (
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
