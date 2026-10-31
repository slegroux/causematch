# The narrated demo video

`site/assets/demo.mp4` (the video on the website, with `demo-poster.jpg`) is made from the
running app, on invented demo data, in three steps: Playwright drives the demo app in three
browser sessions (Riley on a phone, Jordan on a laptop, a Causematch admin on a laptop) and
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
- Network on the first run: the TTS packages (about 1 GB with torch), the spaCy model
  `en_core_web_sm` (from its GitHub release), Kokoro's model (about 330 MB, Hugging Face,
  kept in `~/.cache/huggingface`) and Playwright's Chromium (about 150 MB).
- Disk: about 1.5 GB for the TTS environment, 150 MB for Chromium, and about 600 MB of
  recordings and segments while it runs.
- Nothing else uses port 5190 (override with `CAUSEMATCH_VIDEO_PORT`).

Nothing is installed into the repo's virtualenv except what `uv sync` already installs
(Playwright and Pillow are dev dependencies).

## Steps

`make_demo_video.sh` takes any of these steps, in this order (none: all of them):

| Step | What it does |
| --- | --- |
| `setup` | `uv sync`, `uv run playwright install chromium`, and a Python 3.12 virtualenv for Kokoro (`uv pip install kokoro soundfile`, plus the spaCy model that matches the installed spaCy). Idempotent. |
| `voice` | `tts.py` voices each line of `beats.json` (voice `af_heart`) into `voice/<id>.wav` and `durations.json`. A line whose text is unchanged keeps its `.wav`. |
| `record` | Starts `serve.py` (the demo's invented data, on a new throwaway database, with the fake SMS, code `000000`, and the fake emailer) and runs `record.py`, which plays the whole story in the app and holds each beat until its voice line has finished. Fails, after saving the recordings, on any console error, page error, failed request, 5xx answer or server error. |
| `compose` | `compose.py` builds one segment per beat (layout, caption, voice), joins them, normalises the loudness to -16 LUFS (two-pass `loudnorm`), encodes H.264 (crf 23, faststart) and takes the poster. Fails if a voice line doesn't fit its beat. |
| `install` | Copies `demo.mp4` and `demo-poster.jpg` into `site/assets/`. |

Files that matter: `beats.json` (each beat: `id`, `layout` of card, laptop, phone or
both, `src` and `role` for the caption chip, `caption`, and the spoken `say`), `serve.py`,
`record.py` (the story, one block per beat, in the order of `beats.json`), `compose.py`
(layouts, colours, title and closing cards) and `tts.py`.

## Where things live

Outside the repo, so nothing here can be committed by accident:

- `CAUSEMATCH_VIDEO_CACHE`, default `~/.cache/causematch`: `tts-venv/` (the TTS
  virtualenv) and `espeak-ng-data/` (espeak-ng reads only a short data path, so the
  data from the `espeakng_loader` package is copied here).
- `CAUSEMATCH_VIDEO_WORK`, default `$CAUSEMATCH_VIDEO_CACHE/video-work`: `voice/`,
  `take/` (the three `.webm` recordings, `timeline.json`, `problems.txt`, the segments),
  `rec.db` (the throwaway database), `server.log`, and the finished `demo.mp4` and
  `demo-poster.jpg`.

The only thing written into the repo is `site/assets/demo.mp4` and `demo-poster.jpg`, by
`install`. Commit those with the change that needed them.

## Change one narration line

Edit its `say` (and `caption`) in `beats.json`, then:

```
scripts/video/make_demo_video.sh voice record compose install
```

`voice` voices only the changed line. `record` has to run again, because every beat is
paced by its line's length (about 3 minutes). Keep the words about hours to "hours a
nonprofit confirms": the video says nothing about why anyone needs them.

To change only a caption, the colours or a card, run `compose install`: the recordings
are reused. To change what happens on screen, edit `record.py` and run
`record compose install`. `compose` reads the take in `$CAUSEMATCH_VIDEO_WORK/take`, so a
good take can be composed again and again.

## Why "a few days later" is staged

A shift can only be marked attended once it has started, and a recording can't wait for
that. The `attend` beat moves the one shift's start two days into the past, directly in the
throwaway database (`days_pass()` in `record.py`), and the narration says "a few days
later". It refuses to touch anything but that one shift.

## Invented data only

The repo is public. The video uses the demo seed (`app/demo_seed.py`) and made-up people
(Riley, Jordan, Silver Lake Garden Friends, EIN `99-0000001`). Never record with
`--seed-file` or against any real database, and never show the private preview.

## Timing

Run end to end from nothing (no TTS environment, no espeak data, no Kokoro voice files,
no repo `.venv`; Hugging Face's model cache and Playwright's Chromium kept): TIMING.
