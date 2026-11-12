"""Allow ``python -m ccp.verify`` as an alternative to the ``ccp-verify`` command."""

import sys

from ccp.verify.cli import main

sys.exit(main())
