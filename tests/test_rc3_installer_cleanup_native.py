"""Exercise the actual NSIS cleanup macro on owned native Windows fixtures.

NSIS compilation, installer and uninstaller are real. Only application payload
files are synthetic; this does not establish complete SmartAssistant acceptance.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKENSIS = Path(os.environ.get("NSIS_TEST_MAKENSIS", ""))
if not MAKENSIS.is_file():
    MAKENSIS = Path(os.environ.get("LOCALAPPDATA", "")) / "electron-builder/Cache/nsis/nsis-3.0.4.1/makensis.exe"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not MAKENSIS.is_file(),
    reason="Requires native Windows and the real NSIS compiler",
)


@pytest.fixture
def native_uninstaller(tmp_path):
    source_include = Path(os.environ.get("NSIS_TEST_INCLUDE", str(ROOT / "desktop/build/installer.nsh")))
    source_include = source_include.resolve(strict=True)
    source_include.relative_to(ROOT.resolve(strict=True))
    include = (tmp_path / "installer.nsh").resolve()
    include.relative_to(tmp_path.resolve(strict=True))
    shutil.copyfile(source_include, include)
    assert include.read_bytes() == source_include.read_bytes()
    setup = tmp_path / "fixture-setup.exe"
    script = tmp_path / "fixture.nsi"
    target = tmp_path / "installed app 中文"
    script.write_text(
        '\n'.join([
            'Unicode true', 'RequestExecutionLevel user', 'SilentInstall silent',
            'SilentUnInstall silent', f'OutFile "{setup}"', f'InstallDir "{target}"',
            f'!include "{include}"',
            'Section', 'SetOutPath "$INSTDIR"',
            'FileOpen $0 "$INSTDIR\\payload.txt" w',
            'FileWrite $0 "owned fixture"', 'FileClose $0',
            'WriteUninstaller "$INSTDIR\\uninstall.exe"', 'SectionEnd',
            'Section "uninstall"', 'SetOutPath "$INSTDIR"',
            'Delete "$INSTDIR\\payload.txt"', 'Delete "$INSTDIR\\uninstall.exe"',
            '!insertmacro customUnInstall', 'SectionEnd',
        ]), encoding="utf-8-sig",
    )
    compiled = subprocess.run([str(MAKENSIS), "/V2", str(script)], shell=False, capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    installed = subprocess.run([str(setup), "/S"], cwd=tmp_path, shell=False, timeout=30)
    assert installed.returncode == 0
    assert (target / "uninstall.exe").is_file()
    detached = tmp_path / "uninstall-copy.exe"
    shutil.copy2(target / "uninstall.exe", detached)

    def uninstall():
        # NSIS requires its final _?= tail unquoted, exactly as the real
        # electron-builder updater invokes it. Quoting the complete argument
        # lets a self-relocating trampoline return before uninstall finishes.
        command = f'"{detached}" /S _?={target}'
        result = subprocess.run(command, cwd=tmp_path, shell=False, timeout=30)
        assert result.returncode == 0
        assert not (target / "payload.txt").exists()
        assert not (target / "uninstall.exe").exists()

    return target, uninstall


def test_cleanup_preserves_remaining_files(native_uninstaller):
    target, uninstall = native_uninstaller
    marker = target / "preserve-user-added-file.txt"
    marker.write_text("must survive non-recursive cleanup", encoding="utf-8")
    uninstall()
    time.sleep(3)
    assert marker.read_text(encoding="utf-8") == "must survive non-recursive cleanup"


def test_empty_cleanup_is_synchronous_and_cannot_delete_immediate_reinstall(native_uninstaller):
    target, uninstall = native_uninstaller
    uninstall()
    assert not target.exists(), "Owned empty installation root must be removed before uninstall returns"
    target.mkdir()
    marker = target / "new-install.txt"
    marker.write_text("new installation must survive", encoding="utf-8")
    time.sleep(3)
    assert marker.read_text(encoding="utf-8") == "new installation must survive"
