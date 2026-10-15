from fasthtml.common import H1, P
from monsterui.all import Alert, AlertT, Container, TextPresets


def Page(title, *content, error=None, intro=None):
    return Container(
        H1(title, cls="text-2xl font-bold my-4"),
        P(intro, cls=TextPresets.muted_sm + " mb-4") if intro else None,
        Alert(error, cls=AlertT.error + " mb-4") if error else None,
        *content,
        cls="max-w-md pb-8",
    )
