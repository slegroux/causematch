import os
import tempfile

# Must be set before app.db is imported.
os.environ["CAUSEMATCH_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
