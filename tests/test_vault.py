"""Wires vault.py's own _demo() self-check into pytest. Logic (round-trip,
tamper detection, missing-key error) already lives there and was previously
only run by `python src/vault.py` manually — never collected by CI.

_demo() mutates and deletes os.environ["VAULT_MASTER_KEY"] as part of its
own missing-key check, so this restores whatever every other test file's
module-level os.environ.setdefault(...) already put there, to avoid
breaking any test that runs after this one in the same pytest session.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import vault  # noqa: E402


def test_vault_demo():
    saved = os.environ.get("VAULT_MASTER_KEY")
    try:
        vault._demo()
    finally:
        if saved is None:
            os.environ.pop("VAULT_MASTER_KEY", None)
        else:
            os.environ["VAULT_MASTER_KEY"] = saved


if __name__ == "__main__":
    test_vault_demo()
    print("all checks passed")
