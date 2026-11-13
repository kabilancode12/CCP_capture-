"""Allow ``python -m ccp.index`` as an alternative to the ``ccp-index`` command."""

import sys

from ccp.index.cli import main

if __name__ == "__main__":
    sys.exit(main())
