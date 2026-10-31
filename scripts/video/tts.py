"""Voice each beat's narration line with Kokoro; write OUT_DIR/<id>.wav and durations.json.

python tts.py BEATS.json OUT_DIR CACHE_DIR [VOICE]

Run with the TTS virtualenv's python (make_demo_video.sh sets it up). A line whose text and
voice are unchanged since the last run keeps its .wav, so editing one line voices one line.
CACHE_DIR holds the espeak-ng data copy (espeak-ng reads only a short data path, so the
copy lives outside the virtualenv's long path).
"""

import json
import shutil
import sys
from pathlib import Path

beats = json.loads(Path(sys.argv[1]).read_text())
out, cache = Path(sys.argv[2]), Path(sys.argv[3])
voice = sys.argv[4] if len(sys.argv) > 4 else "af_heart"
out.mkdir(parents=True, exist_ok=True)

import espeakng_loader
import numpy as np
import soundfile as sf
from phonemizer.backend.espeak.wrapper import EspeakWrapper

data_copy = cache / "espeak-ng-data"
if not data_copy.is_dir():
    shutil.copytree(espeakng_loader.get_data_path(), data_copy)

import misaki.espeak  # noqa: F401  sets its own (too long) data path on import
from kokoro import KPipeline

EspeakWrapper.set_library(espeakng_loader.get_library_path())
EspeakWrapper.set_data_path(str(data_copy))

pipe = None
durations = {}
for b in beats:
    wav, stamp = out / f"{b['id']}.wav", out / f"{b['id']}.txt"
    key = f"{voice}\n{b['say']}"
    if not (wav.is_file() and stamp.is_file() and stamp.read_text() == key):
        pipe = pipe or KPipeline(lang_code="a")
        chunks = [a for _, _, a in pipe(b["say"], voice=voice, speed=1.0)]
        sf.write(wav, np.concatenate([np.asarray(c) for c in chunks]), 24000)
        stamp.write_text(key)
        print("voiced", b["id"], flush=True)
    info = sf.info(wav)
    durations[b["id"]] = round(info.frames / info.samplerate, 2)
(out / "durations.json").write_text(json.dumps(durations, indent=1))
print(json.dumps(durations))
