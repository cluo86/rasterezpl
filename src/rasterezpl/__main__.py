"""``python -m rasterezpl`` — the command line (also the entry point of the packaged executable)."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
