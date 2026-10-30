"""Offline startup check for the frozen macOS app, without WeChat or permissions.

This checks native imports and a real QtWebEngine renderer. It does not check
screen capture permission, Accessibility permission, WeChat recognition, or the
application's behavior on a different macOS version than the build machine.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import platform
import sys


def run(output: Path) -> int:
    result = {
        "ok": False,
        "os": platform.mac_ver()[0],
        "arch": platform.machine(),
        "qt_version": None,
        "capture_backend": None,
        "webengine": False,
        "frozen": bool(getattr(sys, "frozen", False)),
        "scope": "offline startup; no WeChat, screen capture, or permission requests",
    }
    try:
        if sys.platform != "darwin":
            raise RuntimeError("Native startup check requires macOS")
        for name in ("AppKit", "Foundation", "Quartz", "Vision", "ApplicationServices", "objc"):
            importlib.import_module(name)
        quartz = sys.modules["Quartz"]
        major = int(result["os"].split(".", 1)[0])
        if major < 12:
            raise RuntimeError("This app requires macOS 12 or later")
        if major >= 14:
            try:
                sck = importlib.import_module("ScreenCaptureKit")
            except ImportError:
                sck = None
            if sck is not None and hasattr(sck, "SCScreenshotManager"):
                result["capture_backend"] = "ScreenCaptureKit"
        if result["capture_backend"] is None:
            if not callable(getattr(quartz, "CGWindowListCreateImage", None)):
                raise RuntimeError("CoreGraphics window screenshot API is missing")
            result["capture_backend"] = "CGWindowListCreateImage"

        from PySide6.QtCore import QTimer, QUrl, qVersion
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication
        from shiboken6 import delete

        result["qt_version"] = qVersion()
        # Ignore unrelated launch arguments and use an off-the-record profile.
        # No user's normal browser cache, cookies, or app config is opened.
        app = QApplication(["ZhongBenAssistant-smoke-test"])
        app.setQuitOnLastWindowClosed(False)
        view = QWebEngineView()
        view.resize(320, 240)
        profile = QWebEngineProfile(app)
        page = QWebEnginePage(profile, view)
        view.setPage(page)
        timer = QTimer()
        timer.setSingleShot(True)
        complete = False

        def finish(error=None):
            nonlocal complete
            if complete:
                return
            complete = True
            timer.stop()
            result["ok"] = error is None
            if error:
                result["error"] = str(error)
            app.quit()

        def checked(value):
            if value == "zhongben-offline-renderer-ok":
                result["webengine"] = True
                finish()
            else:
                finish("QtWebEngine rendered unexpected local page content")

        def loaded(ok):
            if complete:
                return
            if not ok:
                finish("QtWebEngine could not load the local test page")
                return
            page.runJavaScript("document.getElementById('probe').textContent", checked)

        timer.timeout.connect(lambda: finish("QtWebEngine startup timed out after 20 seconds"))
        page.loadFinished.connect(loaded)
        page.renderProcessTerminated.connect(
            lambda status, code: finish("QtWebEngine renderer terminated: %s/%s" % (status, code)))
        timer.start(20_000)
        page.setHtml("<!doctype html><meta charset='utf-8'><p id='probe'>"
                     "zhongben-offline-renderer-ok</p>", QUrl("about:blank"))
        app.exec()
        if not complete:
            finish("Qt application exited before the renderer check finished")
        # Ensure the page is destroyed before its off-the-record profile.
        delete(page)
        delete(view)
        delete(profile)
    except Exception as exc:
        result["ok"] = False
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print("Cannot write startup check result: %s" % exc, file=sys.stderr)
        return 2
    return 0 if result["ok"] else 1
