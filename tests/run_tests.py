"""Zero-dependency test runner for environments without pytest.

Usage:  python3 tests/run_tests.py

Discovers every test_* function in test_grant_flow.py, injects the
emulator-backed fixtures by parameter name, and reports PASS/FAIL.
With pytest + genlayer-test installed, prefer:  pytest tests/ -v
"""

import inspect
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _emulator
import test_grant_flow as suite


def main():
    tests = [
        (name, fn)
        for name, fn in vars(suite).items()
        if name.startswith("test_") and callable(fn)
    ]
    tests.sort(key=lambda item: inspect.getsourcelines(item[1])[1])

    passed, failed = 0, 0
    for name, fn in tests:
        vm, deploy, bob = _emulator.fresh_env()
        fixtures = {
            "direct_vm": vm,
            "direct_deploy": deploy,
            "direct_bob": bob,
        }
        kwargs = {
            param: fixtures[param]
            for param in inspect.signature(fn).parameters
            if param in fixtures
        }
        try:
            fn(**kwargs)
            print(f"PASS  {name}")
            passed += 1
        except Exception:
            print(f"FAIL  {name}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
