#!/usr/bin/env bash
#
# Build the desktop backend into a self-contained onedir bundle via PyInstaller.
# Run from anywhere; paths are resolved relative to the repo root.
#
# Two distinct modes - do not mix their semantics:
#
#   RELEASE (canonical, fail-closed):
#     bash desktop/build/build-backend.sh --release
#     - requires EXACTLY the canonical release Python (toolchain.lock.json:
#       3.11.9); refuses to run on anything else
#     - installs from the hash-locked
#       desktop/build/requirements-desktop-py311.lock.txt
#     - PyInstaller comes from the lock (6.22.3); no unpinned installs
#     - runs pip check and refuses to continue on any mismatch
#
#   DEVELOPMENT (default, flexible):
#     bash desktop/build/build-backend.sh
#     - historical behavior: prefers 3.11, falls back to python3.12/3.10/python3
#     - installs from desktop/build/requirements-desktop.txt + unpinned
#       pyinstaller (convenience only - NEVER used for release artifacts)
#
# Output: desktop/build/dist/smart-assistant-backend/  (folder with the executable)
set -euo pipefail

RELEASE_MODE=0
if [ "${1:-}" = "--release" ]; then
  RELEASE_MODE=1
fi

# --- resolve paths --------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="$SCRIPT_DIR"

if [ "$RELEASE_MODE" -eq 1 ]; then
  # ---------------------------------------------------------------------
  # Canonical RELEASE path: exact toolchain, hash-locked deps, fail closed.
  # ---------------------------------------------------------------------
  LOCK_PYTHON_FILE="$ROOT/.python-version"
  LOCK_FILE="$ROOT/desktop/build/requirements-desktop-py311.lock.txt"
  TOOLCHAIN_LOCK="$ROOT/toolchain.lock.json"
  VENV_DIR="$BUILD_DIR/.venv-release"

  [ -f "$LOCK_PYTHON_FILE" ] || { echo "!! missing $LOCK_PYTHON_FILE" >&2; exit 1; }
  [ -f "$LOCK_FILE" ] || { echo "!! missing $LOCK_FILE" >&2; exit 1; }
  [ -f "$TOOLCHAIN_LOCK" ] || { echo "!! missing $TOOLCHAIN_LOCK" >&2; exit 1; }
  EXPECTED_PYTHON="$(tr -d '[:space:]' < "$LOCK_PYTHON_FILE")"

  if [ -z "${PYTHON:-}" ]; then
    # Locate a python3.11 without falling back to other minor versions.
    for cand in "python3.11" "python3"; do
      if command -v "$cand" >/dev/null 2>&1; then PYTHON="$cand"; break; fi
    done
  fi
  [ -n "${PYTHON:-}" ] || { echo "!! no python3.11 found on PATH; release build requires Python $EXPECTED_PYTHON" >&2; exit 1; }

  GOT_VERSION="$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
  if [ "$GOT_VERSION" != "$EXPECTED_PYTHON" ]; then
    echo "!! release toolchain mismatch: expected Python $EXPECTED_PYTHON (toolchain.lock.json / .python-version), got $GOT_VERSION ($PYTHON)" >&2
    exit 1
  fi

  if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON" -m venv "$VENV_DIR"
  fi
  # shellcheck disable=SC1091
  source "$VENV_DIR/Scripts/activate" 2>/dev/null || source "$VENV_DIR/bin/activate"

  # Use `python -m pip` for the self-upgrade: on Windows the pip.exe shim
  # cannot replace itself while running.
  python -m pip install -q --upgrade pip
  echo "==> Installing hash-locked release dependencies"
  pip install --require-hashes --only-binary=:all: -r "$LOCK_FILE"
  pip check
  GOT_PYI="$(python -c 'import importlib.metadata as im; print(im.version("pyinstaller"))')"
  EXPECTED_PYI="$(python -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["pyinstaller"])' "$TOOLCHAIN_LOCK")"
  if [ "$GOT_PYI" != "$EXPECTED_PYI" ]; then
    echo "!! release toolchain mismatch: expected PyInstaller $EXPECTED_PYI, got $GOT_PYI" >&2
    exit 1
  fi
else
  # ---------------------------------------------------------------------
  # DEVELOPMENT path: historical flexible behavior.
  # ---------------------------------------------------------------------
  VENV_DIR="$BUILD_DIR/.venv-build"

  # Prefer Python 3.11 when available: on 3.13+ web.py must be installed from a
  # GitHub git source (the PyPI build fails), which is flaky on some networks.
  # 3.11 installs web.py straight from PyPI and has the best PyInstaller support.
  if [ -z "${PYTHON:-}" ]; then
    for cand in \
      "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11" \
      "python3.11" \
      "python3.12" \
      "python3"; do
      if command -v "$cand" >/dev/null 2>&1; then
        PYTHON="$cand"
        break
      fi
    done
  fi
  # Prefer Python 3.11: it installs web.py from PyPI (no GitHub clone) and avoids
  # 3.13's removed-cgi compatibility shims. Override with PYTHON=... if needed.
  pick_python() {
    if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
    for c in python3.11 python3.12 python3.10 python3; do
      if command -v "$c" >/dev/null 2>&1; then echo "$c"; return; fi
    done
    echo python3
  }
  PYTHON="$(pick_python)"

  if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating build venv at $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
  fi
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"

  echo "==> Installing build dependencies"
  pip install -q --upgrade pip
  # Don't leave a half-populated venv behind if deps fail (e.g. flaky network):
  # the next run would otherwise reuse a broken venv.
  if ! pip install -q -r "$BUILD_DIR/requirements-desktop.txt"; then
    echo "!! Dependency install failed. Removing the build venv so a retry starts clean." >&2
    deactivate || true
    rm -rf "$VENV_DIR"
    exit 1
  fi
  pip install -q pyinstaller
fi

# --- run pyinstaller from repo root so relative datas resolve -------------
cd "$ROOT"
echo "==> Running PyInstaller (onedir)"
pyinstaller "$BUILD_DIR/smart-assistant-backend.spec" \
  --noconfirm \
  --distpath "$BUILD_DIR/dist" \
  --workpath "$BUILD_DIR/build-work"

echo ""
echo "==> Done. Bundle at: $BUILD_DIR/dist/smart-assistant-backend/"
du -sh "$BUILD_DIR/dist/smart-assistant-backend/" 2>/dev/null || true
echo "==> Smoke test: COW_DESKTOP=1 \"$BUILD_DIR/dist/smart-assistant-backend/smart-assistant-backend\""
