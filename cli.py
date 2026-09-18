import os
import sys
sys.path.insert(0, os.path.abspath("src"))
from terra_pilot.cli.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
