"""Uploaded pictures: decoded by Pillow, re-encoded as plain JPEG, never served as sent.

Re-encoding is the privacy step: the output is a fresh image holding only pixels, so EXIF
(GPS, camera serial), comments and colour profiles stay behind. It is also the safety step:
whatever a browser is later given is a JPEG Pillow wrote, not the uploader's bytes.
"""

import io
import secrets
import warnings
from collections import namedtuple

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD = 6 * 1024 * 1024  # bytes; static/photo.js sends far less (1600 px JPEG)
# A decompression bomb is a small file that decodes to a huge bitmap. Pillow warns past
# this many pixels and refuses at twice it; open() below turns the warning into a refusal.
Image.MAX_IMAGE_PIXELS = 50_000_000  # a 48 MP phone photo still opens
# JPEG includes MPO, an iPhone's multi-picture JPEG
FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
Image.init()  # register every plugin, so open(formats=FORMATS) knows WEBP
FULL = 800  # px, the longest side of a volunteer photo
THUMB = 160  # px, square
QUALITY = 82

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


def process(raw):
    """A volunteer's photo: the whole picture at most FULL px on its longest side, and a
    THUMB px square crop. BadPhoto when it isn't a picture or is too big to decode."""
    if len(raw) > MAX_UPLOAD:
        raise BadPhoto("too large")
    im = _flat(_open(raw))
    full = im.copy()
    full.thumbnail((FULL, FULL), Image.Resampling.LANCZOS)
    return Processed(_jpeg(full), _jpeg(_square(im, THUMB)))
