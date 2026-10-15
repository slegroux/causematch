"""Request input, read explicitly.

FastHTML binds a handler parameter from the path, then the cookies, then headers and the
query, and only then the body. A cookie named like a field would override what the user
typed, so a handler takes user input from here (or from the path), never from a parameter.
"""

import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

MAX_URL = 500


async def str_form(req):
    """The submitted form, or None when any field is a file part."""
    form = await req.form()
    if any(not isinstance(v, str) for _, v in form.multi_items()):
        return None
    return form


def bad_request():
    """A 400 in the shared look (app/ui.py Refusal), not a bare word."""
    from app.ui import Refusal  # imported here: app.ui imports this module

    return Refusal(400)


def query(req, name):
    """One query-string value, "" when absent."""
    return req.query_params.get(name, "")


# Characters that reorder how text is drawn: they let "good.org" display for another host
BIDI = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
# Characters that show as nothing (Hangul and braille fillers are letters or symbols to
# Unicode, so their category doesn't catch them)
FILLERS = frozenset("\u115f\u1160\u3164\uffa0\u2800")
# Format, control, private-use, unassigned and surrogate code points, and the line and
# paragraph separators: none belongs in a name or a bio
HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp"})
ZWJ = "\u200d"
EMOJI = ("So", "Sk")  # a symbol, or a skin-tone modifier, which a joiner may follow
KEYCAPS = frozenset("0123456789#*")
KEYCAP = "\u20e3"  # combining enclosing keycap: 1️⃣ is "1", U+FE0F, U+20E3
EMOJI_STYLE = "\ufe0f"
# Marks that draw nothing: the combining grapheme joiner, Khmer and Mongolian ones,
# variation selectors (U+FE0F is EMOJI_STYLE, checked on its own)
IGNORABLE_MARKS = re.compile(
    "[\u034f\u17b4\u17b5\u180b-\u180f\ufe00-\ufe0e\U000e0100-\U000e01ef]"
)
MAX_MARKS = 3  # combining marks on one character; more is "Zalgo" text
HOST_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")


def hidden_chars(text, multiline=False):
    """True when `text` holds a character that would hide, reorder or break what is
    shown: a format or control character (bidi controls, zero-width spaces, tags), a
    private-use or unassigned code point, a filler, a line separator, a mark that is
    invisible by default (IGNORABLE_MARKS) or has no base character before it, or more
    than MAX_MARKS combining marks on one character. Multiline text may hold line breaks
    and tabs, and a zero-width joiner inside an emoji sequence: after an emoji, a skin
    tone or U+FE0F, and before an emoji (the rainbow flag, a family, a person at a
    laptop), never between letters. The emoji variation selector U+FE0F is allowed only
    right after an emoji (category So) or in a keycap (1, #, * then U+FE0F U+20E3)."""
    marks = 0
    for i, c in enumerate(text):
        category = unicodedata.category(c)
        if c in "\n\r\t":
            if not multiline:
                return True
        elif c == ZWJ:
            before = text[i - 1] if i else ""
            after = text[i + 1] if i + 1 < len(text) else ""
            joins = (
                before
                and after
                and (before == EMOJI_STYLE or unicodedata.category(before) in EMOJI)
                and unicodedata.category(after) == "So"
            )
            if not (multiline and joins):
                return True
            continue
        elif c == EMOJI_STYLE:
            before = text[i - 1] if i else ""
            keycap = before in KEYCAPS and text[i + 1 : i + 2] == KEYCAP
            if not before or not (unicodedata.category(before) == "So" or keycap):
                return True
            continue
        elif IGNORABLE_MARKS.match(c) or category in HIDDEN_CATEGORIES or c in FILLERS:
            return True
        if category in ("Mn", "Me"):
            if i == 0 or text[i - 1].isspace():
                return True  # a mark needs a character to sit on
            marks += 1
            if marks > MAX_MARKS:
                return True
        else:
            marks = 0
    return False


HIDDEN_CHARS_MESSAGE = "Please remove hidden or special characters from your answers."


def https_url(raw):
    """A link someone typed, as a plain https:// address with a host and no user name or
    password, its host in ASCII (an international name as punycode, xn--..., so a
    look-alike letter can't pass for a familiar domain); else None. Never a javascript:,
    data: or http: URL, a backslash (browsers read it as a slash) or a bidi control."""
    url = (raw or "").strip()
    if (
        not url
        or len(url) > MAX_URL
        or "\\" in url
        or re.search(r"[\s\x00-\x1f\x7f]", url)
        or BIDI.search(url)
    ):
        return None
    try:
        u = urlsplit(url)
        port = u.port  # raises ValueError on a malformed port
        if "%" in u.netloc:  # good.org%2eevil.com opens good.org.evil.com
            return None
        host = (u.hostname or "").encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError):
        return None
    labels = host.split(".")
    if (
        u.scheme != "https"
        or u.username
        or u.password
        or len(labels) < 2
        or not all(len(x) <= 63 and HOST_LABEL.fullmatch(x) for x in labels)
    ):
        return None
    url = urlunsplit(u._replace(netloc=host + (f":{port}" if port else "")))
    return url if len(url) <= MAX_URL else None  # punycode can make it longer


def domain(url):
    """example.org for https://www.example.org/volunteer: what a link says it opens."""
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")
