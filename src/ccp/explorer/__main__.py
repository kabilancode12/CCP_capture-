"""Allow ``python -m ccp.explorer`` to run the command line."""

import sys

from ccp.explorer.cli import main

if __name__ == "__main__":
    sys.exit(main())
