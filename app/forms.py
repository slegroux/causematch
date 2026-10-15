"""Request input, read explicitly.

FastHTML binds a handler parameter from the path, then the cookies, then headers and the
query, and only then the body. A cookie named like a field would override what the user
typed, so a handler takes user input from here (or from the path), never from a parameter.
"""

import unicodedata

from fasthtml.common import Response


async def str_form(req):
    """The submitted form, or None when any field is a file part."""
    form = await req.form()
    if any(not isinstance(v, str) for _, v in form.multi_items()):
        return None
    return form


def bad_request():
    return Response("Bad request", status_code=400)


def query(req, name):
    """One query-string value, "" when absent."""
    return req.query_params.get(name, "")


# Characters a typed answer never needs: controls, format marks (zero-width, bidi
# overrides), private-use, unassigned and surrogate code points, line and paragraph
# separators, and the "filler" letters that render as blank space.
HIDDEN_CATEGORIES = {"Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp"}
FILLERS = {"ᅟ", "ᅠ", "ㅤ", "ﾠ", "⠀"}


def hidden_chars(text, multiline=False):
    """True when text holds a character that hides or disguises what it says.
    multiline allows line breaks and tabs."""
    allowed = {"\n", "\r", "\t"} if multiline else set()
    return any(
        ch not in allowed
        and (unicodedata.category(ch) in HIDDEN_CATEGORIES or ch in FILLERS)
        for ch in text
    )


HIDDEN_CHARS_MESSAGE = "Please remove hidden or special characters from your answers."
