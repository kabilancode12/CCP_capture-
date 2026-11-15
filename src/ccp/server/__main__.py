"""Allow ``python -m ccp.server`` as an alternative to the ``ccp-server`` command."""

import sys

from ccp.server.main import main

if __name__ == "__main__":
    sys.exit(main())
