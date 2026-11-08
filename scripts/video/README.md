# The narrated demo video

`site/assets/demo.mp4` (the full walkthrough on the website, with `demo-poster.jpg` and the
captions `demo.vtt`) and `site/assets/demo-short.mp4` (a short cut for the page's hero, with
`demo-short-poster.jpg` and `demo-short.vtt`) are made from the
running app, on invented demo data, in three steps: Playwright drives the demo app in four
browser sessions (Riley on a phone, Jordan on a laptop, a Causematch admin on a laptop, and a
signed-out visitor on a laptop) and
records each; Kokoro text to speech voices one narration line per beat; ffmpeg composes
the recordings, captions and voice into one 1080p video with the loudness normalised.

One command does everything, from nothing:

```
scripts/video/make_demo_video.sh
```

## Prerequisites

- macOS or Linux with `ffmpeg` and `ffprobe` (`brew install ffmpeg`) and `uv`. The
  captions use the font `/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf` (macOS);
  on Linux, change `FONT` in `compose.py`.
- Network on the first run: the TTS packages (`kokoro==0.9.4`, `soundfile==0.14.0`, about
  1 GB with torch), the spaCy model `en_core_web_sm` (from its GitHub release), Kokoro's
  model (Hugging Face repo `hexgrad/Kokoro-82M`, about 330 MB, kept in
  `~/.cache/huggingface`) and Playwright's Chromium (about 370 MB).
- Disk: about 1 GB for the TTS environment, 370 MB for Chromium, and about 70 MB of
  recordings, voice and segments for one run.
- Nothing else uses port 5190 (override with `CAUSEMATCH_VIDEO_PORT`).
- `DEMO_PUBLIC_URL`, for the `record` step: the public https origin the video shows on the
  certificate page's check link, with no path (for example
  `DEMO_PUBLIC_URL=https://example.org scripts/video/make_demo_video.sh`). Both the script
  and `record.py` refuse to record without it, so a local address cannot get on screen.
  The app keeps its local origin (it refuses posts from any other).

Nothing is installed into the repo's virtualenv except what `uv sync` already installs
(Playwright and Pillow are dev dependencies).

## Steps

`make_demo_video.sh` takes any of these steps, in this order (none: all of them):

