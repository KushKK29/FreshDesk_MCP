"""Wires soql_guard.py's own _demo() self-check into pytest. Logic (LIMIT
capping, DML keyword rejection, multi-statement rejection) already lives
there and was previously only run by `python src/soql_guard.py` manually —
never collected by CI, despite being the only defense-in-depth layer
against SOQL injection into Salesforce.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import soql_guard  # noqa: E402


def test_soql_guard_demo():
    soql_guard._demo()


if __name__ == "__main__":
    test_soql_guard_demo()
    print("all checks passed")
