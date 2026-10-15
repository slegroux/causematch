"""/me/preferences: what a volunteer wants from the deck, editable at any time. Private:
nonprofits never see these answers."""

from fasthtml.common import (
    H1,
    H2,
    A,
    Div,
    Fieldset,
    Form,
    Input,
    Label,
    Legend,
    P,
    Redirect,
    Span,
)
from monsterui.all import Alert, AlertT, Container
from starlette.concurrency import run_in_threadpool

from app import data, prefs
from app.choices import AVAILABILITY, FREQUENCY, SKILLS
from app.forms import bad_request, query, str_form
from app.ui import (
    BACK,
    CARD,
    HEART,
    SHIELD,
    TITLE,
    BigButton,
    CauseChip,
    Icon,
    Router,
    VolunteerNav,
)

ar = Router()

UNRECOGNIZED = "Something in that form wasn't recognized. Please try again."
NO_CAUSE = "Pick at least one cause you love or are open to."

# A toggle chip: a visually hidden radio or checkbox, then the pill that shows its state.
CHIP = (
    "flex min-h-11 cursor-pointer select-none items-center justify-center gap-1.5 "
    "rounded-full border border-line bg-white px-3 py-2 text-center text-sm font-bold "
    "leading-tight text-ink transition hover:border-ink-muted "
    "peer-focus-visible:outline peer-focus-visible:outline-2 "
    "peer-focus-visible:outline-offset-2 peer-focus-visible:outline-brand"
)
CHIP_ON = (
    "peer-checked:border-brand peer-checked:bg-brand-soft peer-checked:text-brand-ink"
)
# Love and open in each cause's own color: solid, then its soft tint. Literal class
# strings, so Tailwind's scan of app/ finds them.
CAUSE_ON = {
    "food-insecurity": {
        prefs.LOVE: "peer-checked:border-cause-food peer-checked:bg-cause-food "
        "peer-checked:text-white",
        prefs.OPEN: "peer-checked:border-cause-food peer-checked:bg-cause-food-soft "
        "peer-checked:text-cause-food-ink",
    },
    "environment-parks": {
        prefs.LOVE: "peer-checked:border-cause-parks peer-checked:bg-cause-parks "
        "peer-checked:text-white",
        prefs.OPEN: "peer-checked:border-cause-parks peer-checked:bg-cause-parks-soft "
        "peer-checked:text-cause-parks-ink",
    },
    "animal-shelters": {
        prefs.LOVE: "peer-checked:border-cause-animals peer-checked:bg-cause-animals "
        "peer-checked:text-white",
        prefs.OPEN: "peer-checked:border-cause-animals "
        "peer-checked:bg-cause-animals-soft peer-checked:text-cause-animals-ink",
    },
    "homelessness-services": {
        prefs.LOVE: "peer-checked:border-cause-homeless peer-checked:bg-cause-homeless "
        "peer-checked:text-white",
        prefs.OPEN: "peer-checked:border-cause-homeless "
        "peer-checked:bg-cause-homeless-soft peer-checked:text-cause-homeless-ink",
    },
}
NOT_FOR_ME = (
    "peer-checked:border-ink-muted peer-checked:bg-sand peer-checked:text-ink-muted "
    "peer-checked:line-through"
)


def Chip(kind, name, value, text, checked, on=CHIP_ON, icon=None):
    """kind: radio | checkbox."""
    return Label(
        Input(
            type=kind,
            name=name,
            value=value,
            checked=checked,
            cls="peer sr-only " + ("uk-radio" if kind == "radio" else "uk-checkbox"),
        ),
        Span(icon, text, cls=f"{CHIP} {on}"),
        cls="relative block",
    )


def Section(title, hint, *content):
    return Div(
        H2(title, cls="font-display text-lg font-extrabold text-ink"),
        P(hint, cls="-mt-2 text-sm text-ink-muted") if hint else None,
        *content,
        cls="space-y-3 p-5 " + CARD,
    )


