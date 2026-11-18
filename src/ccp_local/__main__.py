"""Run as `python -m ccp_local ...` or `python <path>/ccp_local ...` (what the hooks use)."""

import sys
from pathlib import Path

if not __package__:  # started as a folder: import the package from its parent folder
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

from ccp_local.cli import main  # noqa: E402

sys.exit(main())
