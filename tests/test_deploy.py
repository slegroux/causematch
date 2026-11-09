"""entrypoint.sh, the image's start: plain uvicorn, or uvicorn under Litestream when
LITESTREAM_BUCKET is set. Run for real with fake litestream and uvicorn on PATH that log
their arguments, one per line, and the LITESTREAM_DB they were given. And
scripts/deploy_production.sh's refusals, with a fake gcloud: no real one ever runs."""

import json
import os
import shlex
import shutil
import subprocess

import pytest
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
    assert json.loads(cmds[-1].removeprefix("CMD ")) == [
        "/bin/sh",
        "/app/entrypoint.sh",
    ]


# Answers what deploy_production.sh asks, from FAKE_* variables, and logs every call.
GCLOUD = """#!/bin/sh
printf '%s\\n' "$*" >> "$LOG"
case "$*" in
"projects describe"*"value(name)"*) echo Causematch ;;
"projects describe"*"value(projectNumber)"*) echo "$FAKE_PROJECT_NUMBER" ;;
"storage buckets describe"*)
	[ "$FAKE_BUCKET_NUMBER" != missing ] || exit 1
	echo "$FAKE_BUCKET_NUMBER"
	;;
"secrets describe GOOGLE"*) exit 1 ;;
"run services describe"*) echo https://causematch-x.a.run.app ;;
esac
"""


def deploy(tmp_path, **env):
    """Run deploy_production.sh against the fake gcloud; (returncode, stderr, calls)."""
    bin_dir, log = tmp_path / "bin", tmp_path / "gcloud.log"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "gcloud").write_text(GCLOUD)
    (bin_dir / "gcloud").chmod(0o755)
    log.write_text("")
    path = f"{bin_dir}:/usr/bin:/bin"
    assert shutil.which("gcloud", path=path) == str(bin_dir / "gcloud")
    base = {
        "PATH": path,
        "LOG": str(log),
        "CPU_ALWAYS_ALLOCATED": "1",
        "CONTACT_EMAIL": "hello@causematch.test",
        "FAKE_PROJECT_NUMBER": "111",
        "FAKE_BUCKET_NUMBER": "111",
    }
    r = subprocess.run(
        ["bash", str(ROOT / "scripts" / "deploy_production.sh")],
        check=False,
        env=base | env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return r.returncode, r.stderr, log.read_text().splitlines()


def deployed(calls):
    return [c for c in calls if c.startswith("run deploy")]


def test_production_deploys_only_to_a_litestream_bucket_in_its_own_project(tmp_path):
    rc, err, calls = deploy(tmp_path)
    assert rc == 0, err
    bucket = "storage buckets describe gs://causematch-la-litestream --raw"
    assert any(c.startswith(bucket) for c in calls)
    assert len(deployed(calls)) == 1

    # another project's bucket (a squatted name), no bucket, an empty answer, and no
    # project number to compare with
    for env in (
        {"FAKE_BUCKET_NUMBER": "222"},
        {"FAKE_BUCKET_NUMBER": "missing"},
        {"FAKE_BUCKET_NUMBER": ""},
        {"FAKE_PROJECT_NUMBER": "", "FAKE_BUCKET_NUMBER": ""},
    ):
        rc, err, calls = deploy(tmp_path, **env)
        assert rc == 1 and "gs://causematch-la-litestream" in err
        assert deployed(calls) == []
        assert not any(c.startswith("secrets") for c in calls)  # checked before them


@pytest.mark.parametrize(
    "env",
    [
        {"SERVICE": "causematch-preview"},
        {"SERVICE_ACCOUNT": "causematch-preview@causematch-la.iam.gserviceaccount.com"},
    ],
)
def test_production_refuses_the_previews_service_or_account(tmp_path, env):
    rc, err, calls = deploy(tmp_path, **env)
    assert rc == 1 and "names the preview" in err
    assert calls == []
