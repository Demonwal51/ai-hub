"""List the mutations of tools/mutations.py whose fragment is not found exactly once.

Run after a refactoring, before the bench: a fragment that moved makes its
mutation "not applied", which protects nothing. Costs no test run.

    python -B tools/mutations_fragments.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mutations import MUTATIONS, SRC  # noqa: E402


def main():
    wrong = 0
    for name, rel, old, _new in MUTATIONS:
        count = (SRC / rel).read_text(encoding="utf-8").count(old)
        if count != 1:
            wrong += 1
            print(f"[{name}] {rel} : fragment trouvé {count} fois")
    print(f"{len(MUTATIONS)} mutations, {wrong} à revoir")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
