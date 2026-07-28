"""Put `coordinator/` on `sys.path` for the test run.

Coordinator modules import each other as `app.<module>` (see
`app/main.py`), which works because CI sets
`PYTHONPATH: <workspace>:<workspace>/coordinator`. Without this file a
plain local `pytest` collects the same tests with a different path and
fails on `import app.*`, so local and CI runs would disagree.

Repo root is already on the path via pytest's rootdir insertion, which is
what lets `import protocol` work.
"""

import sys
from pathlib import Path

COORDINATOR = Path(__file__).parent / "coordinator"
if str(COORDINATOR) not in sys.path:
    sys.path.insert(0, str(COORDINATOR))
