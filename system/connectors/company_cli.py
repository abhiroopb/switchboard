#!/usr/bin/env python3
"""Is the `pmos` on PATH the PM OS connector shim, or an existing CLI with the same name?

One rule for every caller (install.py, install.ps1, verify-kit-install.sh,
install-amp-skills.sh, pm-os-env.sh): the first `pmos` on PATH counts as "real"
(another program that already owns the name) only when it exists and
`pmos --version` does not identify as `pmos-shim`. The shim lives in
system/connectors and answers `--version` with `pmos-shim <version>`; it serves
its tools, login, status, doctor and logout only, so verbs that only the other program has fail
through it. One shared rule keeps the installers from disagreeing about which
one is on PATH.

CLI: prints `real <path>`, `shim <path>`, or `absent`, and exits 0 only for real.
`--quiet` prints nothing.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SHIM_MARK = "pmos-shim"


def pmos_kind(timeout: float = 10.0, which=shutil.which) -> tuple[str, str]:
    """("real" | "shim" | "absent", path)."""
    found = which("pmos")
    if not found:
        return "absent", ""
    path = Path(found)
    if path.parent.name == "connectors" and (path.parent / "pmos-shim.py").is_file():
        return "shim", found
    try:
        result = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        # The shim answers at once; only another program can be this slow.
        return "real", found
    except OSError:
        return "absent", found
    if SHIM_MARK in (result.stdout or "") + (result.stderr or ""):
        return "shim", found
    return "real", found


def has_company_cli() -> bool:
    return pmos_kind()[0] == "real"


def main(argv: list[str]) -> int:
    kind, path = pmos_kind()
    if "--quiet" not in argv:
        print(f"{kind} {path}".strip())
    return 0 if kind == "real" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
