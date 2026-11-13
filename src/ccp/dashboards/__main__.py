"""Allow ``python -m ccp.dashboards`` as an alternative to the ``ccp-dashboards`` command."""

import sys

from ccp.dashboards.cli import main

if __name__ == "__main__":
    sys.exit(main())
