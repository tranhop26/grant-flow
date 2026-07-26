"""In-repo fallback emulator for running the GrantFlow test-suite when the
official `genlayer-test` framework is not installed.

It emulates just enough of the GenVM surface used by the contract:
storage auto-initialization, gl.message context, payable value transfers,
gl.nondet.exec_prompt / gl.nondet.web.get with regex mocks, and
gl.eq_principle running the nondet closure leader-style.

This is NOT a consensus simulator - install `genlayer-test`
(pip install genlayer-test, Python 3.12+) to run the same tests through
the official Direct Mode runtime. The fixtures exposed here mirror the
official ones (direct_vm, direct_deploy, direct_bob) so the identical
test file works in both environments.
"""

import json
import os
import re
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_SENDER = "0x" + "a" * 40
BOB = "0x" + "b" * 40


class UserError(Exception):
    pass


class _Address(str):
    def __new__(cls, value):
        return super().__new__(cls, str(value))

    @property
    def as_hex(self):
        return str(self)


class _GenericAlias:
    """Stands in for TreeMap / DynArray type constructors."""

    def __init__(self, name):
        self.name = name

    def __getitem__(self, item):
        return self


TreeMapAlias = _GenericAlias("TreeMap")
DynArrayAlias = _GenericAlias("DynArray")


class _TreeMap(dict):
    pass  # dict already provides get/items/[] semantics used by the contract


class _State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.sender = DEFAULT_SENDER
        self.value = 0
        self.llm_mocks = []
        self.web_mocks = []
        self.transfers = []
        self.current = None
        self.handles = {}


STATE = _State()


# ----------------------------- gl namespace --------------------------------

def _view(fn):
    return fn


def _write(fn):
    return fn


def _payable(fn):
    fn._payable = True
    return fn


_write.payable = _payable


class _Message:
    @property
    def sender_address(self):
        return _Address(STATE.sender)

    @property
    def value(self):
        return STATE.value


def _exec_prompt(prompt, response_format=None, images=None):
    for pattern, response in STATE.llm_mocks:
        if re.search(pattern, prompt, re.S):
            return response
    raise AssertionError("no LLM mock matched the prompt; register mock_llm()")


class _WebResponse:
    def __init__(self, status, body):
        self.status_code = int(status)
        self.body = body.encode("utf-8") if isinstance(body, str) else body


def _web_get(url):
    for pattern, response in STATE.web_mocks:
        if re.search(pattern, url):
            return _WebResponse(response.get("status", 200), response.get("body", ""))
    raise AssertionError("no web mock matched the URL; register mock_web()")


def _contract_interface(cls):
    class _Iface:
        def __init__(self, addr):
            self._addr = str(addr)

        def emit_transfer(self, value=0):
            current = STATE.current
            if current is not None:
                current._balance = int(getattr(current, "_balance", 0)) - int(value)
            STATE.transfers.append((self._addr, int(value)))

        def emit(self, **kwargs):
            return self

    return _Iface


def _default_for(annotation):
    if annotation is TreeMapAlias:
        return _TreeMap()
    if annotation is DynArrayAlias:
        return []
    if annotation is int:
        return 0
    if annotation is str:
        return ""
    if annotation is bool:
        return False
    return None


class _ContractBase:
    @property
    def balance(self):
        return int(self.__dict__.get("_balance", 0))

    def __getattr__(self, name):
        annotations = {}
        for klass in reversed(type(self).__mro__):
            annotations.update(getattr(klass, "__annotations__", {}))
        if name in annotations:
            value = _default_for(annotations[name])
            object.__setattr__(self, name, value)
            return value
        raise AttributeError(name)


gl = types.SimpleNamespace(
    Contract=_ContractBase,
    public=types.SimpleNamespace(view=_view, write=_write),
    message=_Message(),
    vm=types.SimpleNamespace(UserError=UserError),
    nondet=types.SimpleNamespace(
        exec_prompt=_exec_prompt,
        web=types.SimpleNamespace(get=_web_get),
    ),
    eq_principle=types.SimpleNamespace(
        prompt_comparative=lambda fn, principle=None: fn(),
        prompt_non_comparative=lambda fn, task=None, criteria=None: fn(),
        strict_eq=lambda fn: fn(),
    ),
    evm=types.SimpleNamespace(contract_interface=_contract_interface),
)


def _install_fake_genlayer():
    module = types.ModuleType("genlayer")
    exports = {
        "gl": gl,
        "TreeMap": TreeMapAlias,
        "DynArray": DynArrayAlias,
        "Address": _Address,
        "u256": int,
        "u32": int,
        "i32": int,
        "i64": int,
        "bigint": int,
        "allow_storage": lambda cls: cls,
    }
    for key, value in exports.items():
        setattr(module, key, value)
    module.__all__ = list(exports)
    sys.modules["genlayer"] = module


# ----------------------------- test harness ---------------------------------

class ContractHandle:
    _counter = 0

    def __init__(self, instance):
        ContractHandle._counter += 1
        self._c = instance
        self.address = "0xc0ffee" + str(ContractHandle._counter).zfill(34)
        STATE.handles[self.address] = instance

    def __getattr__(self, name):
        target = getattr(self._c, name)
        if not callable(target):
            return target

        def call(*args, value=0):
            STATE.value = int(value)
            STATE.current = self._c
            if int(value) > 0:
                self._c._balance = int(getattr(self._c, "_balance", 0)) + int(value)
            try:
                return target(*args)
            finally:
                STATE.value = 0
                STATE.current = None

        return call


class _ExpectRevert:
    def __init__(self, fragment):
        self.fragment = fragment

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(
                f"expected UserError containing {self.fragment!r}, nothing raised"
            )
        if not issubclass(exc_type, UserError):
            return False  # unexpected error type: propagate
        if self.fragment not in str(exc):
            raise AssertionError(
                f"expected UserError containing {self.fragment!r}, got {exc}"
            )
        return True  # swallow the expected revert


class _Prank:
    def __init__(self, address):
        self.address = str(address)

    def __enter__(self):
        self.previous = STATE.sender
        STATE.sender = self.address
        return self

    def __exit__(self, exc_type, exc, tb):
        STATE.sender = self.previous
        return False


class DirectVM:
    """Mirror of the genlayer-test direct_vm fixture surface used here."""

    @property
    def sender(self):
        return STATE.sender

    @sender.setter
    def sender(self, address):
        STATE.sender = str(address)

    def prank(self, address):
        return _Prank(address)

    def expect_revert(self, fragment):
        return _ExpectRevert(fragment)

    def mock_llm(self, pattern, response):
        STATE.llm_mocks.append((pattern, response))

    def mock_web(self, pattern, response):
        STATE.web_mocks.append((pattern, response))

    def fund(self, address, amount):
        instance = STATE.handles[address]
        instance._balance = int(getattr(instance, "_balance", 0)) + int(amount)


def make_direct_deploy():
    _install_fake_genlayer()

    def direct_deploy(contract_path, *args):
        path = os.path.join(REPO_ROOT, contract_path)
        source = open(path, encoding="utf-8").read()
        module = types.ModuleType("contract_under_test")
        exec(compile(source, path, "exec"), module.__dict__)
        instance = module.Contract(*args)
        return ContractHandle(instance)

    return direct_deploy


def fresh_env():
    """Reset global state and return (direct_vm, direct_deploy, direct_bob)."""
    STATE.reset()
    return DirectVM(), make_direct_deploy(), BOB
