"""Uploaded pictures: decoded by Pillow, re-encoded as plain JPEG, never served as sent.

Re-encoding is the privacy step: the output is a fresh image holding only pixels, so EXIF
(GPS, camera serial), comments and colour profiles stay behind. It is also the safety step:
whatever a browser is later given is a JPEG Pillow wrote, not the uploader's bytes.
"""

import io
import math
import secrets
import threading
import warnings
from collections import namedtuple

from PIL import Image, ImageChops, ImageFilter, ImageOps, UnidentifiedImageError

MAX_UPLOAD = 6 * 1024 * 1024  # bytes; static/photo.js sends far less (1600 px JPEG)
# A decompression bomb is a small file that decodes to a huge bitmap: a 2 KB WebP can
# claim 7000 x 7000 pixels. _open reads the size from the header and refuses past these
# before decoding anything. A JPEG decodes at 1/2 to 1/8 scale (draft), so a 48 MP phone
# photo is fine; PNG and GIF decode at full size, 4 bytes a pixel; WebP's decoder holds
# about 18 bytes a pixel at its peak. Each cap keeps one decode under about 200 MB.
MAX_PIXELS_JPEG = 50_000_000
MAX_PIXELS = {"JPEG": MAX_PIXELS_JPEG, "MPO": MAX_PIXELS_JPEG, "WEBP": 8_000_000}
MAX_PIXELS_OTHER = 12_000_000  # PNG, GIF
# draft() doesn't help a JPEG of several scans (progressive, or one scan per component):
# libjpeg keeps every DCT coefficient of the whole image, 2 bytes each, until the last
# scan. Such a file may have at most this many pixels x components (about 72 MB).
MAX_COEFFICIENTS = 36_000_000
# Each scan is another pass over the whole image: a file of hundreds of tiny repeated
# scans keeps a decode slot busy for minutes. A real progressive JPEG has about 10.
MAX_SCANS = 50
# Pillow's own check stays as a backstop (an error at twice this); its warning between 1x
# and 2x is redundant with _open's refusal, so it is silenced once, here.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS_JPEG
warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)
# JPEG includes MPO, an iPhone's multi-picture JPEG
FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
Image.init()  # register every plugin, so open(formats=FORMATS) knows WEBP
FULL = 800  # px, the longest side of a volunteer photo
CARD = (960, 640)  # px, an org's or opening's photo: cut to the swipe card's 3:2
LOGO = 320  # px, square: the logo centred on its background, to show as a round badge
THUMB = 160  # px, square
# A decoded picture shrinks at once to at most this many pixels (16 MB as RGBA), keeping
# its shape: enough for every output (a 960 x 640 card cut from a panorama, too).
WORK_PIXELS = 4_000_000
# At most two pictures are being decoded at a time, whatever the thread pool's size: a
# decode is the one moment a full-size bitmap is in memory.
_decoding = threading.BoundedSemaphore(2)
WAIT = 5  # seconds an upload waits for a decode slot, then Busy
QUALITY = 82
# static/theme.css --cm-ink: behind a logo drawn for dark backgrounds
INK = (45, 30, 23)

Processed = namedtuple("Processed", "full thumb")


class BadPhoto(ValueError):
    """Not a picture Pillow can read, or too big to decode safely."""


class Busy(Exception):
    """Both decode slots stayed taken for WAIT seconds: try again in a moment."""


def new_key():
    """22 URL-safe random characters: a photo's id and its URL."""
    return secrets.token_urlsafe(16)


def is_key(key):
    return (
        isinstance(key, str)
        and len(key) == 22
        and all(c.isalnum() or c in "-_" for c in key)
    )


# The JPEG frames libjpeg decodes here: baseline, extended and progressive Huffman. Not
# lossless (SOF3: no draft scaling, 4 bytes a sample), hierarchical or arithmetic ones.
SOF_OK = (0xC0, 0xC1, 0xC2)
SOF_ANY = {*range(0xC0, 0xD0)} - {0xC4, 0xC8, 0xCC}  # 0xC4 DHT, 0xC8 JPG, 0xCC DAC
JpegFrame = namedtuple("JpegFrame", "sof scans first_ns")


