"""Run from a checkout without installing: `python cli.py <repo> <command> ...`."""
import os
import sys

try:
    import terra_pilot  # noqa: F401  (installed, e.g. `pip install -e .`)
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from terra_pilot.cli.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
