from fasthtml.common import (
    H1,
    A,
    Button,
    Div,
    Form,
    Input,
    Option,
    P,
    Select,
    Span,
)
from fasthtml.svg import Path, Rect, Svg
from monsterui.all import (
    AT,
    Alert,
    AlertT,
    Container,
    FormLabel,
    LabelCheckboxX,
    TextPresets,
)

from app.config import demo_banner

# Each cause's chip: its soft tint with its ink (static/theme.css). Literal class strings,
# so Tailwind's scan of app/ finds them.
CAUSE_CHIP = {
    "food-insecurity": "bg-cause-food-soft text-cause-food-ink",
    "environment-parks": "bg-cause-parks-soft text-cause-parks-ink",
    "animal-shelters": "bg-cause-animals-soft text-cause-animals-ink",
    "homelessness-services": "bg-cause-homeless-soft text-cause-homeless-ink",
}
DEMO_NOTICE = "Demo: sample openings, not affiliated with these organizations"


def PrivacyFooter():
    return P(
        A("Privacy", href="/privacy", cls=AT.muted), cls=TextPresets.muted_sm + " pt-8"
    )


def Page(title, *content, error=None, intro=None):
    return Container(
        H1(title, cls="text-2xl font-bold my-4"),
        P(intro, cls=TextPresets.muted_sm + " mb-4") if intro else None,
        Alert(error, cls=AlertT.error + " mb-4") if error else None,
        *content,
        PrivacyFooter(),
        cls="max-w-md pb-8",
    )


def Field(label, *inputs):
    return Div(FormLabel(label), *inputs, cls="space-y-2")


def TextInput(**kwargs):
    """Every text-like <input> (text, tel, email, number, date, datetime-local): Franken's
    bordered field, so no form reads as a bare label."""
    return Input(cls=f"uk-input {kwargs.pop('cls', '')}".strip(), **kwargs)


def BigButton(text):
    """The one main action of a short form: full width and easy to hit with a thumb."""
    return Button(
        text, type="submit", cls="uk-btn uk-btn-primary w-full h-12 text-lg shadow-warm"
    )


def CauseChip(slug, label):
    return Span(
        label,
        cls="inline-flex items-center rounded-full px-3 py-1 text-sm font-bold "
        + CAUSE_CHIP.get(slug, "bg-sand text-ink"),
    )


def Wordmark():
    """A heart on a terracotta tile, then "causematch"."""
    return Div(
        Svg(
            Rect(width="32", height="32", rx="10", fill="currentColor"),
            Path(
                d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2"
                "-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z",
                fill="#fff",
                transform="translate(6 7) scale(0.833)",
            ),
            viewBox="0 0 32 32",
            width="36",
            height="36",
            aria_hidden="true",
            cls="text-brand",
        ),
        Span(
            "cause",
            Span("match", cls="text-brand"),
            cls="font-display text-3xl font-extrabold tracking-tight text-ink",
        ),
        cls="flex items-center gap-2",
    )


def SignInPage(title, *content, error=None, intro=None, aside=None):
    """A sign-in step: the wordmark, then one warm card holding the form."""
    return Div(
        Div(Wordmark(), cls="flex justify-center pt-10 pb-6"),
        Div(
            H1(title, cls="font-display text-2xl font-extrabold text-ink"),
            P(intro, cls="text-ink-muted mt-1") if intro else None,
            Alert(error, cls=AlertT.error + " mt-4") if error else None,
            Div(*content, cls="mt-6 space-y-4"),
            cls="uk-card uk-card-body p-6",
        ),
        aside,
        Div(PrivacyFooter(), cls="text-center"),
        cls="mx-auto max-w-md px-4 pb-10",
    )


def DemoBanner():
    return P(
        DEMO_NOTICE,
        role="note",
        cls="bg-ink text-cream text-center text-xs font-semibold px-4 py-1.5",
    )


def body_wrap(content):
    """Every full page (main.py's fast_app): the demo banner first, when it is on."""
    return (DemoBanner(), content) if demo_banner() else content


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
            A("History", href="/history", cls=AT.muted),
            A("Profile", href="/profile", cls=AT.muted),
            Form(
                Button("Sign out", type="submit", cls=AT.muted + " bg-transparent p-0"),
                action="/logout",
                method="post",
            ),
            cls="flex gap-4 py-3 text-sm",
        ),
    )