def jpeg_frame(raw):
    """(the first SOF marker, the number of scans, the components in the first scan) of
    a JPEG's top-level marker stream, counting at most MAX_SCANS + 1 scans. Segments are
    skipped by their length, so a thumbnail inside EXIF doesn't count; entropy-coded data
    and stray bytes are skipped up to the next real marker, the way libjpeg reads them."""
    i, n, sof, scans, first_ns = 2, len(raw), None, 0, 0
    while i + 4 <= n and scans <= MAX_SCANS:
        if raw[i] != 0xFF:  # stray bytes: libjpeg skips to the next marker, so do we
            if (i := raw.find(b"\xff", i)) == -1:
                break
            continue
        marker = raw[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xD9:  # end of image
            break
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # no length
            i += 2
            continue
        if marker in SOF_ANY and sof is None:
            sof = marker
        if marker == 0xDA and scans == 0 and i + 4 < n:
            first_ns = raw[i + 4]  # FF DA, a 2-byte length, then Ns
        i += 2 + int.from_bytes(raw[i + 2 : i + 4], "big")
        if marker != 0xDA:
            continue
        scans += 1
        while (j := raw.find(b"\xff", i)) != -1 and j + 1 < n:
            nxt = raw[j + 1]
            if nxt != 0 and not 0xD0 <= nxt <= 0xD7:  # a marker, not stuffing or RSTn
                break
            i = j + 2
        else:
            break
        i = j
    return JpegFrame(sof, scans, first_ns)


def _check(raw):
    """(width, height, a JPEG?) from the header alone, or BadPhoto for a picture with
    more pixels, components or scans than may be decoded safely. Nothing is decoded, so
    this runs before taking a decode slot."""
    try:
        with Image.open(io.BytesIO(raw), formats=FORMATS) as probe:
            w, h = probe.size
            jpeg = probe.format in ("JPEG", "MPO")
            if w * h > MAX_PIXELS.get(probe.format, MAX_PIXELS_OTHER):
                raise BadPhoto("too many pixels")
            if jpeg:
                frame = jpeg_frame(raw)
                if frame.sof not in SOF_OK:
                    raise BadPhoto("an unsupported kind of JPEG")
                if frame.scans > MAX_SCANS:
                    raise BadPhoto("too many scans")
                # libjpeg keeps a whole-image coefficient buffer when the frame is
                # progressive or a scan covers fewer than all components
                bands = len(probe.getbands())
                multi = (
                    frame.sof == 0xC2
                    or frame.scans > 1
                    or frame.first_ns < bands
                    or bool(probe.info.get("progressive"))
                )
                if multi and w * h * bands > MAX_COEFFICIENTS:
                    raise BadPhoto("too many coefficients for a multi-scan JPEG")
            probe.verify()  # structure only; a verified image can't be used after
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,  # BadPhoto too
        EOFError,
    ) as e:
        if isinstance(e, BadPhoto):
            raise
        raise BadPhoto("not a readable picture") from e
    return w, h, jpeg


def _open(raw):
    """The decoded image, upright, at most WORK_PIXELS; BadPhoto for anything _check
    refuses or Pillow can't read; Busy when no decode slot frees up within WAIT seconds."""
    w, h, jpeg = _check(raw)
    if not _decoding.acquire(timeout=WAIT):
        raise Busy("both decode slots are taken")
    try:
        scale = min(1, (WORK_PIXELS / (w * h)) ** 0.5)
        size = (max(1, math.ceil(w * scale)), max(1, math.ceil(h * scale)))
        im = Image.open(io.BytesIO(raw), formats=FORMATS)
        if jpeg:
            im.draft("RGB", size)  # decodes straight to a smaller scale
        im.load()
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")  # palette, grey, 1-bit: before resampling
        im.thumbnail(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
        return ImageOps.exif_transpose(im)
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        EOFError,
    ) as e:
        raise BadPhoto("not a readable picture") from e
    finally:
        _decoding.release()


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
