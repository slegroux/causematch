"""The demo app for the video: invented demo data on a new throwaway database, with the
fake SMS (every code is 000000) and the fake emailer, but with the stand-in teams' automatic
"yes" to a like turned off: Jordan answers Riley's like himself, on camera.

python scripts/video/serve.py PORT DB

It also adds the one nonprofit that Jordan claims on camera (invented, unverified, with an
EIN the form accepts), then serves until killed. Local only, like scripts/demo.py.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.chdir(ROOT)
import demo

port, db = int(sys.argv[1]), sys.argv[2]
demo.configure(port, db)  # refuses production and an existing database
os.environ.pop("CAUSEMATCH_DEMO_MATCHES", None)
demo.demo_seed.seed(None)
import main  # noqa: F401  after seeding, so its startup seeds nothing more
from app.db import Org, now, orgs

orgs.insert(
    Org(
        name="Silver Lake Garden Friends",
        ein="990000001",
        causes=json.dumps(["environment-parks"]),
        lat=34.0869,
        lon=-118.2702,
        intro="cold",
        created_at=now(),
        city="Los Angeles",
        zip="90026",
    )
)
print("ready", flush=True)
demo.serve(port)
