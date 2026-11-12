"""Allow ``python -m ccp.ingest`` as an alternative to the ``ccp-ingest`` command."""

import sys

from ccp.ingest.server import main

if __name__ == "__main__":
    sys.exit(main())
