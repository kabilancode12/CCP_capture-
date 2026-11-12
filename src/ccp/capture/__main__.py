"""Allow ``python -m ccp.capture`` as an alternative to the ``ccp-capture`` command."""

import sys

from ccp.capture.cli import main

if __name__ == "__main__":
    sys.exit(main())
