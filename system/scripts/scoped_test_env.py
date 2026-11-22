#!/usr/bin/env python3
"""Keep a test module's env stubs and script imports to that module.

test_dispatch.py and test_owner_tabs.py each stubbed os.environ at import time and
loaded dispatch.py, whose `import owner_tabs` (and other siblings) landed in the
shared sys.modules. Run in one process, the second module's stubs replaced the
first's, and both dispatch copies shared one owner_tabs bound to whichever env was
live at its first import, so the first suite's tests read the other suite's state
dir (CloseoutRecordTests' reaper test failed only alongside test_owner_tabs).

Usage in a test module:

    ENV = {...}
    with scoped_test_env.isolated_load(ENV):
        dispatch = load(...)            # reads ENV; its sibling imports stay private
    setUpModule, tearDownModule = scoped_test_env.module_hooks(ENV)

The module's tests then run with ENV applied, and os.environ and sys.modules are
restored when the module finishes.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent


def _script_modules() -> dict[str, object]:
    """sys.modules entries loaded from this scripts tree (owner_tabs, lib.mux, ...), except test modules."""
    found = {}
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if not file:
            continue
        try:
            Path(file).resolve().relative_to(SCRIPTS)
        except (ValueError, OSError):
            continue
        # Test modules (one may be mid-import right now) and this helper stay put.
        if name not in (__name__, "__main__") and not name.startswith("test_"):
            found[name] = module
    return found


@contextmanager
def isolated_load(env: dict[str, str]) -> Iterator[None]:
    """Apply `env` and give the block fresh, private copies of sibling script modules.

    Modules imported inside the block read `env` at import time and keep their own
    references to the siblings they import. On exit os.environ is restored, the
    block's script modules leave sys.modules, and the entries that were there before
    come back, so no other test module sees them.
    """
    saved = _script_modules()
    for name in saved:
        sys.modules.pop(name, None)
    try:
        with patch.dict(os.environ, env):
            yield
    finally:
        for name in _script_modules():
            sys.modules.pop(name, None)
        sys.modules.update(saved)


def module_hooks(env: dict[str, str]) -> tuple[Callable[[], None], Callable[[], None]]:
    """setUpModule and tearDownModule that apply `env` only while the module's tests run."""
    patcher = patch.dict(os.environ, env)

    def set_up_module() -> None:
        patcher.start()

    def tear_down_module() -> None:
        patcher.stop()

    return set_up_module, tear_down_module
