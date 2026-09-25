#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic release toolchain verifier for SmartAssistant.

Reads toolchain.lock.json (repo root) and verifies the ACTIVE toolchain:
the Python interpreter running this script, the Node/npm found on PATH, the
Electron and electron-builder packages installed under desktop/node_modules,
and the PyInstaller version visible to the running interpreter.

Machine-readable output (JSON) with one PASS/FAIL per check; exit code is
non-zero on any FAIL. Optional git-clean check is skipped with --skip-git-clean
(pre-commit validation runs on a dirty tree by design).
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_PATH = os.path.join(ROOT, "toolchain.lock.json")


def _read_lock():
    if not os.path.isfile(LOCK_PATH):
        return None, {"TOOLCHAIN_LOCK_PRESENT": "FAIL"}
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            lock = json.load(f)
        if lock.get("schema_version") != 1:
            return lock, {"TOOLCHAIN_LOCK_PRESENT": "FAIL"}
        return lock, {"TOOLCHAIN_LOCK_PRESENT": "PASS"}
    except Exception:
        return None, {"TOOLCHAIN_LOCK_PRESENT": "FAIL"}


def _python_version():
    return ".".join(str(v) for v in sys.version_info[:3])


def _node_version():
    r = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def _npm_version():
    # npm ships as an npm.cmd shim on Windows; spell out literal argv lists so
    # subprocess never needs a shell and nothing is interpolated.
    if os.name == "nt":
        r = subprocess.run(["npm.cmd", "--version"], capture_output=True, text=True)
    else:
        r = subprocess.run(["npm", "--version"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def _pkg_version(pkg_subpath):
    pkg_json = os.path.join(ROOT, "desktop", "node_modules", pkg_subpath, "package.json")
    if not os.path.isfile(pkg_json):
        return None
    try:
        with open(pkg_json, "r", encoding="utf-8") as f:
            return json.load(f).get("version")
    except Exception:
        return None


def _pyinstaller_version():
    try:
        import importlib.metadata as im

        return im.version("pyinstaller")
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-git-clean",
        action="store_true",
        help="skip the git-clean check (pre-commit validation runs on a dirty tree)",
    )
    parser.add_argument(
        "--skip-node",
        action="store_true",
        help="skip Node/npm checks (python-only validation stages)",
    )
    args = parser.parse_args()

    lock, checks = _read_lock()
    result = "PASS" if checks["TOOLCHAIN_LOCK_PRESENT"] == "PASS" else "FAIL"
    if lock is None:
        print(json.dumps({"result": result, "checks": checks}, indent=2))
        sys.exit(1)

    def check(name, actual, expected):
        checks[name] = "PASS" if actual == expected else "FAIL"
        if checks[name] == "FAIL":
            checks[name + "_ACTUAL"] = actual
            checks[name + "_EXPECTED"] = expected
        return checks[name]

    check("TOOLCHAIN_PYTHON_MATCH", _python_version(), lock["python"])

    if args.skip_node:
        checks["TOOLCHAIN_NODE_MATCH"] = "SKIP"
        checks["TOOLCHAIN_NPM_MATCH"] = "SKIP"
    else:
        node_v = _node_version()
        if node_v is None:
            checks["TOOLCHAIN_NODE_MATCH"] = "FAIL"
            checks["TOOLCHAIN_NODE_MATCH_ACTUAL"] = "node not found on PATH"
        else:
            check("TOOLCHAIN_NODE_MATCH", node_v, "v" + lock["node"])
        npm_v = _npm_version()
        if npm_v is None:
            checks["TOOLCHAIN_NPM_MATCH"] = "FAIL"
        else:
            check("TOOLCHAIN_NPM_MATCH", npm_v, lock.get("npm", npm_v))

    electron_v = _pkg_version("electron")
    builder_v = _pkg_version("electron-builder")
    if electron_v is None or builder_v is None:
        # node_modules not installed yet (e.g. CI runs the verifier before npm ci)
        checks["TOOLCHAIN_ELECTRON_MATCH"] = "SKIP"
        checks["TOOLCHAIN_ELECTRON_BUILDER_MATCH"] = "SKIP"
        checks["TOOLCHAIN_SKIP_NOTE"] = "desktop/node_modules not installed"
    else:
        check("TOOLCHAIN_ELECTRON_MATCH", electron_v, lock["electron"])
        check("TOOLCHAIN_ELECTRON_BUILDER_MATCH", builder_v, lock["electron_builder"])

    pyi_v = _pyinstaller_version()
    if pyi_v is None:
        checks["TOOLCHAIN_PYINSTALLER_MATCH"] = "FAIL"
        checks["TOOLCHAIN_PYINSTALLER_MATCH_ACTUAL"] = "pyinstaller not importable"
    else:
        check("TOOLCHAIN_PYINSTALLER_MATCH", pyi_v, lock["pyinstaller"])

    lock_files = {
        "TOOLCHAIN_PYTHON_LOCK_PRESENT": os.path.isfile(
            os.path.join(ROOT, "desktop", "build", "requirements-desktop-py311.lock.txt")
        ),
        "TOOLCHAIN_NODE_LOCK_PRESENT": os.path.isfile(
            os.path.join(ROOT, "desktop", "package-lock.json")
        ),
    }
    checks["TOOLCHAIN_PYTHON_LOCK_PRESENT"] = "PASS" if lock_files["TOOLCHAIN_PYTHON_LOCK_PRESENT"] else "FAIL"
    checks["TOOLCHAIN_NODE_LOCK_PRESENT"] = "PASS" if lock_files["TOOLCHAIN_NODE_LOCK_PRESENT"] else "FAIL"

    if args.skip_git_clean:
        checks["TOOLCHAIN_GIT_CLEAN"] = "SKIP"
    else:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        checks["TOOLCHAIN_GIT_CLEAN"] = "PASS" if (r.returncode == 0 and not r.stdout.strip()) else "FAIL"

    # Aggregate: any FAIL fails the run; SKIP (not-yet-applicable) and
    # informational entries (non-check strings) do not affect the verdict.
    check_values = [v for k, v in checks.items() if k.startswith("TOOLCHAIN_")]
    result = "PASS" if "FAIL" not in check_values and "PASS" in check_values else "FAIL"
    print(json.dumps({"result": result, "checks": checks}, indent=2))
    sys.exit(0 if result == "PASS" else 1)


if __name__ == "__main__":
    main()
