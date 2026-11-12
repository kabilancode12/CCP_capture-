"""Allow ``python -m ccp.db`` as an alternative to the ``ccp-db`` command."""

import sys

from ccp.db.cli import main

if __name__ == "__main__":
    sys.exit(main())
