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
FILLERS = {"\u115f", "\u1160", "\u3164", "\uffa0", "\u2800"}
ZWJ = "\u200d"  # joins emoji in multi-line text; nowhere else
MAX_MARKS = 3  # combining marks in a row: "Nguyễn" has 2, "Zalgo" text has dozens


def hidden_chars(text, multiline=False):
    """True when text holds a character that hides or disguises what it says.

    multiline allows tabs and line breaks, and a zero-width joiner between two
    characters. More than MAX_MARKS combining marks in a row also counts."""
    marks = 0
    for i, ch in enumerate(text):
        cat = unicodedata.category(ch)
        marks = marks + 1 if cat in ("Mn", "Me") else 0
        if marks > MAX_MARKS or ch in FILLERS:
            return True
        if cat not in HIDDEN_CATEGORIES:
            continue
        if multiline and ch in "\t\n\r":
            continue
        if multiline and ch == ZWJ and 0 < i < len(text) - 1:
            continue
        return True
    return False


HIDDEN_CHARS_MESSAGE = "Please remove hidden or special characters from your answers."
