"""Badge detection has the same point-size contract for both capture backends."""
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from mac_assistant.models import WindowInfo
from mac_assistant.platform_macos import MacWeChat, _red_components


class RetinaBadgeTests(unittest.TestCase):
    def setUp(self):
        self.window = WindowInfo(11, 22, "微信", -800, 70, 900, 700)
        self.layout = {"nav_width": 68.0, "chat_left": 270.0}
        self.adapter = MacWeChat()

    @staticmethod
    def draw_badge(image, box, sx, sy, shape="ellipse"):
        left, top, right, bottom = box
        pixels = (int(left * sx), int(top * sy), int(right * sx) - 1, int(bottom * sy) - 1)
        draw = ImageDraw.Draw(image)
        getattr(draw, shape)(pixels, fill=(245, 40, 45))

    def test_same_row_badge_gives_same_screen_point_at_1x_and_2x(self):
        positions = []
        for scale in (1, 2):
            image = Image.new("RGB", (900 * scale, 700 * scale), "white")
            self.draw_badge(image, (109, 90, 125, 106), scale, scale)
            positions.append(self.adapter._row_badge(image, self.window, self.layout))
        self.assertEqual(positions, [{"x": 117.0, "y": 98.0}] * 2)

    def test_component_boxes_remain_in_original_image_pixels(self):
        for sx, sy in ((1, 1), (2, 2), (2, 1)):
            with self.subTest(scale=(sx, sy)):
                image = Image.new("RGB", (100 * sx, 100 * sy), "white")
                self.draw_badge(image, (20, 25, 36, 41), sx, sy)
                self.assertEqual(_red_components(image, (0, 0, image.width, image.height), sx, sy),
                                 [(20 * sx, 25 * sy, 36 * sx, 41 * sy)])

    def test_large_red_avatar_is_excluded_at_both_pixel_densities(self):
        for scale in (1, 2):
            with self.subTest(scale=scale):
                image = Image.new("RGB", (900 * scale, 700 * scale), "white")
                self.draw_badge(image, (102, 130, 148, 176), scale, scale, "rectangle")
                self.assertIsNone(self.adapter._row_badge(image, self.window, self.layout))
                self.draw_badge(image, (109, 210, 125, 226), scale, scale)
                self.assertEqual(self.adapter._row_badge(image, self.window, self.layout),
                                 {"x": 117.0, "y": 218.0})

    def test_navigation_badge_triggers_double_click_at_both_pixel_densities(self):
        for scale in (1, 2):
            with self.subTest(scale=scale):
                adapter = MacWeChat()
                image = Image.new("RGB", (900 * scale, 700 * scale), "white")
                self.draw_badge(image, (45, 90, 61, 106), scale, scale)
                adapter._window = Mock(return_value=self.window)
                adapter._nodes = Mock(return_value=[])
                adapter._layout = Mock(return_value=self.layout)
                adapter._capture = Mock(return_value=(object(), image))
                adapter._ocr = Mock(return_value=[])
                adapter._click = Mock()
                adapter.contact = Mock(return_value={"name": "客户", "window_id": 11, "pid": 22})
                with patch("mac_assistant.platform_macos.time.sleep"):
                    result = adapter.unread_candidate()
                adapter._click.assert_called_once_with(self.window, 34.0, 100.0, double=True)
                self.assertEqual(result["name"], "客户")
                self.assertEqual(result["unread_epoch"], 1)

    def test_nonpositive_or_nonfinite_density_is_rejected(self):
        image = Image.new("RGB", (20, 20), "white")
        for sx, sy in ((0, 1), (-1, 1), (1, float("nan")), (float("inf"), 1)):
            with self.subTest(scale=(sx, sy)):
                with self.assertRaises(ValueError):
                    _red_components(image, (0, 0, 20, 20), sx, sy)


if __name__ == "__main__":
    unittest.main()
