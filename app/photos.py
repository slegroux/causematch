"""Uploaded pictures: decoded by Pillow, re-encoded as plain JPEG, never served as sent.

Re-encoding is the privacy step: the output is a fresh image holding only pixels, so EXIF
(GPS, camera serial), comments and colour profiles stay behind. It is also the safety step:
whatever a browser is later given is a JPEG Pillow wrote, not the uploader's bytes.
"""

import io
import secrets
import warnings
from collections import namedtuple

from PIL import Image, ImageChops, ImageFilter, ImageOps, UnidentifiedImageError

MAX_UPLOAD = 6 * 1024 * 1024  # bytes; static/photo.js sends far less (1600 px JPEG)
# A decompression bomb is a small file that decodes to a huge bitmap. Pillow warns past
# this many pixels and refuses at twice it; open() below turns the warning into a refusal.
Image.MAX_IMAGE_PIXELS = 50_000_000  # a 48 MP phone photo still opens
# JPEG includes MPO, an iPhone's multi-picture JPEG
FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
Image.init()  # register every plugin, so open(formats=FORMATS) knows WEBP
FULL = 800  # px, the longest side of a volunteer photo
CARD = (960, 640)  # px, an org's or opening's photo: cut to the swipe card's 3:2
LOGO = 320  # px, square: the logo centred on its background, to show as a round badge
THUMB = 160  # px, square
QUALITY = 82
INK = (
    45,
    30,
    23,
)  # static/theme.css --cm-ink: behind a logo drawn for dark backgrounds

Processed = namedtuple("Processed", "full thumb")


class BadPhoto(ValueError):
    """Not a picture Pillow can read, or too big to decode safely."""


def new_key():
    """22 URL-safe random characters: a photo's id and its URL."""
    return secrets.token_urlsafe(16)


def is_key(key):
    return (
        isinstance(key, str)
        and len(key) == 22
        and all(c.isalnum() or c in "-_" for c in key)
    )


def _open(raw):
    """The decoded image, upright; BadPhoto for anything else."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw), formats=FORMATS) as probe:
                probe.verify()  # structure only; a verified image can't be used after
            im = Image.open(io.BytesIO(raw), formats=FORMATS)
            if im.format in ("JPEG", "MPO"):
                # a big JPEG decodes straight to a smaller scale: less memory and time
                im.draft("RGB", (FULL * 2, FULL * 2))
            im.load()
            return ImageOps.exif_transpose(im)
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        EOFError,
    ) as e:
        raise BadPhoto("not a readable picture") from e


def _flat(im, background=(255, 255, 255)):
    """RGB pixels only, on `background` where the picture is transparent."""
    im = im.convert("RGBA")
    out = Image.new("RGB", im.size, background)
    out.paste(im, mask=im.getchannel("A"))
    return out


def _jpeg(im):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=QUALITY, optimize=True, progressive=True)
    return buf.getvalue()


def _square(im, size):
    return ImageOps.fit(im, (size, size), Image.Resampling.LANCZOS)


def _card(im):
    """Centre-cut to 3:2, at most CARD; never enlarged past the picture's own pixels."""
    w = min(CARD[0], im.width, round(im.height * 3 / 2))
    return ImageOps.fit(im, (w, round(w * 2 / 3)), Image.Resampling.LANCZOS)


def _logo_background(im):
    """White, unless the logo is drawn to sit on something dark: where a transparent
    logo's outline meets the transparency, it is mostly near white (a white wordmark), so
    it gets the ink colour. White inside a coloured tile doesn't count. A logo with no
    real transparency keeps the colour of its own corner, so the badge continues it."""
    alpha = im.getchannel("A")
    if alpha.getextrema()[0] >= 200:
        return im.getpixel((0, 0))[:3]
    solid = alpha.point(lambda a: 255 if a > 128 else 0)
    # solid pixels next to a transparent one
    edge = ImageChops.multiply(
        solid, ImageChops.invert(solid).filter(ImageFilter.MaxFilter(3))
    )
    light = im.convert("L").point(lambda v: 255 if v > 215 else 0)
    edges = edge.histogram()[255]
    light_edges = ImageChops.multiply(edge, light).histogram()[255]
    return INK if edges and light_edges / edges > 0.3 else (255, 255, 255)


def _logo(im):
    """The logo trimmed of empty margins, centred on a LOGO px square of its background,
    small enough that a circle cut from the square keeps all of it."""
    im = im.convert("RGBA")
    background = _logo_background(im)
    flat = _flat(im, background)
    empty = Image.new("RGB", flat.size, background)
    box = (
        ImageChops.difference(flat, empty)
        .convert("L")
        .point(lambda v: 255 if v > 24 else 0)
        .getbbox()
    )
    if box:
        flat = flat.crop(box)
    scale = 0.86 * LOGO / (flat.width**2 + flat.height**2) ** 0.5
    flat = flat.resize(
        (max(1, round(flat.width * scale)), max(1, round(flat.height * scale))),
        Image.Resampling.LANCZOS,
    )
    out = Image.new("RGB", (LOGO, LOGO), background)
    out.paste(flat, ((LOGO - flat.width) // 2, (LOGO - flat.height) // 2))
    return out


def process(raw, kind="volunteer"):
    """(full, thumb) JPEGs of an uploaded picture, by what it is for:

    - volunteer: the whole picture at most FULL px on its longest side;
    - card: an org's or opening's photo, centre-cut to the swipe card's 3:2 (CARD);
    - logo: on a LOGO px square of its own background (white, or ink for a white logo).

    The thumb is a THUMB px square. BadPhoto when it isn't a picture or is too big to
    decode."""
    if len(raw) > MAX_UPLOAD:
        raise BadPhoto("too large")
    if kind == "logo":
        logo = _logo(_open(raw))
        return Processed(_jpeg(logo), _jpeg(_square(logo, THUMB)))
    im = _flat(_open(raw))
    if kind == "card":
        return Processed(_jpeg(_card(im)), _jpeg(_square(im, THUMB)))
    full = im.copy()
    full.thumbnail((FULL, FULL), Image.Resampling.LANCZOS)
    return Processed(_jpeg(full), _jpeg(_square(im, THUMB)))
