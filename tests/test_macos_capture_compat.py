"""Cross-platform regression checks for the two native window-only backends.

These mock API contracts and failure handling. They are not substitutes for
running the packaged application against WeChat on each supported macOS.
"""
import builtins
from contextlib import nullcontext
from dataclasses import replace
import io
import sys
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from mac_assistant.models import AssistantError, WindowInfo
from mac_assistant.platform_macos import MacWeChat, _macos_version


class FrameworkCompatibilityTests(unittest.TestCase):
    def native_modules(self, with_capture=True):
        modules = {name: types.ModuleType(name) for name in (
            "AppKit", "ApplicationServices", "Quartz", "Vision", "Foundation", "objc")}
        if with_capture:
            modules["Quartz"].CGWindowListCreateImage = Mock()
        return modules

    def test_macos_12_and_13_do_not_import_screencapturekit(self):
        original_import = builtins.__import__
        for version in ("12.0", "12.2.1", "12.7.6", "13.7.8"):
            with self.subTest(version=version):
                imports = []

                def guarded_import(name, *args, **kwargs):
                    imports.append(name)
                    if name == "ScreenCaptureKit":
                        self.fail("Older macOS must not load ScreenCaptureKit")
                    return original_import(name, *args, **kwargs)

                with patch("mac_assistant.platform_macos.sys.platform", "darwin"), \
                        patch("mac_assistant.platform_macos.platform.mac_ver", return_value=(version, (), "")), \
                        patch.dict(sys.modules, self.native_modules()), \
                        patch("builtins.__import__", side_effect=guarded_import):
                    native = MacWeChat()._frameworks()
                self.assertIsNone(native[3])
                self.assertNotIn("ScreenCaptureKit", imports)

    def test_macos_14_and_newer_keep_screenshot_manager(self):
        for version in ("14.0", "15.7", "26.0"):
            with self.subTest(version=version):
                modules = self.native_modules(with_capture=False)
                sck = types.ModuleType("ScreenCaptureKit")
                sck.SCScreenshotManager = object()
                modules["ScreenCaptureKit"] = sck
                with patch("mac_assistant.platform_macos.sys.platform", "darwin"), \
                        patch("mac_assistant.platform_macos.platform.mac_ver", return_value=(version, (), "")), \
                        patch.dict(sys.modules, modules):
                    self.assertIs(MacWeChat()._frameworks()[3], sck)

    def test_missing_screenshot_capability_can_use_coregraphics(self):
        modules = self.native_modules()
        modules["ScreenCaptureKit"] = types.ModuleType("ScreenCaptureKit")
        with patch("mac_assistant.platform_macos.sys.platform", "darwin"), \
                patch("mac_assistant.platform_macos._macos_version", return_value=(14, 0)), \
                patch.dict(sys.modules, modules):
            self.assertIs(MacWeChat()._frameworks()[2], modules["Quartz"])

    def test_missing_both_capture_capabilities_is_actionable(self):
        with patch("mac_assistant.platform_macos.sys.platform", "darwin"), \
                patch("mac_assistant.platform_macos._macos_version", return_value=(12, 0)), \
                patch.dict(sys.modules, self.native_modules(with_capture=False)):
            with self.assertRaisesRegex(AssistantError, "截图接口"):
                MacWeChat()._frameworks()

    def test_older_macos_is_rejected_before_native_imports(self):
        with patch("mac_assistant.platform_macos.sys.platform", "darwin"), \
                patch("mac_assistant.platform_macos._macos_version", return_value=(11, 7)):
            with self.assertRaisesRegex(AssistantError, "macOS 12"):
                MacWeChat()._frameworks()

    def test_invalid_os_version_is_not_assumed_supported(self):
        with patch("mac_assistant.platform_macos.platform.mac_ver", return_value=("", (), "")):
            self.assertEqual(_macos_version(), (0, 0))


