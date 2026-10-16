"""python -I app/photo_worker.py KIND MEMORY_BYTES CPU_SECONDS: decode one picture alone.

app/photos.py's process runs this for every upload, with an environment of PATH and LANG
only: the picture comes in on stdin, and out on stdout go the two JPEGs of
photos.process_here (an 8-byte big-endian length of the full one, the full one, then the
thumb). A refused picture exits 2; anything else that goes wrong exits non-zero too, and
the parent gives the uploader BadPhoto.

On stderr, for the parent's log and for tests/test_photos.py's check of the limits:
"ready vmsize_kb=N as_limit=N" once the limits are set and the imports done, just before
the picture is read (the address space in use then, and RLIMIT_AS read back: on Linux
both are real, elsewhere N is "-"); "done vmpeak_kb=N" after a picture is processed (the
largest address space the process ever had); "refused: why (cause)" for a refused one.

The limits are set before anything is imported or read: no core dumps, RLIMIT_CPU and,
on Linux, RLIMIT_AS (address space). macOS doesn't enforce RLIMIT_AS, so there only
RLIMIT_CPU and the parent's wall-clock timeout apply. The child runs as the same user as
the app: it limits resources, it doesn't drop privilege.
"""

import resource
import sys
from pathlib import Path


def _status_kb(field):
    """A /proc/self/status figure in kB (VmSize, VmPeak), or "-" off Linux."""
    try:
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith(field + ":"):
                    return line.split()[1]
    except OSError:
        pass
    return "-"


def main(argv):
    kind, memory, cpu = argv[0], int(argv[1]), int(argv[2])
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))  # SIGXCPU, then SIGKILL
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    # -I adds nothing to sys.path: the repo root, explicitly
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import photos

    limit = resource.getrlimit(resource.RLIMIT_AS)[0]
    as_limit = limit if sys.platform.startswith("linux") else "-"
    sys.stderr.write(f"ready vmsize_kb={_status_kb('VmSize')} as_limit={as_limit}\n")
    sys.stderr.flush()
    raw = sys.stdin.buffer.read(photos.MAX_UPLOAD + 1)
    try:
        out = photos.process_here(raw, kind)
    except photos.BadPhoto as e:
        sys.stderr.write(f"refused: {e} ({e.__cause__!r})\n")
        return 2
    sys.stdout.buffer.write(len(out.full).to_bytes(8, "big") + out.full + out.thumb)
    sys.stderr.write(f"done vmpeak_kb={_status_kb('VmPeak')}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
