"""Uploaded pictures: decoded by Pillow, re-encoded as plain JPEG, never served as sent.

Re-encoding is the privacy step: the output is a fresh image holding only pixels, so EXIF
(GPS, camera serial), comments and colour profiles stay behind. It is also the safety step:
whatever a browser is later given is a JPEG Pillow wrote, not the uploader's bytes.
"""

import io
import logging
import math
import secrets
import signal
import subprocess
import sys
import threading
import warnings
from collections import namedtuple
from pathlib import Path

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
PARENT_FORMATS = ("JPEG", "PNG", "GIF")  # _check never opens a WebP with Pillow
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
log = logging.getLogger("causematch")


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
    """(the SOF marker, the number of scans, the components in the first scan) of a
    JPEG, counting at most MAX_SCANS + 1 scans; BadPhoto for any stream this can't read
    exactly as libjpeg does. Fail closed: between segments only a marker may follow
    (fill bytes allowed), so no stray bytes and no FF 00, and one SOF before any scan.
    Inside entropy-coded data, as in libjpeg, FF* 00 is data, FF* D0-D7 a restart, and
    any other FF* xx the next marker. A thumbnail inside EXIF is skipped by its length."""
    n = len(raw)
    if raw[:2] != b"\xff\xd8":
        raise BadPhoto("not a JPEG")
    i, sof, scans, first_ns = 2, None, 0, 0
    while True:
        if i >= n or raw[i] != 0xFF:
            raise BadPhoto("bytes where a JPEG marker should be")
        while i < n and raw[i] == 0xFF:  # fill bytes before the marker
            i += 1
        if i >= n:
            raise BadPhoto("a JPEG cut short")
        marker = raw[i]
        i += 1
        if marker == 0xD9:  # end of image: anything after it is never read
            break
        if marker == 0x00 or 0xD0 <= marker <= 0xD8 or marker == 0x01:
            raise BadPhoto("a JPEG marker out of place")
        if i + 2 > n or (length := int.from_bytes(raw[i : i + 2], "big")) < 2:
            raise BadPhoto("a JPEG segment cut short")
        end = i + length
        if end > n:
            raise BadPhoto("a JPEG segment cut short")
        if marker in SOF_ANY:
            if sof is not None:
                raise BadPhoto("two JPEG frames")
            sof = marker
        if marker == 0xDA:
            if sof is None or length < 3:
                raise BadPhoto("a JPEG scan before its frame")
            scans += 1
            if scans == 1:
                first_ns = raw[i + 2]
            if scans > MAX_SCANS:
                break
            i = _entropy_end(raw, end)
        else:
            i = end
    return JpegFrame(sof, scans, first_ns)


def _entropy_end(raw, i):
    """Where a scan's entropy-coded data starting at i ends: the first FF of the next
    marker. FF FF ... 00 is a stuffed FF and FF FF ... D0-D7 a restart, as libjpeg reads
    them, so a run of FFs can't hide a marker."""
    n = len(raw)
    while (j := raw.find(b"\xff", i)) != -1:
        k = j + 1
        while k < n and raw[k] == 0xFF:
            k += 1
        if k >= n:
            break
        if raw[k] != 0x00 and not 0xD0 <= raw[k] <= 0xD7:
            return j
        i = k + 1
    raise BadPhoto("a JPEG cut short")


def webp_size(raw):
    """(width, height) of a WebP from its RIFF header, read here in Python, so the app
    process never runs libwebp on an upload (the decode runs it, in the child); BadPhoto
    for a header this can't read, or an animated WebP."""
    if len(raw) < 30 or raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
        raise BadPhoto("not a WebP")
    kind, data = raw[12:16], raw[20:30]
    if kind == b"VP8 ":  # lossy: a 3-byte frame tag, the start code, then 14-bit sizes
        if data[3:6] != b"\x9d\x01\x2a":
            raise BadPhoto("not a WebP")
        w = int.from_bytes(data[6:8], "little") & 0x3FFF
        h = int.from_bytes(data[8:10], "little") & 0x3FFF
    elif kind == b"VP8L":  # lossless: a signature, then 14-bit sizes minus one
        if data[0] != 0x2F:
            raise BadPhoto("not a WebP")
        bits = int.from_bytes(data[1:5], "little")
        w, h = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    elif kind == b"VP8X":  # extended: flags, then 24-bit canvas sizes minus one
        if data[0] & 0x02:
            raise BadPhoto("an animated WebP")
        w = int.from_bytes(data[4:7], "little") + 1
        h = int.from_bytes(data[7:10], "little") + 1
    else:
        raise BadPhoto("not a WebP")
    if not (w and h):
        raise BadPhoto("not a WebP")
    return w, h


def _check(raw):
    """(width, height, a JPEG?) from the header alone, or BadPhoto for a picture with
    more pixels, components or scans than may be decoded safely. Nothing is decoded, so
    this runs before taking a decode slot. A WebP's size is read by webp_size; JPEG, PNG
    and GIF headers by Pillow, whose readers for them are plain Python."""
    if raw[:4] == b"RIFF":
        w, h = webp_size(raw)
        if w * h > MAX_PIXELS["WEBP"]:
            raise BadPhoto("too many pixels")
        return w, h, False
    try:
        with Image.open(io.BytesIO(raw), formats=PARENT_FORMATS) as probe:
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
    refuses or Pillow can't read."""
    w, h, jpeg = _check(raw)
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


