"""A macOS Qt shell around the hosted page, without changing the hosted UI."""
import html
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
from urllib.parse import urlsplit

from PySide6.QtCore import QFile, QIODevice, QPoint, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from .bridge import Backend
from .config import load_config, resource_dir
from .controller import build_controller


class AssistantPage(QWebEnginePage):
    """Keep the privileged WebChannel on the configured hosted origin only."""
    def __init__(self, profile, allowed_host, parent=None):
        super().__init__(profile, parent)
        self.allowed_host = allowed_host.lower()

    def acceptNavigationRequest(self, url, kind, is_main_frame):
        if not is_main_frame:
            return True
        if url.scheme() == "https" and url.host().lower() == self.allowed_host:
            return True
        if url.toString() == "about:blank":
            return True
        if kind == QWebEnginePage.NavigationType.NavigationTypeLinkClicked and url.scheme() in ("https", "http"):
            QDesktopServices.openUrl(url)
        return False


class AssistantWindow(QMainWindow):
    def __init__(self, config, controller):
        super().__init__()
        self.config, self.controller = config, controller
        self._closing = False
        self._follow_minimized = False
        self._seen_wechat = False
        self._offline = False
        self.setWindowTitle("中犇微信助手 · Mac预览版")
        self.setMinimumSize(config.min_width, 360)
        self.resize(config.width, 820)
        self.view = QWebEngineView(self)
        self.setCentralWidget(self.view)
        self.profile = QWebEngineProfile("ZhongBenAssistant", self)
        self.profile.setPersistentStoragePath(str(config.data_dir / "web-profile"))
        self.profile.setCachePath(str(config.data_dir / "web-cache"))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
        self.page = AssistantPage(self.profile, urlsplit(config.main_url).hostname or "", self.view)
        self.view.setPage(self.page)
        self.backend = Backend(controller, config, self)
        self.channel = QWebChannel(self.page)
        self.channel.registerObject("backend", self.backend)
        self.channel.registerObject("mac_backend", self.backend)
        self.page.setWebChannel(self.channel)
        self.backend.status_updated.connect(self._apply_status)
        self._install_scripts()
        self.view.loadFinished.connect(self._loaded)
        self._menus()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.backend.poll)
        self.timer.start(max(500, int(config.poll_interval * 1000)))
        self.statusBar().showMessage("正在连接微信；默认辅助模式，发送前需要人工确认")
        self.view.load(QUrl(config.main_url))
        QTimer.singleShot(200, self.backend.poll)

    def _install_scripts(self):
        qfile = QFile(":/qtwebchannel/qwebchannel.js")
        if not qfile.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError("Qt WebChannel resource missing")
        channel_js = bytes(qfile.readAll()).decode("utf-8")
        qfile.close()
        resources = resource_dir() / "resources"
        # QWebChannel is installed before page scripts create their WX bridge.
        prelude = "Object.defineProperty(window,'QWebChannel',{value:undefined,writable:true,configurable:true});\n"
        self._inject("mac-webchannel", prelude + channel_js + "\n" + (resources / "bridge.js").read_text(encoding="utf-8"), True)
        self._inject("customer-name-cache", (resources / "customer_profile_cache.js").read_text(encoding="utf-8-sig"), True)
        self._inject("customer-contact-sync", (resources / "contact_sync.js").read_text(encoding="utf-8"), False)

    def _inject(self, name, source, creation):
        script = QWebEngineScript()
        script.setName(name)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation if creation
                                 else QWebEngineScript.InjectionPoint.DocumentReady)
        script.setSourceCode(source)
        self.page.scripts().insert(script)

    def _menus(self):
        menu = self.menuBar().addMenu("微信助手")
        for label, callback in (("重新加载页面", self._reload), ("权限检查", self._permissions),
                                ("打开配置和日志文件夹", self._open_data), ("停止当前任务", self.controller.stop),
                                ("退出", self.close)):
            action = QAction(label, self)
            action.triggered.connect(callback)
            menu.addAction(action)

    def _reload(self):
        self._offline = False
        self.view.load(QUrl(self.config.main_url))

    def _loaded(self, ok):
        if ok or self._closing or self._offline:
            return
        self._offline = True
        source = (resource_dir() / "resources/offline.html").read_text(encoding="utf-8")
        source = "<script>window.__assistantMainUrl=" + json.dumps(self.config.main_url) + ";</script>" + source
        self.view.setHtml(source, QUrl(self.config.main_url))

    def _open_data(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.config.data_dir)))

    def _permissions(self):
        self.backend.call("permission-menu", "permissions", "{}")
        info = QMessageBox(self)
        info.setWindowTitle("微信助手权限")
        info.setText("请在系统设置 → 隐私与安全性中，允许本应用使用“辅助功能”和“屏幕录制”。")
        info.setInformativeText("辅助功能用于读取和操作微信控件，屏幕录制用于只截取微信窗口。修改权限后可能需要重新启动本应用。")
        button = info.addButton("打开系统设置", QMessageBox.ButtonRole.ActionRole)
        info.addButton(QMessageBox.StandardButton.Close)
        info.exec()
        if info.clickedButton() == button:
            QDesktopServices.openUrl(QUrl("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"))

    def _apply_status(self, response):
        if self._closing or not isinstance(response, dict):
            return
        data = response.get("data", response)
        permissions = data.get("permissions") or {}
        if permissions and not (permissions.get("accessibility") and permissions.get("screen_recording")):
            self.statusBar().showMessage("缺少权限：请通过应用菜单打开“权限检查”")
        else:
            self.statusBar().showMessage("辅助模式：生成草稿后人工确认发送" if data.get("mode", "assist") == "assist" else "自动模式运行中")
        # The controller supplies geometry from its background read. Never call
        # AX, OCR or screenshot APIs from this Qt slot.
        geometry = data.get("window") or data.get("window_geometry")
        if geometry is None:
            if self._seen_wechat and not self.isMinimized():
                self._follow_minimized = True
                self.showMinimized()
            return
        if hasattr(geometry, "__dict__"):
            geometry = vars(geometry)
        if not isinstance(geometry, dict):
            return
        self._seen_wechat = True
        try:
            x, y = float(geometry["x"]), float(geometry["y"])
            width, height = float(geometry["width"]), float(geometry["height"])
            screen = QGuiApplication.screenAt(QPoint(int(x + width / 2), int(y + height / 2))) or self.screen()
            available = screen.availableGeometry()
            own_width = min(max(self.width(), self.config.min_width), self.config.max_width, available.width())
            desired_x = int(x + width + 5)
            if desired_x + own_width > available.right() + 1:
                desired_x = int(x - own_width - 5)
            desired_x = max(available.left(), min(desired_x, available.right() - own_width + 1))
            own_height = min(max(360, int(height)), available.height())
            desired_y = max(available.top(), min(int(y), available.bottom() - own_height + 1))
            self.setGeometry(desired_x, desired_y, own_width, own_height)
            if self._follow_minimized:
                self._follow_minimized = False
                self.showNormal()
        except (TypeError, ValueError, KeyError):
            return

    def closeEvent(self, event):
        self._closing = True
        self.timer.stop()
        self.backend.close()
        self.page.setWebChannel(None)
        self.view.setPage(QWebEnginePage(self.view))
        self.page.deleteLater()
        event.accept()


def run():
    config = load_config()
    handler = RotatingFileHandler(config.log_dir / "application.log", maxBytes=5 * 1024 * 1024,
                                  backupCount=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()],
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = QApplication(sys.argv)
    app.setApplicationName("中犇微信助手")
    app.setOrganizationName("ZhongBenAssistant")
    controller = build_controller(config)
    window = AssistantWindow(config, controller)
    window.show()
    return app.exec()
