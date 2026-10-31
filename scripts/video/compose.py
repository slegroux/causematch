"""Compose the three recordings and the voice-over into one 1080p video.

python compose.py BEATS.json VOICE_DIR TAKE_DIR OUT.mp4
Each beat is a segment with its own layout (card, laptop, phone, both), caption and voice
line; the segments are joined, then encoded once (H.264, crf 23, faststart) with the audio
normalised to -16 LUFS (two-pass loudnorm). Also writes demo-poster.jpg next to OUT.mp4.
Fails if a voice line doesn't fit inside its beat.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BEATS = json.loads(Path(sys.argv[1]).read_text())
VOICE, TAKE, OUT = Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
ART, SEG = TAKE / "art", TAKE / "seg"
ART.mkdir(exist_ok=True)
SEG.mkdir(exist_ok=True)
W, H = 1920, 1080
CREAM, INK, MUTED, LINE, SAND = "#FFF8F0", "#2D1E17", "#6E5A4E", "#EADCCD", "#F5E9DC"
BRAND, BRAND_SOFT, GOOD, GOOD_SOFT = "#C0452A", "#FBE4DC", "#1F7A4D", "#DDF1E6"
ADMIN, ADMIN_SOFT = "#4B5BA6", "#E3E7F7"
FONT = "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf"
TONES = {
    "Volunteer": (GOOD, GOOD_SOFT),
    "Nonprofit": (BRAND, BRAND_SOFT),
    "Admin": (ADMIN, ADMIN_SOFT),
}

# screen rectangles (x, y, w, h) per layout
BOTH_PHONE, BOTH_LAPTOP = (110, 136, 476, 810), (648, 172, 1216, 760)
BIG_LAPTOP = (256, 92, 1408, 880)
BIG_PHONE = (300, 120, 476, 810)


def font(size):
    return ImageFont.truetype(FONT, size)


def mark(d, x, y, s):
    d.rounded_rectangle((x, y, x + s, y + s), radius=s * 0.26, fill=BRAND)
    r, cy = s * 0.17, y + s * 0.42
    for cx in (x + s * 0.36, x + s * 0.64):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="white")
    d.polygon(
        [
            (x + s * 0.205, cy + r * 0.35),
            (x + s * 0.795, cy + r * 0.35),
            (x + s * 0.5, y + s * 0.8),
        ],
        fill="white",
    )


def wordmark(d, x, y, size):
    mark(d, x, y - size * 0.05, size * 1.15)
    f = font(size)
    tx = x + size * 1.45
    d.text((tx, y), "cause", font=f, fill=INK)
    d.text((tx + d.textlength("cause", font=f), y), "match", font=f, fill=BRAND)


def phone_frame(d, rect):
    x, y, w, h = rect
    d.rounded_rectangle((x - 14, y - 14, x + w + 14, y + h + 14), radius=44, fill=INK)


def laptop_frame(d, rect, url):
    x, y, w, h = rect
    d.rounded_rectangle((x - 1, y - 41, x + w + 1, y + h + 1), radius=14, fill=LINE)
    d.rounded_rectangle((x, y - 40, x + w, y + 8), radius=13, fill=SAND)
    for i in range(3):
        cx = x + 22 + i * 20
        d.ellipse((cx - 6, y - 26, cx + 6, y - 14), fill=LINE)
    d.rounded_rectangle((x + 100, y - 32, x + 560, y - 8), radius=12, fill=CREAM)
    d.text((x + 116, y - 28), url, font=font(15), fill=MUTED)


def chip(d, x, y, role, size=17):
    tone, soft = TONES[role]
    f = font(size)
    label = role.upper()
    w = d.textlength(label, font=f) + 2 * size
    d.rounded_rectangle((x, y, x + w, y + size * 2), radius=size, fill=soft)
    d.text((x + size, y + size * 0.42), label, font=f, fill=tone)
    return w


def wrap(d, text, f, width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if d.textlength(trial, font=f) <= width:
            line = trial
        else:
            lines.append(line)
            line = word
    return lines + [line]


def caption_bar(d, n, role, text):
    tone, _ = TONES[role]
    top = 1000
    d.ellipse((96, top, 148, top + 52), fill=tone)
    f = font(28)
    d.text(
        (122 - d.textlength(str(n), font=f) / 2, top + 10), str(n), font=f, fill="white"
    )
    w = chip(d, 166, top + 9, role)
    d.text((166 + w + 20, top + 10), text, font=font(29), fill=INK)


def frame_art(b, n):
    """The still parts of a beat: background, device frames, caption, highlight."""
    im = Image.new("RGB", (W, H), CREAM)
    d = ImageDraw.Draw(im)
    lay, role = b["layout"], b["role"]
    url = (
        "causematch / admin"
        if b.get("src") == "admin"
        else "causematch / nonprofit dashboard"
    )
    if lay == "laptop":
        laptop_frame(d, BIG_LAPTOP, url)
        caption_bar(d, n, role, b["caption"])
        wordmark(d, 1660, 1012, 26)
    elif lay == "phone":
        wordmark(d, 96, 40, 34)
        phone_frame(d, BIG_PHONE)
        x0 = 900
        d.ellipse((x0, 300, x0 + 64, 364), fill=TONES[role][0])
        f = font(34)
        d.text(
            (x0 + 32 - d.textlength(str(n), font=f) / 2, 313),
            str(n),
            font=f,
            fill="white",
        )
        chip(d, x0 + 84, 310, role, 20)
        big = font(54)
        y = 410
        for line in wrap(d, b["caption"], big, 860):
            d.text((x0, y), line, font=big, fill=INK)
            y += 74
    else:  # both
        wordmark(d, 96, 34, 34)
        d.text((96, 86), "VOLUNTEER: RILEY, ON A PHONE", font=font(17), fill=GOOD)
        d.text(
            (648, 100),
            "NONPROFIT: JORDAN AT SILVER LAKE GARDEN FRIENDS",
            font=font(17),
            fill=BRAND,
        )
        phone_frame(d, BOTH_PHONE)
        laptop_frame(d, BOTH_LAPTOP, url)
        if role == "Volunteer":
            x, y, w, h = BOTH_PHONE
            d.rounded_rectangle(
                (x - 22, y - 22, x + w + 22, y + h + 22),
                radius=52,
                outline=GOOD,
                width=6,
            )
        else:
            x, y, w, h = BOTH_LAPTOP
            d.rounded_rectangle(
                (x - 8, y - 48, x + w + 8, y + h + 8), radius=20, outline=BRAND, width=6
            )
        caption_bar(d, n, role, b["caption"])
    path = ART / f"{b['id']}.png"
    im.save(path)
    return path


def card_art(name, lines):
    im = Image.new("RGB", (W, H), CREAM)
    d = ImageDraw.Draw(im)
    size = 64
    total = size * 1.45 + d.textlength("causematch", font=font(size))
    wordmark(d, (W - total) / 2, 330, size)
    y = 470
    for text, size, color in lines:
        f = font(size)
        d.text(((W - d.textlength(text, font=f)) / 2, y), text, font=f, fill=color)
        y += size * 1.7
    path = ART / f"{name}.png"
    im.save(path)
    return path


tl = json.loads((TAKE / "timeline.json").read_text())
starts = {b["id"]: b["t"] for b in tl["beats"]}
order = [b["id"] for b in tl["beats"] if b["id"] != "end"]
offsets = tl["offsets"]
voice_len = json.loads((VOICE / "durations.json").read_text())
CARDS = {
    "intro": [
        ("From sign-up to first shift", 56, INK),
        (
            "A new nonprofit and a new volunteer find each other on Causematch.",
            28,
            MUTED,
        ),
        ("Recorded live in the app, on invented demo data", 22, MUTED),
    ],
    "outro": [
        ("Swipe to volunteer.", 56, INK),
        (
            "Volunteers swipe, nonprofits say yes, a chat opens, and confirmed hours are kept.",
            28,
            MUTED,
        ),
        ("Open source: github.com/slegroux/causematch", 22, MUTED),
    ],
}

segments = []
n = 0
for i, b in enumerate(BEATS):
    bid, start = b["id"], starts[b["id"]]
    if bid == "outro":
        dur = voice_len[bid] + 2.0
    else:
        dur = starts[order[order.index(bid) + 1]] - start
    if b["layout"] == "card":
        art = card_art(bid, CARDS[bid])
        inputs = ["-loop", "1", "-framerate", "30", "-t", f"{dur:.3f}", "-i", str(art)]
        graph = "[0:v]format=yuv420p[v]"
        voice_idx = 1
    else:
        n += 1
        art = frame_art(b, n)
        inputs = ["-loop", "1", "-framerate", "30", "-t", f"{dur:.3f}", "-i", str(art)]
        layers = []
        if b["layout"] == "laptop":
            layers.append((b["src"], BIG_LAPTOP))
        elif b["layout"] == "phone":
            layers.append(("phone", BIG_PHONE))
        else:
            layers += [("phone", BOTH_PHONE), (b["src"], BOTH_LAPTOP)]
        graph, last = [], "0:v"
        for k, (src, (x, y, w, h)) in enumerate(layers, start=1):
            ss = max(0.0, start - offsets[src])
            inputs += [
                "-ss",
                f"{ss:.3f}",
                "-t",
                f"{dur + 0.5:.3f}",
                "-i",
                str(TAKE / f"{src}.webm"),
            ]
            graph.append(
                f"[{k}:v]fps=30,scale={w}:{h}:flags=lanczos,setsar=1,"
                f"tpad=stop_mode=clone:stop_duration=5[s{k}]"
            )
            graph.append(f"[{last}][s{k}]overlay={x}:{y}:shortest=0[o{k}]")
            last = f"o{k}"
        graph.append(f"[{last}]format=yuv420p[v]")
        graph = ";".join(graph)
        voice_idx = len(layers) + 1
    if voice_len[bid] + 0.25 > dur:  # adelay 250 ms in front of the line
        raise SystemExit(
            f"{bid}: the voice line ({voice_len[bid]}s) doesn't fit its beat ({dur:.1f}s)"
        )
    inputs += ["-i", str(VOICE / f"{bid}.wav")]
    graph += f";[{voice_idx}:a]aresample=48000,adelay=250|250,apad[a]"
    out = SEG / f"{i:02d}-{bid}.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            f"{dur:.3f}",
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "1",
            str(out),
        ],
        check=True,
    )
    segments.append(out)
    print(f"{bid:9s} {dur:5.1f}s")

listing = SEG / "list.txt"
listing.write_text("".join(f"file '{p.name}'\n" for p in segments))
joined = SEG / "joined.mp4"
subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
        "-c",
        "copy",
        str(joined),
    ],
    check=True,
)

# loudness: measure, then apply the measured values (linear), so the result lands on target
TARGET = "I=-16:TP=-1.5:LRA=11"
probe = subprocess.run(
    [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(joined),
        "-af",
        f"loudnorm={TARGET}:print_format=json",
        "-vn",
        "-f",
        "null",
        "-",
    ],
    capture_output=True,
    text=True,
    check=True,
).stderr
m = json.loads(re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", probe, re.DOTALL).group(0))
measured = (
    f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
    f"measured_LRA={m['input_lrm'] if 'input_lrm' in m else m['input_lra']}:"
    f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true"
)
subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(joined),
        "-af",
        f"loudnorm={TARGET}:{measured}",
        "-ar",
        "48000",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(OUT),
    ],
    check=True,
)

# the poster: a still from the middle of the accept beat, both screens on show
poster_at = 0.0
cum = 0.0
for p, b in zip(segments, BEATS, strict=True):
    seg_len = float(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(p),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    if b["id"] == "accept":
        poster_at = cum + 3.0
        break
    cum += seg_len
subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-ss",
        f"{poster_at:.2f}",
        "-i",
        str(OUT),
        "-frames:v",
        "1",
        "-vf",
        "scale=1280:-1",
        "-q:v",
        "5",
        str(OUT.with_name("demo-poster.jpg")),
    ],
    check=True,
)
print(OUT)
