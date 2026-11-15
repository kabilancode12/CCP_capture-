"""Allow ``python -m ccp.decisions`` (there is no ``ccp-decisions`` launcher yet)."""

import sys

from ccp.decisions.cli import main

if __name__ == "__main__":
    sys.exit(main())
