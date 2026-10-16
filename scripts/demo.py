"""Run Causematch on a throwaway database full of demo data, for showing it to people.

uv run python scripts/demo.py [--port 5077] [--db PATH] [--seed-file PATH]

Local only: it refuses CAUSEMATCH_ENV=production and Cloud Run (K_SERVICE), runs in
development mode with the fake SMS (every code is 000000) and the fake emailer, and uses a
new database in a temporary folder unless --db names a path that doesn't exist yet.

Without --seed-file the orgs are the invented ones in app/db.py. --seed-file loads
nonprofits from a JSON file kept outside this repo (the repo is public: real names never
go in it), in the format app/demo_seed.py describes, with logos-raw/ and photos-raw/ next
to it. Their openings are samples: each is titled "Sample: ...", and every page carries a
banner saying so.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import demo_seed  # imports nothing that reads the environment


def refuse_production(env=os.environ):
    from app.config import is_production  # no side effects at import, unlike app.db

    if is_production(env):
        sys.exit(
            "demo.py is for local demos only: refusing to run with "
            "CAUSEMATCH_ENV=production or on Cloud Run (K_SERVICE)"
        )


def _git(cwd, *args):
    r = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )
    return r.stdout.split() if r.returncode == 0 else []


def inside_this_repo(path):
    """True when path is in this checkout, or in any other git checkout of this repo
    (one sharing its first commit): real org data must not land where `git add` sees it."""
    path = Path(path).resolve()
    if path.is_relative_to(ROOT):
        return True
    here = Path(path)
    while not here.is_dir():  # the nearest folder that exists
        here = here.parent
    roots = set(_git(ROOT, "rev-list", "--max-parents=0", "HEAD"))
    return bool(roots & set(_git(here, "rev-list", "--max-parents=0", "HEAD")))


def configure(port, db=None, seed_file=None):
    """The demo's environment. Runs before anything imports app, whose modules read it."""
    refuse_production()
    if seed_file and inside_this_repo(seed_file):
        sys.exit(
            "--seed-file must live outside the repo: real org data never enters git"
        )
    if db and Path(db).exists():
        sys.exit(f"{db} exists: demo.py only ever fills a new database")
    db = (
        Path(db)
        if db
        else Path(tempfile.mkdtemp(prefix="causematch-demo-")) / "demo.db"
    )
    os.environ.update(
        CAUSEMATCH_ENV="development",
        CAUSEMATCH_DB=str(db),
        CAUSEMATCH_FAKE_SMS="1",
        CAUSEMATCH_FAKE_EMAIL="1",
        PUBLIC_BASE_URL=f"http://127.0.0.1:{port}",
    )
    os.environ.pop("CAUSEMATCH_SEED_DEMO", None)
    if seed_file:
        os.environ["CAUSEMATCH_DEMO_BANNER"] = "1"
    else:
        os.environ.pop("CAUSEMATCH_DEMO_BANNER", None)
    return db


def serve(port):
    import uvicorn

    from main import UVICORN, app

    uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", **UVICORN)
    ).run()


def main(argv=None):
    refuse_production()
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=5077)
    p.add_argument(
        "--db", help="a new database file (default: one in a temporary folder)"
    )
    p.add_argument("--seed-file", help="nonprofits JSON, kept outside the repo")
    a = p.parse_args(argv)
    db = configure(a.port, a.db, a.seed_file)
    info = demo_seed.seed(a.seed_file)
    import main as _app  # noqa: F401  after seeding, so its startup seeds nothing more

    url = os.environ["PUBLIC_BASE_URL"]
    print(f"Causematch demo on {url}  (database {db})")
    print(
        f"  Volunteer: {url}/login  phone {demo_seed.ME_PHONE}, code 000000 "
        "(any number works)"
    )
    for role, (email, _) in (
        ("Coordinator", demo_seed.COORDINATOR),
        ("Admin", demo_seed.ADMIN),
    ):
        print(f"  {role} ({email}): {demo_seed.sign_in_link(email)}")
    print(
        "  Links work once, for 15 minutes; for a new one, sign in at /org/login with"
    )
    print(f"  the address and the link prints below. Coordinator's org: {info['org']}")
    if a.seed_file:
        print("  Banner on: sample openings, not affiliated with these organizations")
    sys.stdout.flush()
    serve(a.port)


if __name__ == "__main__":
    main()
