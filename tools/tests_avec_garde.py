"""Run the test suite with a watchdog: after N seconds, dump every thread's stack and exit.

A hung test then names itself, with the line it is stuck on, instead of
leaving a silent process behind.

    python -B tools/tests_avec_garde.py [--seconds 120] [-k motif]
"""

import argparse
import faulthandler
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("-k", action="append", default=[], help="ne lancer que les tests correspondants")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT))
    faulthandler.dump_traceback_later(args.seconds, exit=True)
    loader = unittest.TestLoader()
    if args.k:
        loader.testNamePatterns = [f"*{k}*" for k in args.k]
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    faulthandler.cancel_dump_traceback_later()
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
