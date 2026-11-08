"""The executable's entry point (PyInstaller runs this file as __main__, where a package's relative imports would
not resolve). The same command line as ``rasterezpl`` / ``python -m rasterezpl``."""

import sys

from rasterezpl.cli import main

if __name__ == "__main__":
    sys.exit(main())
