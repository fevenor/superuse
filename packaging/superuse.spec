# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir spec: Superuse.exe (windowed tray) + SuperuseMCP.exe
# (console, stdio) sharing one dependency tree (docs/development/windows-operations.md §6).
# Build: uv run --locked --extra app --extra packaging pyinstaller
#        packaging/superuse.spec --noconfirm
#
# One Analysis per entry point; each EXE embeds only its own script's TOC,
# and a single COLLECT merges/dedupes binaries and data for both.

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
WEB_DIR = ROOT / "web"

datas = []
if WEB_DIR.is_dir():
    datas.append((str(WEB_DIR), "web"))
for notice in ("LICENSE", "NOTICE.md", "README.md"):
    if (ROOT / notice).is_file():
        datas.append((str(ROOT / notice), "."))
if (ROOT / "third_party" / "CHANGES.md").is_file():
    datas.append((str(ROOT / "third_party" / "CHANGES.md"), "third_party"))
for license_file in (ROOT / "third_party" / "licenses").glob("*.txt"):
    datas.append((str(license_file), "third_party/licenses"))

COMMON = dict(
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=["pystray._win32"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "ruff"],
    noarchive=False,
)

a_tray = Analysis([str(ROOT / "packaging" / "entry_tray.py")], **COMMON)
a_mcp = Analysis([str(ROOT / "packaging" / "entry_mcp.py")], **COMMON)

pyz_tray = PYZ(a_tray.pure)
pyz_mcp = PYZ(a_mcp.pure)

tray_exe = EXE(
    pyz_tray,
    a_tray.scripts,
    [],
    exclude_binaries=True,
    name="Superuse",
    debug=False,
    strip=False,
    upx=False,
    console=False,  # notification-area app: no console window (W01)
    icon=None,
)

mcp_exe = EXE(
    pyz_mcp,
    a_mcp.scripts,
    [],
    exclude_binaries=True,
    name="SuperuseMCP",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # stdio bridge: keep stdin/stdout usable (W03)
    icon=None,
)

coll = COLLECT(
    tray_exe,
    mcp_exe,
    a_tray.binaries,
    a_tray.datas,
    a_mcp.binaries,
    a_mcp.datas,
    strip=False,
    upx=False,
    name="Superuse",
)
