"""Launch a throwaway, web-only JobHunt for looking at the UI on the Mac.

Two things make this safe to run while the Pi is live:

* Slack and the scheduler are disabled, so this process cannot open a second
  Socket Mode connection or double-fetch (the two-instance mistake, D2).
* `db.DB_PATH` is repointed at a *copy* of the database before anything opens
  it, so clicking around — pipeline transitions, adding sources — leaves the
  real `jobhunt.db` untouched and safe to deploy from.

Not part of the app. Run it from anywhere:

    ./jobhunt/venv/bin/python jobhunt/scripts/preview_local.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

# Running a script file puts *its* directory on sys.path, not the repo root, so
# `import jobhunt` would fail. Add the repo root (two levels up) ourselves.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["JOBHUNT_NO_SLACK"] = "1"
os.environ["JOBHUNT_NO_SCHEDULER"] = "1"

from jobhunt import db  # noqa: E402  (must follow the env vars above)

# JOBHUNT_PREVIEW_DB points this at another database — usually a fresh backup
# pulled off the Pi, so the preview shows the live data rather than whatever
# the Mac last happened to fetch. It is still copied first, never opened.
_real = Path(os.environ.get("JOBHUNT_PREVIEW_DB") or db.DB_PATH)
_scratch = Path(tempfile.mkdtemp(prefix="jobhunt-preview-")) / "jobhunt.db"
shutil.copy(_real, _scratch)
db.DB_PATH = _scratch

from jobhunt.app import create_app  # noqa: E402  (must follow the DB_PATH swap)

if __name__ == "__main__":
    from waitress import serve

    print("preview DB: {0} (copy of {1})".format(_scratch, _real))
    serve(create_app(), host="127.0.0.1", port=5055, threads=8)
