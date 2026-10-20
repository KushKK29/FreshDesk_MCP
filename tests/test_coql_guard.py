"""Wires coql_guard.py's own _demo() self-check into pytest, same pattern
as test_soql_guard.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import coql_guard  # noqa: E402


def test_coql_guard_demo():
    coql_guard._demo()


if __name__ == "__main__":
    test_coql_guard_demo()
    print("all checks passed")
