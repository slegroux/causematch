"""python -m app.photo_worker KIND MEMORY_BYTES CPU_SECONDS: decode one picture alone.

app/photos.py's process runs this for every upload: the picture comes in on stdin, and
out on stdout go the two JPEGs of photos.process_here (an 8-byte big-endian length of
the full one, the full one, then the thumb). A refused picture exits 2; anything else
that goes wrong exits non-zero too, and the parent gives the uploader BadPhoto.

The limits are set before the upload is even read. On Linux: RLIMIT_AS (address space)
and RLIMIT_CPU. macOS doesn't enforce RLIMIT_AS, so there only RLIMIT_CPU and the
parent's wall-clock timeout apply.
"""

import resource
import sys


def main(argv):
    kind, memory, cpu = argv[0], int(argv[1]), int(argv[2])
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    from app import photos

    raw = sys.stdin.buffer.read(photos.MAX_UPLOAD + 1)
    try:
        out = photos.process_here(raw, kind)
    except photos.BadPhoto:
        return 2
    sys.stdout.buffer.write(len(out.full).to_bytes(8, "big") + out.full + out.thumb)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
