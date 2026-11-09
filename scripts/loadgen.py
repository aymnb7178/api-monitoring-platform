"""Thin wrapper: python scripts/loadgen.py --project NAME [--requests N] (see `python -m apimon loadgen -h`)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from apimon.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["loadgen", *sys.argv[1:]]))
