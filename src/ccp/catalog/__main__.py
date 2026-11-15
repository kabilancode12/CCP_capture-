"""Allow ``python -m ccp.catalog`` as an alternative to the ``ccp-catalog`` command."""

import sys

from ccp.catalog.cli import main

if __name__ == "__main__":
    sys.exit(main())
