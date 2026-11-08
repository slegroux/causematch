"""The demo app for the video: invented demo data on a new throwaway database, with the
fake SMS and the fake emailer (the sign-in links are read from its log), but with the
stand-in teams' automatic "yes" to a like turned off: Jordan answers Riley's like himself, on camera.

python scripts/video/serve.py PORT DB

The seed holds the one nonprofit that Jordan claims on camera (demo_seed.CLAIM_ORG:
invented, unverified, with an EIN the form accepts); its pending claim is dropped, so the
admin's list is empty until Jordan asks. Then it serves until killed. Local only, like
scripts/demo.py.
"""

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
from app.db import claim_requests

claim_requests.delete_where("status = 'pending'")
print("ready", flush=True)
demo.serve(port)