def CauseRow(cause, state):
    """One cause, rated love / open / not for me; no choice leaves it unrated."""
    on = CAUSE_ON.get(cause.slug, {})
    return Fieldset(
        Legend(CauseChip(cause.slug, cause.label), cls="mb-2"),
        Div(
            *[
                Chip(
                    "radio",
                    f"cause_{cause.slug}",
                    value,
                    text,
                    state == value,
                    NOT_FOR_ME if value == prefs.NO else on.get(value, CHIP_ON),
                    Icon(HEART, size=16, filled=True) if value == prefs.LOVE else None,
                )
                for value, text in prefs.CAUSE_STATES
            ],
            cls="grid grid-cols-3 gap-2",
        ),
    )


def Checks(name, options, checked, cls):
    return Div(
        *[Chip("checkbox", name, v, label, v in checked) for v, label in options],
        cls=cls,
    )


def PrefsPage(f, error=None, saved=False):
    return Container(
        VolunteerNav("me"),
        A(
            Icon(BACK, size=18),
            "Me",
            href="/profile",
            cls="-ml-1 inline-flex min-h-11 items-center gap-1 font-bold text-ink-muted "
            "hover:text-ink",
        ),
        H1("Preferences", cls=TITLE + " !mt-0"),
        P(
            Icon(SHIELD, size=16, cls="mt-0.5 text-ink-muted"),
            Span(
                "Only you see these. They decide which openings you see, and in what order."
            ),
            cls="-mt-2 mb-4 flex gap-2 text-sm text-ink-muted",
        ),
        Alert(error, cls=AlertT.error + " mb-4") if error else None,
        Alert(
            "Saved. Your deck now follows these preferences.",
            cls=AlertT.success + " mb-4",
        )
        if saved and not error
        else None,
        Form(
            Section(
                "Causes",
                "Love ranks first. Not for me hides every opening for that cause.",
                *[CauseRow(c, f["causes"].get(c.slug)) for c in data.active_causes()],
            ),
            Section(
                "When you're usually free",
                None,
                Checks(
                    "availability",
                    AVAILABILITY,
                    f["availability"],
                    "grid grid-cols-2 gap-2",
                ),
            ),
            Section(
                "How often",
                None,
                Div(
                    *[
                        Chip("radio", "frequency", v, label, f["frequency"] == v)
                        for v, label in FREQUENCY
                    ],
                    cls="flex flex-wrap gap-2",
                ),
            ),
            Section(
                "What you bring",
                "Openings that need it rank higher.",
                Checks("skills", SKILLS, f["skills"], "flex flex-wrap gap-2"),
            ),
            Div(BigButton("Save preferences"), cls="cm-savebar z-20 -mx-1 py-3"),
            action="/me/preferences",
            method="post",
            cls="space-y-4",
        ),
        cls="max-w-md pb-8",
    )


@ar("/me/preferences", methods=["GET"])
def preferences_page(req):
    return PrefsPage(
        data.preferences(req.scope["uid"]), saved=query(req, "saved") == "1"
    )


@ar("/me/preferences", methods=["POST"])
async def preferences_save(req):
    form = await str_form(req)
    if form is None:
        return bad_request()
    return await run_in_threadpool(_sync_save, req, form)


def _sync_save(req, form):
    uid = req.scope["uid"]
    active = [c.slug for c in data.active_causes()]
    rated = {s: form.get(f"cause_{s}") for s in active if form.get(f"cause_{s}")}
    f = {
        # a rating of a cause that is inactive now is kept for when it comes back
        "causes": {
            **{
                s: v
                for s, v in data.preferences(uid)["causes"].items()
                if s not in active
            },
            **rated,
        },
        "availability": form.getlist("availability"),
        "skills": form.getlist("skills"),
        "frequency": form.get("frequency", ""),
    }
    if (
        not set(rated.values()) <= {v for v, _ in prefs.CAUSE_STATES}
        or not set(f["availability"]) <= {v for v, _ in AVAILABILITY}
        or not set(f["skills"]) <= {v for v, _ in SKILLS}
        or f["frequency"] not in {v for v, _ in FREQUENCY} | {""}
    ):
        return PrefsPage(f, UNRECOGNIZED)
    if not {prefs.LOVE, prefs.OPEN} & set(rated.values()):
        return PrefsPage(f, NO_CAUSE)
    data.save_preferences(uid, **f)
    return Redirect("/me/preferences?saved=1")
