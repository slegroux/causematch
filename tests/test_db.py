import os
import subprocess
import sys
from pathlib import Path

import apsw
import pytest

from app.db import (
    ROOT,
    Org,
    Profile,
    causes,
    ensure_causes,
    now,
    openings,
    orgs,
    profiles,
    seed_demo,
)


def test_ensure_causes_adds_missing_without_overwriting():
    causes.update(slug="homelessness-services", active=True)
    causes.delete("environment-parks")
    ensure_causes()
    assert causes["environment-parks"].label == "Environment & parks"
    assert causes["homelessness-services"].active  # edited row left alone


def test_demo_seed_is_idempotent():
    before = (orgs.count, openings.count)
    seed_demo()
    assert (orgs.count, openings.count) == before


def test_ein_is_unique_but_nullable():
    orgs.insert(Org(name="A", ein="12-3456789", created_at=now()))
    orgs.insert(Org(name="B", created_at=now()))
    orgs.insert(Org(name="C", created_at=now()))
    with pytest.raises(apsw.ConstraintError):
        orgs.insert(Org(name="D", ein="12-3456789", created_at=now()))


def run_db_import(cwd, **env):
    e = {k: v for k, v in os.environ.items() if k != "CAUSEMATCH_DB"} | env
    code = f"import sys; sys.path.insert(0, {str(ROOT)!r}); from app.db import DB_PATH; print(DB_PATH)"
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=cwd,
        env=e,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_default_db_path_is_anchored_to_repo_not_cwd(tmp_path):
    expected = str(ROOT / "data" / "causematch.db")
    assert run_db_import(tmp_path) == expected
    assert (
        run_db_import(tmp_path, CAUSEMATCH_DB="") == expected
    )  # empty, as in .env.example
    assert not Path(tmp_path, "data").exists()


def test_one_live_coordinator_per_email_however_cased():
    from app import data

    org = orgs()[0].id
    data.add_coordinator("pat@example.org", org)
    with pytest.raises(apsw.ConstraintError):
        profiles.insert(
            Profile(role="coordinator", email="PAT@example.org", created_at=now())
        )
    profiles.insert(  # only coordinators are unique by email
        Profile(role="volunteer", email="pat@example.org", created_at=now())
    )
    profiles.update(id=data.coordinator_by_email("pat@example.org"), deleted_at=now())
    data.add_coordinator("pat@example.org", org)  # a deleted one frees the address
