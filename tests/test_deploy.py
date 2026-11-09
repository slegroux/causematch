"""entrypoint.sh, the image's start: plain uvicorn, or uvicorn under Litestream when
LITESTREAM_BUCKET is set. Run for real with fake litestream and uvicorn on PATH that log
their arguments, one per line, and the LITESTREAM_DB they were given."""

import json
import os
import shlex
import subprocess

from conftest import ROOT

FAKE = """#!/bin/sh
{ echo "== $(basename "$0") LITESTREAM_DB=${LITESTREAM_DB:-}"; printf '%s\\n' "$@"; } >> "$LOG"
"""


def start(tmp_path, **env):
    """Run entrypoint.sh; (returncode, stderr, [[program, arg, ...], ...] as called)."""
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.log"
    bin_dir.mkdir(exist_ok=True)
    for name in ("litestream", "uvicorn"):
        (bin_dir / name).write_text(FAKE)
        (bin_dir / name).chmod(0o755)
    log.write_text("")
    base = {"PATH": f"{bin_dir}:/usr/bin:/bin", "LOG": str(log), "PORT": "8080"}
    r = subprocess.run(
        ["sh", str(ROOT / "entrypoint.sh")],
        check=False,
        env=base | {"CAUSEMATCH_DB": str(tmp_path / "causematch.db")} | env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = [c.strip().split("\n") for c in log.read_text().split("== ") if c.strip()]
    return r.returncode, r.stderr, calls


def procfile_uvicorn():
    """The Procfile's command, as entrypoint.sh must run it: no uv run, PORT=8080."""
    line = (ROOT / "Procfile").read_text().strip().removeprefix("web: uv run ")
    return shlex.split(line.replace("${PORT:-8080}", "8080"))


def test_without_a_bucket_it_runs_the_procfiles_uvicorn_and_refuses_cloud_run_production(
    tmp_path,
):
    rc, _, calls = start(
        tmp_path, CAUSEMATCH_ENV="preview", K_SERVICE="causematch-preview"
    )
    assert rc == 0
    assert calls == [["uvicorn LITESTREAM_DB=", *procfile_uvicorn()[1:]]]

    # production on Cloud Run would keep its only database in the container
    for env in ({"CAUSEMATCH_ENV": "production"}, {}):
        rc, err, calls = start(tmp_path, K_SERVICE="causematch", **env)
        assert rc == 1 and "LITESTREAM_BUCKET" in err and calls == []


def test_with_a_bucket_it_restores_a_missing_database_then_replicates_under_uvicorn(
    tmp_path,
):
    db, config = str(tmp_path / "causematch.db"), str(ROOT / "litestream.yml")
    env = {"CAUSEMATCH_ENV": "production", "K_SERVICE": "causematch"}
    rc, _, calls = start(tmp_path, LITESTREAM_BUCKET="b", **env)
    assert rc == 0
    replicate = [
        f"litestream LITESTREAM_DB={db}",
        "replicate",
        "-config",
        config,
        "-exec",
        " ".join(procfile_uvicorn()),
    ]
    assert calls == [
        [f"litestream LITESTREAM_DB={db}", "restore", "-config", config]
        + ["-if-replica-exists", db],
        replicate,
    ]

    # the file is there already (a restart within the instance): no restore over it
    open(db, "w").close()
    rc, _, calls = start(tmp_path, LITESTREAM_BUCKET="b", **env)
    assert rc == 0 and calls == [replicate]
    assert os.path.getsize(db) == 0


def test_the_images_start_is_entrypoint_sh():
    """The Dockerfile's last CMD is what Cloud Run runs: entrypoint.sh, not uvicorn alone."""
    cmds = [
        line
        for line in (ROOT / "Dockerfile").read_text().splitlines()
        if line.startswith("CMD ")
    ]
    assert json.loads(cmds[-1].removeprefix("CMD ")) == ["/app/entrypoint.sh"]
