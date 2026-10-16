#!/usr/bin/env python3
"""Run every test file in tests/ and fail loud on the first problem.

Usage: python check.py        (or ./check.py)

Each tests/test_*.py is a standalone script (no pytest) that exits 0 on
success. This just runs them all, in one place, so "did I break anything"
is one command instead of one per file. Tests that need MongoDB print
"SKIPPED" and exit 0 if mongod isn't reachable — this script treats that as
a FAIL, not a pass, because a push where every DB-backed test silently
skipped is exactly the "nothing breaking gets pushed" guarantee failing
silently.
"""
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).parent / "tests"


def main() -> int:
    test_files = sorted(TESTS_DIR.glob("test_*.py"))
    if not test_files:
        print("No test files found in tests/ — nothing to check.")
        return 1

    failures = []
    skipped = []
    for test_file in test_files:
        result = subprocess.run(
            [sys.executable, str(test_file)],
            capture_output=True, text=True, cwd=Path(__file__).parent,
        )
        output = result.stdout + result.stderr
        if "SKIPPED" in output:
            skipped.append(test_file.name)
            print(f"SKIP  {test_file.name}  (no local MongoDB — this test did not actually run)")
        elif result.returncode != 0 or "all checks passed" not in output:
            failures.append(test_file.name)
            print(f"FAIL  {test_file.name}")
            print(output.strip()[-2000:])
        else:
            print(f"PASS  {test_file.name}")

    print()
    print(f"{len(test_files) - len(failures) - len(skipped)}/{len(test_files)} passed, {len(skipped)} skipped, {len(failures)} failed")

    if skipped:
        print("Start mongod and re-run before pushing — skipped tests did not verify anything.")
    if failures:
        print("Failing: " + ", ".join(failures))

    return 1 if (failures or skipped) else 0


if __name__ == "__main__":
    sys.exit(main())
