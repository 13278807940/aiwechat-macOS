# -*- mode: python ; coding: utf-8 -*-
"""Run on macOS only; architecture follows the native Python interpreter."""
from pathlib import Path
import os
import platform
import sys

if sys.platform != "darwin":
    raise RuntimeError("Mac .app must be built on macOS; use the supplied CI workflow from Windows")

ROOT = Path(SPECPATH).parent
APP_NAME = "中犇微信助手"
arch = os.environ.get("MAC_BUILD_ARCH") or platform.machine()
if arch not in ("arm64", "x86_64") or platform.machine() != arch:
    raise RuntimeError("Use Python matching the target architecture; universal2 is not assumed")

datas = [(str(ROOT / "resources"), "resources"),
         (str(ROOT / ".env.example"), ".")]
if (ROOT / ".env").is_file():
    datas.append((str(ROOT / ".env"), "."))

analysis = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[], datas=datas,
    hiddenimports=["AppKit", "Foundation", "Quartz", "Vision", "ApplicationServices",
                   "ScreenCaptureKit", "CoreFoundation", "objc"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2"], noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name="ZhongBenAssistant", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, argv_emulation=False,
    target_arch=arch, codesign_identity=os.environ.get("MAC_CODESIGN_IDENTITY") or None,
    entitlements_file=str(ROOT / "packaging" / "entitlements.plist"),
)
collection = COLLECT(exe, analysis.binaries, analysis.datas,
                     strip=False, upx=False, name="ZhongBenAssistant")
app = BUNDLE(
    collection, name=APP_NAME + ".app", bundle_identifier="net.saizhuge.zhongben.wechatassistant",
    info_plist={
        "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": "2.0.3", "CFBundleVersion": "203",
        "LSMinimumSystemVersion": "12.0", "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "NSScreenCaptureUsageDescription": "仅截取微信窗口，用于读取当前联系人和消息。",
        "NSAccessibilityUsageDescription": "识别微信联系人、输入回复，并在确认后发送。",
    },
)
