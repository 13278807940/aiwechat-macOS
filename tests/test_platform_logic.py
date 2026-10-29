import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from mac_assistant.models import AssistantError, WindowInfo
from mac_assistant.platform_macos import MacWeChat, TextSpan, _clean_name, _message_lines, _red_components


class PlatformLogicTests(unittest.TestCase):
    def setUp(self):
        self.window = WindowInfo(11, 22, "微信", -800, 70, 900, 700)
        self.adapter = MacWeChat()

    def test_non_macos_import_has_actionable_permission_result(self):
        with patch("mac_assistant.platform_macos.sys.platform", "win32"):
            self.assertFalse(self.adapter.permission_status()["supported"])
            with self.assertRaises(AssistantError):
                self.adapter.windows()

    def test_name_cleaning_preserves_group_suffix_and_internal_punctuation(self):
        self.assertEqual(_clean_name(" 兄弟(5)? ？� "), "兄弟(5)")
        self.assertEqual(_clean_name("问?题"), "问?题")

    def test_red_avatar_cannot_be_a_badge_component(self):
        image = Image.new("RGB", (160, 200), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 30, 65, 75), fill=(245, 20, 20))
        draw.ellipse((108, 80, 121, 93), fill=(245, 40, 45))
        badges = _red_components(image, (0, 0, 160, 200))
        self.assertEqual(len(badges), 1)
        self.assertEqual(badges[0], (108, 80, 122, 94))

    def test_message_selection_keeps_single_digit_and_rejects_timestamp(self):
        image = Image.new("RGB", (900, 700), (250, 250, 250))
        spans = [TextSpan("10:01", 560, 110, 35, 15), TextSpan("1", 305, 190, 10, 18),
                 TextSpan("已输入的草稿", 310, 590, 90, 18)]
        lines = _message_lines(spans, 260, 540, 900, image)
        self.assertEqual([line[2] for line in lines], ["1"])

    def test_long_outgoing_bubble_is_classified_by_color_not_left_edge(self):
        image = Image.new("RGB", (900, 700), (250, 250, 250))
        ImageDraw.Draw(image).rectangle((295, 190, 820, 230), fill=(120, 225, 100))
        spans = [TextSpan("客户问题", 305, 130, 80, 18), TextSpan("很长的自己发出的回复", 305, 200, 460, 18)]
        lines = _message_lines(spans, 260, 540, 900, image)
        self.assertEqual(lines[-1][3], "outgoing")

    def test_ax_title_only_accepts_chat_header_and_not_settings(self):
        nodes = [
            {"role": "AXButton", "texts": ["设置"], "frame": (-520, 115, 40, 20)},
            {"role": "AXStaticText", "texts": ["兄弟(5)?"], "frame": (-500, 120, 140, 20)},
            {"role": "AXStaticText", "texts": ["上一个列表联系人"], "frame": (-710, 120, 140, 20)},
        ]
        friend = self.adapter._contact_from(self.window, nodes, {"chat_left": 260})
        self.assertEqual(friend["name"], "兄弟(5)")
        self.assertEqual(friend["window_id"], 11)
        self.assertEqual(friend["pid"], 22)
        self.assertEqual(friend["type"], "friend")

    def test_send_rejects_contact_mismatch_before_input(self):
        self.adapter._window = Mock(return_value=self.window)
        self.adapter.contact = Mock(return_value={"name": "别人", "window_id": 11})
        self.adapter._nodes = Mock()
        with self.assertRaises(AssistantError):
            self.adapter.write_reply("目标客户", "回复", send=True)
        self.adapter._nodes.assert_not_called()

    def test_send_rejects_existing_draft_without_overwriting(self):
        self.adapter._window = Mock(return_value=self.window)
        self.adapter.contact = Mock(return_value={"name": "客户", "window_id": 11})
        self.adapter._nodes = Mock(return_value=[])
        self.adapter._layout = Mock(return_value={"input": {"element": object(), "frame": (0, 0, 300, 120)}})
        self.adapter._frameworks = Mock(return_value=(None, Mock(), None))
        self.adapter._ax_attr = Mock(return_value="人工草稿")
        self.adapter._activate = Mock()
        with self.assertRaisesRegex(AssistantError, "草稿"):
            self.adapter.write_reply("客户", "AI回复", send=True)
        self.adapter._activate.assert_not_called()

    def test_adapter_never_closes_a_user_opened_window(self):
        self.assertFalse(self.adapter.close_contact_window(11))

    def test_edited_owned_draft_can_be_confirmed_and_sent(self):
        self.adapter._window = Mock(return_value=self.window)
        self.adapter.contact = Mock(return_value={"name": "客户", "window_id": 11})
        self.adapter._nodes = Mock(return_value=[])
        self.adapter._layout = Mock(return_value={"input": {"element": object(), "frame": (0, 0, 300, 120)}})
        native_ax = Mock()
        native_ax.AXUIElementSetAttributeValue.return_value = 0
        self.adapter._frameworks = Mock(return_value=(None, native_ax, None))
        self.adapter._ax_attr = Mock(side_effect=["原草稿", "原草稿", "修改后", ""])
        self.adapter._owned_drafts[(22, 11, "客户")] = "原草稿"
        self.adapter._activate = Mock()
        self.adapter._click = Mock()
        self.adapter._key = Mock()
        with patch('mac_assistant.platform_macos.time.sleep'):
            result = self.adapter.write_reply("客户", "修改后", send=True)
        self.assertTrue(result["sent"])
        self.adapter._key.assert_called_once_with(self.window, 36)

    def test_unreadable_input_is_not_treated_as_an_empty_draft(self):
        self.adapter._window = Mock(return_value=self.window)
        self.adapter.contact = Mock(return_value={"name": "客户", "window_id": 11})
        self.adapter._nodes = Mock(return_value=[])
        self.adapter._layout = Mock(return_value={"input": {"element": object(), "frame": (0, 0, 300, 120)}})
        self.adapter._frameworks = Mock(return_value=(None, Mock(), None))
        self.adapter._ax_attr = Mock(return_value=None)
        self.adapter._activate = Mock()
        with self.assertRaisesRegex(AssistantError, "无法读取"):
            self.adapter.write_reply("客户", "回复", send=True)
        self.adapter._activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
