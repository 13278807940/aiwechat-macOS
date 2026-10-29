"""macOS 14+ WeChat adapter (AX + window-only ScreenCaptureKit + Vision).

No macOS frameworks are imported on other platforms. All coordinates used for
input are Quartz screen points; captured pixels are mapped back to those points.
The adapter pauses on ambiguous identity, drafts, missing permissions or a locked
session. It never falls back to capturing the desktop.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import re
import sys
import threading
import time
from typing import Any

from .models import AssistantError, WindowInfo


_BUNDLE_IDS = {"com.tencent.xinWeChat", "com.tencent.WeChat", "com.tencent.Weixin", "com.tencent.wechat"}
_UTILITY_NAMES = {"微信", "WeChat", "Weixin", "聊天", "Chats", "消息", "通讯录", "发现", "设置",
                  "搜索", "Search", "发送", "Send", "语音通话", "视频通话", "聊天信息", "更多",
                  "Contacts", "Settings", "服务号", "公众号", "订阅号消息", "微信团队"}
_TIME = re.compile(r"^(?:\d{1,2}:\d{2}|(?:昨天|今天|星期[一二三四五六日天])(?:\s+\d{1,2}:\d{2})?|\d{4}年\d+月\d+日(?:.*)?)$")


def _clean_name(value: Any) -> str:
    text = str(value or "").strip()
    while text and (text[-1] in "?？\ufffd" or text[-1].isspace()):
        text = text[:-1]
    return text.strip()


def _valid_name(value: Any) -> bool:
    text = _clean_name(value)
    return bool(text and len(text) <= 100 and text not in _UTILITY_NAMES and not _TIME.match(text)
                and not text.startswith(("正在输入", "对方正在", "搜索")))


@dataclass(frozen=True)
class TextSpan:
    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float = 1.0


def _red_components(image, box):
    """Small, compact red badges; large red avatars cannot be badge candidates."""
    left, top, right, bottom = [int(v) for v in box]
    left, top = max(0, left), max(0, top)
    right, bottom = min(image.width, right), min(image.height, bottom)
    if right <= left or bottom <= top:
        return []
    pixels = image.convert("RGB").load()
    points = set()
    for y in range(top, bottom):
        for x in range(left, right):
            r, g, b = pixels[x, y]
            if r >= 175 and r - g >= 65 and g <= 145 and b <= 175:
                points.add((x, y))
    result = []
    while points:
        pending = [points.pop()]
        component = []
        while pending:
            x, y = pending.pop()
            component.append((x, y))
            for p in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if p in points:
                    points.remove(p)
                    pending.append(p)
        if len(component) < 5:
            continue
        x0, x1 = min(p[0] for p in component), max(p[0] for p in component)
        y0, y1 = min(p[1] for p in component), max(p[1] for p in component)
        w, h = x1 - x0 + 1, y1 - y0 + 1
        if 3 <= w <= 30 and 3 <= h <= 30 and 0.5 <= w / h <= 2.1 and len(component) / (w * h) >= 0.4:
            result.append((x0, y0, x1 + 1, y1 + 1))
    return sorted(result, key=lambda item: item[1])


def _message_lines(spans, chat_left, input_top, width, image, sx=1.0, sy=1.0):
    """Classify visible bubble text and reject timestamps/toolbars/outgoing text."""
    result = []
    pixels = image.convert("RGB")
    for span in spans:
        text = span.text.strip()
        if (not text or span.confidence < 0.4 or _TIME.match(text)
                or span.y < 86 or span.y + span.height >= input_top - 7
                or span.x < chat_left + 35 or span.x + span.width > width - 8
                or text in {"以下为新消息", "以上是打招呼的内容", "你撤回了一条消息", "对方撤回了一条消息"}):
            continue
        # Own outgoing bubbles are green in both supported WeChat themes. Sample
        # above/below text, not a central glyph, so long outgoing bubbles that
        # begin near the left edge are still excluded.
        colors = []
        for px, py in ((span.x + 2, span.y - 3), (span.x + span.width / 2, span.y - 3),
                       (span.x - 4, span.y + span.height / 2),
                       (span.x + span.width / 2, span.y + span.height + 3)):
            ix, iy = int(px * sx), int(py * sy)
            if 0 <= ix < pixels.width and 0 <= iy < pixels.height:
                colors.append(pixels.getpixel((ix, iy)))
        outgoing = any(g > r + 18 and g > b + 15 and g > 80 for r, g, b in colors)
        if not outgoing and span.x > chat_left + (width - chat_left) * 0.62:
            continue
        # Centered notices have no avatar/bubble anchor. They must not become
        # customer questions, while a left-hand single digit remains valid.
        if not outgoing and span.x > chat_left + (width - chat_left) * 0.36 and span.width < 100:
            continue
        result.append((span.y, span.x, text, "outgoing" if outgoing else "incoming"))
    return sorted(result)


class MacWeChat:
    def __init__(self):
        self._native = None
        self._lock = threading.RLock()
        self._last_nav_badge = None
        self._unread_epoch = 0
        self._owned_popups = set()
        self._owned_drafts = {}

    def _frameworks(self):
        if sys.platform != "darwin":
            raise AssistantError("微信原生操作仅支持 macOS 14 或更新版本；Windows 只能检查代码或发起远程构建")
        if self._native is None:
            try:
                import AppKit
                import ApplicationServices
                import Quartz
                import ScreenCaptureKit
                import Vision
                import Foundation
                import objc
                from PIL import Image
            except ImportError as exc:
                raise AssistantError("缺少 macOS 依赖，请先执行 requirements-macos.txt 的安装命令") from exc
            if not hasattr(ScreenCaptureKit, "SCScreenshotManager"):
                raise AssistantError("需要 macOS 14 或更新版本的 ScreenCaptureKit")
            self._native = (AppKit, ApplicationServices, Quartz, ScreenCaptureKit, Vision, Foundation, objc, Image)
        return self._native

    def permission_status(self, prompt=False):
        if sys.platform != "darwin":
            return {"supported": False, "accessibility": False, "screen_recording": False,
                    "message": "当前系统不是 macOS，原生微信操作不可用"}
        try:
            appkit, ax, quartz, *_ = self._frameworks()
            access = bool(ax.AXIsProcessTrustedWithOptions({ax.kAXTrustedCheckOptionPrompt: bool(prompt)}))
            recording = bool(quartz.CGPreflightScreenCaptureAccess())
            if prompt and not recording:
                recording = bool(quartz.CGRequestScreenCaptureAccess())
            return {"supported": True, "accessibility": access, "screen_recording": recording,
                    "message": "权限已就绪" if access and recording else "请在系统设置中允许辅助功能与屏幕录制，然后重新启动助手"}
        except AssistantError as exc:
            return {"supported": False, "accessibility": False, "screen_recording": False, "message": str(exc)}

    def _require_ready(self):
        status = self.permission_status()
        if not all(status.get(key) for key in ("supported", "accessibility", "screen_recording")):
            raise AssistantError(status["message"])
        quartz = self._frameworks()[2]
        session = quartz.CGSessionCopyCurrentDictionary() or {}
        if session.get("CGSSessionScreenIsLocked") or not session.get("kCGSessionOnConsoleKey", True):
            raise AssistantError("系统已锁定或切换用户，已暂停微信操作")

    def _applications(self):
        appkit = self._frameworks()[0]
        return {int(app.processIdentifier()): app for app in appkit.NSWorkspace.sharedWorkspace().runningApplications()
                if str(app.bundleIdentifier() or "") in _BUNDLE_IDS and not app.isTerminated()}

    def windows(self):
        quartz = self._frameworks()[2]
        applications = self._applications()
        options = quartz.kCGWindowListOptionOnScreenOnly | quartz.kCGWindowListExcludeDesktopElements
        result = []
        for item in quartz.CGWindowListCopyWindowInfo(options, quartz.kCGNullWindowID) or []:
            pid = int(item.get(quartz.kCGWindowOwnerPID, 0))
            if pid not in applications or applications[pid].isHidden() or int(item.get(quartz.kCGWindowLayer, -1)) != 0:
                continue
            frame = item.get(quartz.kCGWindowBounds, {})
            w, h = float(frame.get("Width", 0)), float(frame.get("Height", 0))
            if w < 360 or h < 350:
                continue
            result.append(WindowInfo(window_id=int(item[quartz.kCGWindowNumber]), pid=pid,
                                     title=str(item.get(quartz.kCGWindowName) or ""),
                                     x=float(frame.get("X", 0)), y=float(frame.get("Y", 0)), width=w, height=h))
        return sorted(result, key=lambda window: window.width * window.height, reverse=True)

    def main_window(self):
        windows = self.windows()
        return windows[0] if windows else None

    def window_geometry(self):
        return self.main_window()

    def _window(self):
        self._require_ready()
        window = self.main_window()
        if window is None:
            raise AssistantError("未找到已显示的微信聊天窗口；请登录微信并恢复主窗口")
        return window

    def _ax_attr(self, element, key, default=None):
        ax = self._frameworks()[1]
        try:
            error, value = ax.AXUIElementCopyAttributeValue(element, key, None)
            return value if error == 0 and value is not None else default
        except Exception:
            return default

    def _ax_frame(self, element):
        ax = self._frameworks()[1]
        try:
            pos = self._ax_attr(element, "AXPosition")
            size = self._ax_attr(element, "AXSize")
            okp, p = ax.AXValueGetValue(pos, ax.kAXValueCGPointType, None)
            oks, s = ax.AXValueGetValue(size, ax.kAXValueCGSizeType, None)
            if okp and oks:
                return float(p.x), float(p.y), float(s.width), float(s.height)
        except Exception:
            pass
        return None

    def _ax_window(self, window):
        ax = self._frameworks()[1]
        app = ax.AXUIElementCreateApplication(window.pid)
        ax.AXUIElementSetMessagingTimeout(app, 0.12)
        for element in self._ax_attr(app, "AXWindows", []) or []:
            frame = self._ax_frame(element)
            if frame and abs(frame[0] - window.x) < 6 and abs(frame[1] - window.y) < 6 and abs(frame[2] - window.width) < 8:
                return element
        return None

    def _nodes(self, window):
        element = self._ax_window(window)
        if element is None:
            return []
        pending, result = [(element, 0)], []
        deadline = time.monotonic() + 0.9
        while pending and len(result) < 700 and time.monotonic() < deadline:
            node, depth = pending.pop()
            if depth > 14:
                continue
            role = str(self._ax_attr(node, "AXRole", ""))
            texts = [str(self._ax_attr(node, key, "") or "") for key in ("AXTitle", "AXDescription", "AXValue")]
            result.append({"element": node, "role": role, "texts": texts, "frame": self._ax_frame(node)})
            children = self._ax_attr(node, "AXChildren", []) or []
            pending.extend((child, depth + 1) for child in reversed(list(children)[:120]))
        return result

    def _layout(self, window, nodes):
        # AX input geometry wins over ratio fallback. The ratio fallback is
        # used for recognition only; editing still requires input read-back.
        candidates = []
        for node in nodes:
            frame = node["frame"]
            if frame and node["role"] in ("AXTextArea", "AXTextField"):
                x, y, w, h = frame
                if x > window.x + 160 and y > window.y + window.height * 0.52 and w > 140:
                    candidates.append(node)
        input_node = max(candidates, key=lambda item: item["frame"][2] * item["frame"][3], default=None)
        if input_node:
            frame = input_node["frame"]
            chat_left = max(210.0, frame[0] - window.x - 12)
            input_top = frame[1] - window.y - 12
        else:
            chat_left = min(340.0, max(250.0, window.width * 0.30))
            input_top = window.height - max(140.0, window.height * 0.24)
        return {"chat_left": chat_left, "input_top": input_top, "input": input_node, "nav_width": 68.0}

    @staticmethod
    def _await(start, timeout=3.0):
        ready = threading.Event()
        state = {}
        def done(value, error):
            state.update(value=value, error=error)
            ready.set()
        start(done)
        if not ready.wait(timeout):
            raise AssistantError("微信窗口截图超时，请恢复窗口后重试")
        if state.get("error") is not None or state.get("value") is None:
            raise AssistantError("无法获取微信窗口画面，请检查屏幕录制权限并重启助手")
        return state["value"]

    def _capture(self, window):
        appkit, _, quartz, sck, _, _, objc, image_module = self._frameworks()
        with objc.autorelease_pool():
            shareable = self._await(lambda done: sck.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(True, False, done))
            native_window = next((item for item in shareable.windows() if int(item.windowID()) == window.window_id
                                  and int(item.owningApplication().processID()) == window.pid), None)
            if native_window is None:
                raise AssistantError("微信窗口已发生变化，请重新选择联系人")
            content_filter = sck.SCContentFilter.alloc().initWithDesktopIndependentWindow_(native_window)
            config = sck.SCStreamConfiguration.alloc().init()
            # Produce point-sized pixels for stable layout. Window-only capture
            # remains valid when another app overlaps WeChat on the desktop.
            config.setWidth_(max(1, int(round(window.width))))
            config.setHeight_(max(1, int(round(window.height))))
            config.setShowsCursor_(False)
            if hasattr(config, "setIgnoreShadowsSingleWindow_"):
                config.setIgnoreShadowsSingleWindow_(True)
            cgimage = self._await(lambda done: sck.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(content_filter, config, done))
            width, height = int(quartz.CGImageGetWidth(cgimage)), int(quartz.CGImageGetHeight(cgimage))
            if width < 100 or height < 100:
                raise AssistantError("微信窗口截图尺寸无效，请恢复微信窗口")
            # AppKit encodes the correct orientation/byte order across CPUs.
            bitmap = appkit.NSBitmapImageRep.alloc().initWithCGImage_(cgimage)
            encoded = bitmap.representationUsingType_properties_(appkit.NSBitmapImageFileTypePNG, {})
            if encoded is None:
                raise AssistantError("无法解码微信窗口图像")
            with image_module.open(io.BytesIO(bytes(encoded))) as opened:
                image = opened.convert("RGB")
            return cgimage, image

    def _ocr(self, cgimage, window):
        _, _, quartz, _, vision, _, objc, _ = self._frameworks()
        with objc.autorelease_pool():
            request = vision.VNRecognizeTextRequest.alloc().init()
            request.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelAccurate)
            request.setRecognitionLanguages_(["zh-Hans", "en-US"])
            request.setUsesLanguageCorrection_(False)
            handler = vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimage, {})
            ok, error = handler.performRequests_error_([request], None)
            if not ok:
                raise AssistantError("Apple Vision 文字识别失败")
            spans = []
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if not candidates:
                    continue
                candidate, box = candidates[0], observation.boundingBox()
                spans.append(TextSpan(str(candidate.string()), float(box.origin.x) * window.width,
                                      (1 - float(box.origin.y) - float(box.size.height)) * window.height,
                                      float(box.size.width) * window.width, float(box.size.height) * window.height,
                                      float(candidate.confidence())))
            return spans

    def _contact_from(self, window, nodes, layout, spans=None):
        left = layout["chat_left"]
        candidates = []
        for node in nodes:
            frame = node["frame"]
            if not frame or node["role"] not in ("AXStaticText", "AXButton", "AXHeading"):
                continue
            x, y, w, h = frame
            if left + 2 <= x - window.x <= window.width - 100 and 25 <= y - window.y <= 95 and h < 48:
                name = next((_clean_name(text) for text in node["texts"] if _valid_name(text)), "")
                if name:
                    candidates.append((y - window.y, x - window.x, name))
        if not candidates and spans is not None:
            for span in spans:
                if (left + 2 <= span.x <= window.width - 100 and 26 <= span.y <= 95
                        and span.height < 42 and span.confidence >= 0.45 and _valid_name(span.text)):
                    candidates.append((span.y, span.x, _clean_name(span.text)))
        if not candidates:
            raise AssistantError("未识别到聊天顶部的联系人名称，请选中联系人后重试")
        candidates.sort(key=lambda item: (item[0], item[1]))
        name = candidates[0][2]
        # A friend can literally be named “兄弟(5)”; a numeric suffix is not a
        # reliable group marker. Require an explicit group header control.
        group = any(node.get("frame") and
                    20 <= node["frame"][1] - window.y <= 100 and
                    node["frame"][0] - window.x > left and
                    any(text in {"群聊信息", "群成员", "Group Info", "Group Members"}
                        for text in node["texts"]) for node in nodes)
        return {"name": name, "weixinhao": "", "type": "group" if group else "friend",
                "window_id": window.window_id, "pid": window.pid}

    def contact(self):
        with self._lock:
            window = self._window()
            nodes = self._nodes(window)
            layout = self._layout(window, nodes)
            try:
                return self._contact_from(window, nodes, layout)
            except AssistantError:
                cgimage, _ = self._capture(window)
                return self._contact_from(window, nodes, layout, self._ocr(cgimage, window))

    def _click(self, window, x, y, double=False):
        _, _, quartz, *_ = self._frameworks()
        self._activate(window)
        for count in range(1, 3 if double else 2):
            location = (window.x + x, window.y + y)
            for event_type in (quartz.kCGEventLeftMouseDown, quartz.kCGEventLeftMouseUp):
                event = quartz.CGEventCreateMouseEvent(None, event_type, location, quartz.kCGMouseButtonLeft)
                quartz.CGEventSetIntegerValueField(event, quartz.kCGMouseEventClickState, count)
                quartz.CGEventPost(quartz.kCGHIDEventTap, event)
            if double and count == 1:
                time.sleep(0.08)

    def _activate(self, window):
        appkit = self._frameworks()[0]
        self._require_ready()
        application = self._applications().get(window.pid)
        if application is None:
            raise AssistantError("微信进程已退出")
        application.activateWithOptions_(appkit.NSApplicationActivateIgnoringOtherApps)
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            active = appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
            if active and int(active.processIdentifier()) == window.pid:
                return
            time.sleep(0.04)
        raise AssistantError("无法激活微信，已暂停输入")

    def _row_badge(self, image, window, layout):
        # Only the small strip at the upper-right corner of list avatars is
        # inspected; a red face in the avatar center does not satisfy it.
        nav = layout["nav_width"]
        sx, sy = image.width / window.width, image.height / window.height
        box = ((nav + 33) * sx, 73 * sy, min(nav + 65, layout["chat_left"] - 90) * sx,
               min(window.height - 12, image.height / sy) * sy)
        badges = _red_components(image, box)
        if not badges:
            return None
        x0, y0, x1, y1 = badges[0]
        return {"x": (x0 + x1) / (2 * sx), "y": (y0 + y1) / (2 * sy)}

    def unread_candidate(self):
        with self._lock:
            window = self._window()
            nodes = self._nodes(window)
            layout = self._layout(window, nodes)
            cgimage, image = self._capture(window)
            sx, sy = image.width / window.width, image.height / window.height
            nav_box = (24 * sx, 64 * sy, 65 * sx, 147 * sy)
            nav_badges = _red_components(image, nav_box)
            candidate = self._row_badge(image, window, layout)
            if not nav_badges and not candidate:
                self._last_nav_badge = None
                return None
            spans = self._ocr(cgimage, window)
            # Retain a row's name before message-button double click clears its
            # unread badge by displaying the conversation.
            selected_name = ""
            if candidate:
                row_names = [span for span in spans if layout["nav_width"] + 63 <= span.x < layout["chat_left"] - 15
                             and candidate["y"] - 10 <= span.y <= candidate["y"] + 24 and _valid_name(span.text)]
                if row_names:
                    selected_name = _clean_name(min(row_names, key=lambda span: span.y).text)
            # Include the badge pixels (including the white count) so a new
            # notification count is a new edge even when its bounding box is
            # unchanged. Stationary badges never cause periodic double-clicks.
            badge_token = tuple((box, hashlib.sha256(image.crop(box).tobytes()).hexdigest()) for box in nav_badges)
            if nav_badges and badge_token != self._last_nav_badge:
                self._last_nav_badge = badge_token
                self._unread_epoch += 1
                button = next((node for node in nodes if node["frame"] and node["role"] == "AXButton"
                               and node["frame"][0] < window.x + layout["nav_width"]
                               and any(text in ("聊天", "Chats", "消息") for text in node["texts"])), None)
                x, y = (34.0, 100.0) if button is None else (
                    button["frame"][0] - window.x + button["frame"][2] / 2,
                    button["frame"][1] - window.y + button["frame"][3] / 2)
                self._click(window, x, y, double=True)
                time.sleep(0.23)
                # Re-read row placement, not old coordinates: double-click can
                # scroll the list. If its badge vanished, find retained name.
                cgimage, image = self._capture(window)
                spans = self._ocr(cgimage, window)
                candidate = self._row_badge(image, window, layout)
            if selected_name:
                row = next((span for span in spans if _clean_name(span.text) == selected_name
                            and layout["nav_width"] + 55 < span.x < layout["chat_left"] - 12), None)
                if row:
                    self._click(window, min(layout["chat_left"] - 25, row.x + 35), row.y + row.height / 2)
                    time.sleep(0.18)
            elif candidate:
                self._click(window, min(layout["chat_left"] - 30, layout["nav_width"] + 105), candidate["y"] + 15)
                time.sleep(0.18)
            elif not nav_badges:
                return None
            contact = self.contact()
            if selected_name and contact["name"] != selected_name:
                raise AssistantError("未读联系人与聊天标题不一致，已取消本轮操作")
            return dict(contact, unread_epoch=self._unread_epoch)

    def read_conversation(self):
        with self._lock:
            window = self._window()
            nodes = self._nodes(window)
            layout = self._layout(window, nodes)
            cgimage, image = self._capture(window)
            spans = self._ocr(cgimage, window)
            contact = self._contact_from(window, nodes, layout, spans)
            lines = _message_lines(spans, layout["chat_left"], layout["input_top"], window.width,
                                   image, image.width / window.width, image.height / window.height)
            if not lines:
                return {"contact": contact, "message": "", "fingerprint": "", "window_id": window.window_id}
            last = lines[-1]
            if last[3] != "incoming":
                return {"contact": contact, "message": "", "fingerprint": "", "window_id": window.window_id}
            # Collect wrapped lines belonging to the last incoming bubble.
            selected = [last]
            for line in reversed(lines[:-1]):
                if line[3] != "incoming" or selected[0][0] - line[0] > 32 or abs(line[1] - last[1]) > 24:
                    break
                selected.insert(0, line)
            message = "\n".join(line[2] for line in selected)
            check = self.contact()
            if (check["name"], check["pid"], check["window_id"]) != (contact["name"], window.pid, window.window_id):
                raise AssistantError("识别时联系人发生变化，请重试")
            history = "|".join(f"{line[3]}:{line[2]}" for line in lines[-10:])
            # A badge for a different conversation must not manufacture a new
            # identity for this conversation's unchanged last message.
            fingerprint = hashlib.sha256(f"{window.pid}|{window.window_id}|{contact['name']}|{history}".encode()).hexdigest()
            return {"contact": contact, "message": message, "fingerprint": fingerprint, "window_id": window.window_id}

    def _key(self, window, keycode, command=False):
        appkit, _, quartz, *_ = self._frameworks()
        active = appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if not active or int(active.processIdentifier()) != window.pid:
            raise AssistantError("微信不是当前活动应用，已暂停键盘操作")
        for down in (True, False):
            event = quartz.CGEventCreateKeyboardEvent(None, keycode, down)
            if command:
                quartz.CGEventSetFlags(event, quartz.kCGEventFlagMaskCommand)
            quartz.CGEventPost(quartz.kCGHIDEventTap, event)

    def write_reply(self, contact_name, text, send=False):
        text = str(text or "")
        if not text.strip() or len(text) > 12000:
            raise AssistantError("回复内容为空或过长")
        with self._lock:
            window = self._window()
            expected = _clean_name(contact_name)
            contact = self.contact()
            if contact["name"] != expected or contact["window_id"] != window.window_id:
                raise AssistantError("当前联系人已改变，已取消输入与发送")
            nodes = self._nodes(window)
            layout = self._layout(window, nodes)
            input_node = layout["input"]
            if input_node is None:
                raise AssistantError("未找到可校验的微信输入框，请选中普通文字聊天后重试")
            ax = self._frameworks()[1]
            element = input_node["element"]
            raw_value = self._ax_attr(element, "AXValue", None)
            if raw_value is None:
                raise AssistantError("无法读取微信输入框，已暂停写入")
            current = str(raw_value)
            draft_key = (window.pid, window.window_id, expected)
            owned = self._owned_drafts.get(draft_key)
            if current and current.strip() != text.strip() and current != owned:
                raise AssistantError("微信输入框已有其他草稿，为避免覆盖已暂停操作")
            self._activate(window)
            frame = input_node["frame"]
            self._click(window, frame[0] - window.x + min(frame[2] / 2, 180), frame[1] - window.y + min(frame[3] / 2, 40))
            before_edit = self.contact()
            if before_edit["name"] != expected or before_edit["window_id"] != window.window_id:
                raise AssistantError("聚焦输入框时联系人发生变化，已取消写入")
            if self._ax_attr(element, "AXValue", None) != raw_value:
                raise AssistantError("微信输入框草稿发生变化，已暂停写入")
            if current.strip() != text.strip():
                error = ax.AXUIElementSetAttributeValue(element, "AXValue", text)
                if error != 0:
                    self._paste(window, text, replace=bool(current))
                time.sleep(0.1)
            readback = self._ax_attr(element, "AXValue", None)
            if readback is None or str(readback).strip() != text.strip():
                raise AssistantError("无法核对微信输入框内容，已保留草稿；请人工检查后发送")
            check = self.contact()
            if check["name"] != expected or check["window_id"] != window.window_id:
                raise AssistantError("输入过程中联系人发生变化，已取消发送")
            self._owned_drafts[draft_key] = text
            if send:
                self._key(window, 36)  # kVK_Return; user may configure Command+Return.
                time.sleep(0.15)
                if str(self._ax_attr(element, "AXValue", "") or "").strip():
                    raise AssistantError("微信输入框未清空，未确认发送成功；请检查微信发送快捷键设置")
                self._owned_drafts.pop(draft_key, None)
            return {"name": expected, "window_id": window.window_id, "prepared": True, "sent": bool(send)}

    def _paste(self, window, text, replace=False):
        appkit = self._frameworks()[0]
        clipboard = appkit.NSPasteboard.generalPasteboard()
        previous = [(str(kind), clipboard.dataForType_(kind)) for kind in clipboard.types() or []]
        clipboard.clearContents()
        clipboard.setString_forType_(text, appkit.NSPasteboardTypeString)
        count = clipboard.changeCount()
        try:
            if replace:
                self._key(window, 0, command=True)  # Select only our verified draft.
            self._key(window, 9, command=True)  # kVK_ANSI_V
            time.sleep(0.16)
        finally:
            if clipboard.changeCount() == count:
                clipboard.clearContents()
                for kind, data in previous:
                    if data is not None:
                        clipboard.setData_forType_(data, kind)

    def close_contact_window(self, window_id):
        # This adapter uses the main chat. It never closes a window opened by
        # the user and never closes the main WeChat window.
        if int(window_id) not in self._owned_popups:
            return False
        return False