class CaptureBackendTests(unittest.TestCase):
    def setUp(self):
        self.window = WindowInfo(11, 22, "微信", -800, 70, 900, 700)
        self.adapter = MacWeChat()
        self.frame = object()
        self.quartz = types.SimpleNamespace(
            CGWindowListCreateImage=Mock(return_value=self.frame), CGRectNull=object(),
            kCGWindowListOptionIncludingWindow=8, kCGWindowImageBoundsIgnoreFraming=1)
        self.adapter._native = (None, None, self.quartz, None, None, None,
                                types.SimpleNamespace(autorelease_pool=nullcontext), Image)
        self.adapter._frameworks = Mock(side_effect=lambda: self.adapter._native)
        self.adapter._require_ready = Mock()
        self.adapter.windows = Mock(return_value=[self.window])
        self.adapter._decode_capture = Mock(return_value=(self.frame, "decoded"))

    def test_legacy_capture_is_bound_to_one_nonzero_window_id(self):
        result = self.adapter._capture(self.window)
        self.assertEqual(result, (self.frame, "decoded"))
        self.quartz.CGWindowListCreateImage.assert_called_once_with(
            self.quartz.CGRectNull, 8, 11, 1)
        self.assertEqual(self.adapter.windows.call_count, 2)
        self.assertEqual(self.adapter._require_ready.call_count, 2)

    def test_screenshot_manager_capability_wins_over_legacy(self):
        sck = types.SimpleNamespace(SCScreenshotManager=object())
        native = list(self.adapter._native)
        native[3] = sck
        self.adapter._native = tuple(native)
        self.adapter._capture_screenshot_manager = Mock(return_value=self.frame)
        self.adapter._capture(self.window)
        self.adapter._capture_screenshot_manager.assert_called_once_with(self.window, sck)
        self.quartz.CGWindowListCreateImage.assert_not_called()

    def test_missing_screenshot_manager_uses_legacy_api(self):
        native = list(self.adapter._native)
        native[3] = types.SimpleNamespace()
        self.adapter._native = tuple(native)
        self.adapter._capture(self.window)
        self.quartz.CGWindowListCreateImage.assert_called_once()

    def test_zero_window_id_never_becomes_a_desktop_capture(self):
        with self.assertRaisesRegex(AssistantError, "标识"):
            self.adapter._capture(replace(self.window, window_id=0))
        self.quartz.CGWindowListCreateImage.assert_not_called()

    def test_permission_denied_does_not_capture(self):
        self.adapter._require_ready.side_effect = AssistantError("屏幕录制权限未就绪")
        with self.assertRaisesRegex(AssistantError, "权限"):
            self.adapter._capture(self.window)
        self.quartz.CGWindowListCreateImage.assert_not_called()

    def test_revoked_permission_discards_captured_frame(self):
        self.adapter._require_ready.side_effect = [None, AssistantError("屏幕录制权限未就绪")]
        with self.assertRaisesRegex(AssistantError, "权限"):
            self.adapter._capture(self.window)
        self.adapter._decode_capture.assert_not_called()

    def test_reused_window_id_for_another_pid_is_rejected(self):
        self.adapter.windows.return_value = [replace(self.window, pid=99)]
        with self.assertRaisesRegex(AssistantError, "变化"):
            self.adapter._capture(self.window)
        self.quartz.CGWindowListCreateImage.assert_not_called()

    def test_window_closed_during_capture_discards_frame(self):
        self.adapter.windows.side_effect = [[self.window], []]
        with self.assertRaisesRegex(AssistantError, "变化"):
            self.adapter._capture(self.window)
        self.adapter._decode_capture.assert_not_called()

    def test_resized_or_moved_window_discards_frame(self):
        for updated in (replace(self.window, width=1000), replace(self.window, x=-700)):
            with self.subTest(window=updated):
                self.adapter.windows.side_effect = [[self.window], [updated]]
                with self.assertRaisesRegex(AssistantError, "调整大小"):
                    self.adapter._capture(self.window)
        self.adapter._decode_capture.assert_not_called()

    def test_native_capture_error_has_actionable_message(self):
        self.quartz.CGWindowListCreateImage.side_effect = RuntimeError("capture failure")
        with self.assertRaisesRegex(AssistantError, "屏幕录制权限"):
            self.adapter._capture(self.window)
        self.adapter._decode_capture.assert_not_called()

    def test_screenshot_manager_filter_checks_window_id_and_owner_pid(self):
        other = types.SimpleNamespace(windowID=lambda: 11,
                                      owningApplication=lambda: types.SimpleNamespace(processID=lambda: 99))
        target = types.SimpleNamespace(windowID=lambda: 11,
                                       owningApplication=lambda: types.SimpleNamespace(processID=lambda: 22))
        shareable = types.SimpleNamespace(windows=lambda: [other, target])
        get_content = Mock(side_effect=lambda exclude, only_screen, callback: callback(shareable, None))
        native_filter = Mock()
        native_config = Mock()
        screenshot = Mock(side_effect=lambda content_filter, config, callback: callback(self.frame, None))
        sck = types.SimpleNamespace(
            SCShareableContent=types.SimpleNamespace(
                getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_=get_content),
            SCContentFilter=Mock(), SCStreamConfiguration=Mock(),
            SCScreenshotManager=types.SimpleNamespace(
                captureImageWithFilter_configuration_completionHandler_=screenshot))
        sck.SCContentFilter.alloc.return_value.initWithDesktopIndependentWindow_.return_value = native_filter
        sck.SCStreamConfiguration.alloc.return_value.init.return_value = native_config
        self.assertIs(self.adapter._capture_screenshot_manager(self.window, sck), self.frame)
        sck.SCContentFilter.alloc.return_value.initWithDesktopIndependentWindow_.assert_called_once_with(target)
        screenshot.assert_called_once_with(native_filter, native_config, unittest.mock.ANY)


