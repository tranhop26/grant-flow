"""Pytest wiring for the GrantFlow test-suite.

Preferred path: install the official framework (Python 3.12+):

    pip install -r requirements-dev.txt
    pytest tests/ -v

When `genlayer-test` is installed its plugin provides the direct-mode
fixtures (direct_vm, direct_deploy, direct_bob) and this file stays out
of the way. When it is NOT installed (e.g. sandboxed CI without registry
access), equivalent fixtures backed by tests/_emulator.py are provided so
the identical test file still runs. The emulator mirrors the fixture
surface but is not a consensus simulator - see _emulator.py.
"""

try:
    import gltest  # noqa: F401  (provided by the genlayer-test distribution)
    _HAVE_GENLAYER_TEST = True
except ImportError:
    _HAVE_GENLAYER_TEST = False

if not _HAVE_GENLAYER_TEST:
    import pytest

    import _emulator

    @pytest.fixture
    def direct_vm():
        vm, _, _ = _emulator.fresh_env()
        return vm

    @pytest.fixture
    def direct_deploy(direct_vm):
        return _emulator.make_direct_deploy()

    @pytest.fixture
    def direct_bob():
        return _emulator.BOB
