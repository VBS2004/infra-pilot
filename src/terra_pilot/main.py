"""Console entry point: `terra-pilot <repo> <command> ...` (same as `python cli.py`)."""
import sys

from terra_pilot.cli.cli import main as _cli_main


def main() -> None:
    sys.exit(_cli_main(sys.argv))


if __name__ == "__main__":
    main()