class CaptureDecodeTests(unittest.TestCase):
    def setUp(self):
        self.window = WindowInfo(11, 22, "微信", 0, 0, 900, 700)
        self.quartz = types.SimpleNamespace(CGImageGetWidth=Mock(return_value=900),
                                            CGImageGetHeight=Mock(return_value=700))
        self.appkit = Mock()
        self.bitmap = self.appkit.NSBitmapImageRep.alloc.return_value.initWithCGImage_.return_value

    def encoded_image(self, size, solid=False):
        image = Image.new("RGB", size, "white")
        if not solid:
            ImageDraw.Draw(image).rectangle((5, 5, 40, 40), fill="green")
        encoded = io.BytesIO()
        image.save(encoded, format="PNG")
        self.bitmap.representationUsingType_properties_.return_value = encoded.getvalue()

    def test_null_frame_is_reported_before_reading_dimensions(self):
        with self.assertRaisesRegex(AssistantError, "屏幕录制权限"):
            MacWeChat._decode_capture(None, self.window, self.appkit, self.quartz, Image)
        self.quartz.CGImageGetWidth.assert_not_called()

    def test_zero_size_is_rejected(self):
        self.quartz.CGImageGetWidth.return_value = 0
        with self.assertRaisesRegex(AssistantError, "尺寸无效"):
            MacWeChat._decode_capture(object(), self.window, self.appkit, self.quartz, Image)

    def test_distorted_aspect_ratio_is_rejected(self):
        self.quartz.CGImageGetWidth.return_value = 1800
        with self.assertRaisesRegex(AssistantError, "尺寸无效"):
            MacWeChat._decode_capture(object(), self.window, self.appkit, self.quartz, Image)

    def test_retina_frame_keeps_native_resolution(self):
        self.quartz.CGImageGetWidth.return_value = 1800
        self.quartz.CGImageGetHeight.return_value = 1400
        self.encoded_image((1800, 1400))
        frame = object()
        captured, image = MacWeChat._decode_capture(frame, self.window, self.appkit, self.quartz, Image)
        self.assertIs(captured, frame)
        self.assertEqual(image.size, (1800, 1400))

    def test_solid_blank_frame_is_rejected(self):
        self.encoded_image((900, 700), solid=True)
        with self.assertRaisesRegex(AssistantError, "空白"):
            MacWeChat._decode_capture(object(), self.window, self.appkit, self.quartz, Image)

    def test_failed_png_encoding_is_actionable(self):
        self.bitmap.representationUsingType_properties_.return_value = None
        with self.assertRaisesRegex(AssistantError, "解码"):
            MacWeChat._decode_capture(object(), self.window, self.appkit, self.quartz, Image)

    def test_decoded_size_mismatch_is_rejected(self):
        self.encoded_image((800, 600))
        with self.assertRaisesRegex(AssistantError, "尺寸不一致"):
            MacWeChat._decode_capture(object(), self.window, self.appkit, self.quartz, Image)


if __name__ == "__main__":
    unittest.main()
