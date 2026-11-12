"""Allow ``python -m ccp.verify`` as an alternative to the ``ccp-verify`` command."""

import sys

from ccp.verify.cli import main

if __name__ == "__main__":
    sys.exit(main())
