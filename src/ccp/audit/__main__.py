"""Allow ``python -m ccp.audit`` as an alternative to the ``ccp-audit`` command."""

import sys

from ccp.audit.cli import main

if __name__ == "__main__":
    sys.exit(main())