| Step | What it does |
| --- | --- |
| `setup` | `uv sync`, `uv run playwright install chromium`, and a Python 3.12 virtualenv for Kokoro (`uv pip install kokoro soundfile`, plus the spaCy model that matches the installed spaCy). Idempotent. |
| `voice` | `tts.py` voices each line of `beats.json` (voice `af_heart`, or `CAUSEMATCH_VIDEO_VOICE`; changing it re-voices every line) into `voice/<id>.wav` and `durations.json`, then the same for the short cut's `beats-short.json` into `voice-short/`. A line whose text is unchanged keeps its `.wav`. |
| `record` | Starts `serve.py` (the demo's invented data, on a new throwaway database, with the fake SMS and the fake emailer, whose log holds the sign-in links) and runs `record.py`, which plays the whole story in the app and holds each beat until its voice line has finished. Fails, after saving the recordings, on any console error, page error, failed request, 5xx answer or server error. |
| `compose` | `compose.py` builds one segment per beat (layout, caption, voice), joins them, normalises the loudness to -16 LUFS (two-pass `loudnorm`), encodes H.264 (crf 23, faststart) and takes the poster and writes `demo.vtt`: one caption cue per sentence of the narration, timed from the beat's start. Fails if a voice line doesn't fit its beat. It runs twice, on the same recording: `beats.json` makes `demo.mp4`, `beats-short.json` makes `demo-short.mp4` (see "The short cut"). |
| `install` | Copies `demo.mp4`, `demo-poster.jpg`, `demo.vtt` and the three `demo-short` files into `site/assets/`. |

Files that matter: `beats.json` (each beat: `id`, `layout` of card, laptop, phone or
both, `src` and `role` for the caption chip, `caption`, and the spoken `say`), `beats-short.json`
(the short cut, below), `serve.py`,
`record.py` (the story, one block per beat, in the order of `beats.json`), `compose.py`
(layouts, colours, title and closing cards) and `tts.py`.

## Where things live

Outside the repo, so nothing here can be committed by accident:

- `CAUSEMATCH_VIDEO_CACHE`, default `~/.cache/causematch`: `tts-venv/` (the TTS
  virtualenv) and `espeak-ng-data/` (espeak-ng reads only a short data path, so the
  data from the `espeakng_loader` package is copied here).
- `CAUSEMATCH_VIDEO_WORK`, default `$CAUSEMATCH_VIDEO_CACHE/video-work` (the script refuses
  a folder inside the repo; `setup` marks its work folder with `.causematch-video-work`, and
  `record` deletes `take/` and `rec.db` only in a folder that has the mark): `voice/`,
  `take/` (the four `.webm` recordings, `timeline.json`, `problems.txt`, the segments),
  `rec.db` (the throwaway database), `server.log`, and the finished `demo.mp4` and
  `demo-poster.jpg` and `demo.vtt`.

The only things written into the repo are `site/assets/demo.mp4`, `demo-poster.jpg`,
`demo.vtt` and the three `demo-short` files, by `install`. Commit those with the change that needed them.

## Change one narration line

Edit its `say` (and `caption`) in `beats.json` (or `beats-short.json`), then:

```
scripts/video/make_demo_video.sh voice record compose install
```

`voice` voices only the changed line. `record` has to run again for a line of `beats.json`,
because every beat is paced by its line's length (about 2 minutes 20 seconds). A line of
`beats-short.json` needs only `voice compose install`: the short cut takes its footage from
the same recording and lasts as long as its lines. Keep the words about hours to "hours a
nonprofit confirms" and the one example the preference page gives, school: the
video says nothing more about why anyone needs them.

To change only a caption, the colours or a card, run `compose install`: the recordings
are reused. To change what happens on screen, edit `record.py` and run
`record compose install`. `compose` reads the take in `$CAUSEMATCH_VIDEO_WORK/take`, so a
good take can be composed again and again.

## Why "after the shift" is staged

A shift can only be marked attended once it has started, and a recording can't wait for
that. The `attend` beat moves the one shift's start two days into the past (9 am Los
Angeles time) directly in the throwaway database (`days_pass()` in `record.py`), and the
narration says "after the shift". The opening is posted for the next Saturday at 9 am (so
Riley's card shows the "You're free Sat mornings" reason), which means the date on the
schedule after the move is earlier than the one on Riley's card: the narration stays
neutral about how many days passed. `days_pass()` refuses to run in production, on a path
inside the repo, or on a database without the demo seed; it opens the file read-write only
(never creates one), changes exactly one row in one transaction (rolled back otherwise)
and closes the connection.

## Riley's last name is added off camera

A certificate carries the volunteer's full name, so Riley needs a last name. At the end of
the `attend` beat (a laptop-only beat), `record.py` opens a second page in the phone's
browser context, saves "Chen" on the profile form there and closes it. The recorded phone
screen is untouched; that page's own recording is never composed.

## Why the takeover is nudged

On Riley's phone the deck asks `/deck/news` every 10 seconds, and a nonprofit's yes brings
the "It's a match!" takeover with no tap. Left alone, the takeover would land anywhere in
those 10 seconds, often well after the narration says it has. So the `accept` beat fires the
page's own tab-visible trigger (a `visibilitychange` event) once, while Jordan types, and
then waits for the takeover (`dialog.cm-moment[open]`). The fetch and the takeover are the
app's own; only their timing is chosen.

## The check link is opened by a fourth browser

The `check` beat shows a certificate's check link signed out, so it opens in its own fresh
browser context (`guest`, a laptop), not in Jordan's or the admin's signed-in ones. That
context exists for the whole recording and sits on a blank page until the beat. The link is
the one Riley's certificate page prints, with its origin swapped on the page, while
recording, for the public one (`DEMO_PUBLIC_URL`, below), so no local address is on screen.
The path is the real one; the swap changes only the text the camera sees, never the app.
The certificate page itself shows the code and the link; the QR code is in the PDF, which
the video names but does not open.

## Invented data only

The repo is public. The video uses the demo seed (`app/demo_seed.py`) and made-up people
(Riley, Jordan, Silver Lake Garden Friends, EIN `99-0000001`). Never record with
`--seed-file` or against any real database, and never show the private preview.

## The short cut

`beats-short.json` makes `demo-short.mp4` (about 1 minute 7 seconds, for the website's hero)
from the same recording as the full video: no second take and no second story in
`record.py`. Each beat has its own, shorter voice line (voiced into `voice-short/`) and lasts
as long as that line plus a tail, and takes its footage from a beat of the recording:

- `from`: the recorded beat the footage comes from (cards have none).
- `mark`, `pre`: start at a named moment `record.py` noted inside the beat (`marks` in
  `timeline.json`: `form`, `deck`, `liked`, `accepted`, `takeover`, `passed`, `cert`, `link`),
  `pre` seconds earlier.
- `speed`: play the footage faster, so a longer action fits a shorter line.
- `tail` (default 1 second), `poster` (a mark: the poster is taken just after it).

`compose.py` stops, naming the beat, when a beat's footage would run past the end of the
recorded beat it comes from: lengthen the line less, raise `speed`, or move `pre`. After a
new `record`, check the short cut's stills: the marks move by a second or so from take to
take, and the narration is lined up with them (the heart is tapped as the line says so).

## Timing

The result of one run (2026-11-07): the full video 2:30 (150.4 s), 1920x1080, 6.6 MB; the
short cut 1:07 (67.6 s), 2.7 MB; both -16.1 LUFS. Recording the story takes about 2 minutes
20 seconds (paced by the voice lines), composing both videos about 40 seconds, and voicing a
changed line or two about 20 seconds. The first volunteer appears at about 0:34, and no
stretch of the full video is silent for more than 3 seconds (the longest gap between caption
cues in `demo.vtt` is 2.6 seconds).
