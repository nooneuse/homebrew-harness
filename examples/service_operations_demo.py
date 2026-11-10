"""Offline P1-P4 service scenario; real Git/Run/backup operations, synthetic model."""

import argparse
import json
from pathlib import Path

from delivery_demo import run_demo


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(
        json.dumps(run_demo(parser.parse_args().output, operations_demo=True), indent=2)
    )
