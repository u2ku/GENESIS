"""The suite's own floor: a test run never opens a browser on the desk.

The wizard's last act is to open the interface, and that is right for a
person. A test run is not a person, and neither is an agent's shell. The
guard for that is `GENESIS_NO_BROWSER`, and this file sets it for pytest
itself — which loads `conftest.py` for every run in the repository,
including a run of a single file. `tests/hermetic.py` sets it too, but
only when some test imports it, so a suite run on its own would not have
it.

Every subprocess a test starts inherits the environment, so this holds
for the real CLI the tests drive, not only for the code under test.
"""
from __future__ import annotations

import os

os.environ.setdefault("GENESIS_NO_BROWSER", "1")