def process_here(raw, kind="volunteer"):
    """(full, thumb) JPEGs of an uploaded picture, by what it is for, decoded in this
    process (app/photo_worker.py runs it; everything else calls process):

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


# The decode runs in a child process (app/photo_worker.py), so a picture that slips past
# _check's limits can't take the app down with it: it is killed after WORKER_TIMEOUT
# seconds on every platform, and on Linux (Cloud Run) it also can't map more than
# WORKER_MEMORY or use more than WORKER_CPU seconds. macOS doesn't enforce RLIMIT_AS, so
# development gets the CPU limit and the timeout only. The child limits resources; it
# doesn't drop privilege (same user), so it gets no secrets: WORKER_ENV is all it sees.
WORKER_TIMEOUT = 10  # seconds of wall clock, then the child is killed
WORKER_MEMORY = 512 * 2**20  # bytes of address space: a decode peaks under 200 MB RSS
WORKER_CPU = 8  # seconds
WORKER_OUT = 4 * 2**20  # bytes the child may answer (two JPEGs: about 200 KB)
WORKER_ERR = 64 * 2**10  # bytes of stderr kept for the log
WORKER_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "app" / "photo_worker.py"


def worker_command(kind):
    """The child that decodes one picture: -I (no PYTHON* variables, no user
    site-packages, nothing added to sys.path) and the worker's own path."""
    return [
        sys.executable,
        "-I",
        str(WORKER),
        kind,
        str(WORKER_MEMORY),
        str(WORKER_CPU),
    ]


def _run_worker(kind, raw):
    """(return code, stdout, stderr) of one child fed `raw`. It is killed past
    WORKER_TIMEOUT (raises subprocess.TimeoutExpired once it is reaped) or as soon as it
    writes more than WORKER_OUT to stdout or WORKER_ERR to stderr."""
    child = subprocess.Popen(
        worker_command(kind),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
        env=WORKER_ENV,
    )
    out, err = bytearray(), bytearray()

    def feed():
        try:
            child.stdin.write(raw)
            child.stdin.close()
        except OSError:  # the child exited early: its exit code says why
            pass

    def drain(stream, buf, cap):
        while chunk := stream.read1(65536):
            buf += chunk
            if len(buf) > cap:
                child.kill()
                return

    threads = [
        threading.Thread(target=feed, daemon=True),
        threading.Thread(
            target=drain, args=(child.stdout, out, WORKER_OUT), daemon=True
        ),
        threading.Thread(
            target=drain, args=(child.stderr, err, WORKER_ERR), daemon=True
        ),
    ]
    timed_out = threading.Event()

    def time_out():
        timed_out.set()
        child.kill()

    # a timer, not wait(timeout=): Popen.wait polls with sleeps when given a timeout
    timer = threading.Timer(WORKER_TIMEOUT, time_out)
    timer.daemon = True
    for t in (*threads, timer):
        t.start()
    try:
        child.wait()
    finally:
        timer.cancel()
        for t in threads:
            t.join(timeout=5)
        for stream in (child.stdout, child.stderr):
            stream.close()
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(child.args, WORKER_TIMEOUT)
    return child.returncode, bytes(out), bytes(err)


def _log_failure(kind, why, code=None, err=b""):
    """One warning line: never the picture, only the exit and stderr's last line."""
    signal_name = ""
    if code is not None and code < 0:
        try:
            signal_name = signal.Signals(-code).name
        except ValueError:
            signal_name = f"signal {-code}"
    tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
    log.warning(
        "photo worker (%s) %s: rc=%s %s %s",
        kind,
        why,
        code,
        signal_name,
        ascii(tail[0][-300:]),
    )


def _is_jpeg(b):
    return b[:2] == b"\xff\xd8" and b[-2:] == b"\xff\xd9"


def process(raw, kind="volunteer"):
    """process_here's (full, thumb), decoded in a child process, at most two at a time.

    The header checks (_check) run here first, so most refusals cost no child and no
    slot. BadPhoto for a refused picture and for a child that failed, ran out of time or
    memory, or answered something other than two JPEGs; Busy when no slot frees up
    within WAIT seconds."""
    if len(raw) > MAX_UPLOAD:
        raise BadPhoto("too large")
    _check(raw)
    if not _decoding.acquire(timeout=WAIT):
        raise Busy("both decode slots are taken")
    try:
        code, out, err = _run_worker(kind, raw)
    except subprocess.TimeoutExpired as e:  # killed and reaped
        _log_failure(kind, "timed out")
        raise BadPhoto("the picture took too long to decode") from e
    except OSError as e:  # couldn't start a child (no memory, too many processes)
        log.warning("photo worker (%s) didn't start: %s", kind, type(e).__name__)
        raise Busy("no decoder could start") from e
    finally:
        _decoding.release()
    if code == 2:
        raise BadPhoto("the picture couldn't be decoded")  # refused by its checks
    if code != 0 or not 8 <= len(out) <= WORKER_OUT:
        _log_failure(kind, "failed", code, err)
        raise BadPhoto("the picture couldn't be decoded")
    size = int.from_bytes(out[:8], "big")
    if 8 + size > len(out):
        _log_failure(kind, "answered a short file", code, err)
        raise BadPhoto("the decoder answered something else")
    full, thumb = out[8 : 8 + size], out[8 + size :]
    if not (_is_jpeg(full) and _is_jpeg(thumb)):
        raise BadPhoto("the decoder answered something else")
    return Processed(full, thumb)
