import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from mac_assistant.config import DEFAULT_URL, load_config, read_env
from mac_assistant.controller import AssistantController
from mac_assistant.models import AssistantError
from mac_assistant.name_cache import NameCache


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.logger = patch("mac_assistant.diagnostics.write_customer_event", return_value=True)
        self.logger.start()
        self.addCleanup(self.logger.stop)
        self.cache_logger = patch("mac_assistant.name_cache.write_customer_event", return_value=True)
        self.cache_logger.start()
        self.addCleanup(self.cache_logger.stop)
        self.contact = {"name": "兄弟(5)", "type": "friend", "pid": 22, "window_id": 77}
        self.adapter = Mock()
        self.adapter.contact.side_effect = lambda: dict(self.contact)
        self.adapter.unread_candidate.return_value = None
        self.adapter.read_conversation.side_effect = lambda: {
            "contact": dict(self.contact), "message": "1", "fingerprint": "message-1", "window_id": 77}
        self.ai = Mock()
        self.ai.chat.return_value = "您好，已收到。"
        self.cache = NameCache(Path(self.temporary.name) / "names.json")
        self.controller = AssistantController(self.adapter, self.cache, self.ai)

    def test_default_assist_reads_consumed_badge_and_numeric_message_but_never_sends(self):
        response = self.controller.prepare_reply()
        self.assertEqual(response["code"], 1)
        self.assertEqual(response["data"]["question"], "1")
        self.adapter.unread_candidate.assert_called_once()
        self.adapter.write_reply.assert_called_once_with("兄弟(5)", "您好，已收到。", send=False)
        self.assertEqual(self.cache.names(), ["兄弟(5)"])
        document = json.loads(self.cache.path.read_text(encoding="utf-8"))
        self.assertEqual(set(document), {"version", "names"})

    def test_manual_send_needs_session_and_fresh_contact_and_question(self):
        response = self.controller.prepare_reply()
        session = response["data"]["session_id"]
        self.assertEqual(self.controller.send_message("裸文本")["code"], 0)
        sent = self.controller.send_reply({"session_id": session, "answer": "人工修改"})
        self.assertEqual(sent["code"], 1)
        self.adapter.write_reply.assert_called_with("兄弟(5)", "人工修改", send=True)
        self.assertEqual(self.controller.send_reply({"session_id": session})["code"], 0)

    def test_auto_is_only_enabled_by_literal_mode(self):
        response = self.controller.prepare_reply({"mode": "auto"})
        self.assertEqual(response["data"]["status"], "sent")
        self.adapter.write_reply.assert_called_with("兄弟(5)", "您好，已收到。", send=True)
        self.assertEqual(self.ai.chat.call_args.args[0], "1")

    def test_account_or_window_switch_during_ai_blocks_draft_and_send(self):
        def switch(*args, **kwargs):
            self.contact["pid"] = 99
            return "旧联系人的回复"
        self.ai.chat.side_effect = switch
        self.assertEqual(self.controller.prepare_reply({"mode": "auto"})["code"], 0)
        self.adapter.write_reply.assert_not_called()

    def test_stop_during_ai_blocks_send_without_waiting_for_request(self):
        def cancel(*args, **kwargs):
            self.controller.stop()
            return "已取消的回复"
        self.ai.chat.side_effect = cancel
        self.assertEqual(self.controller.prepare_reply({"mode": "auto"})["code"], 0)
        self.adapter.write_reply.assert_not_called()

    def test_pending_assist_is_reused_and_auto_does_not_upgrade_it(self):
        prepared = self.controller.prepare_reply()
        again = self.controller.prepare_reply({"mode": "auto"})
        self.assertEqual(again["data"]["session_id"], prepared["data"]["session_id"])
        self.ai.chat.assert_called_once()
        self.assertEqual(self.adapter.write_reply.call_count, 1)

    def test_new_message_before_manual_send_blocks_outdated_reply(self):
        prepared = self.controller.prepare_reply()
        self.adapter.read_conversation.side_effect = lambda: {
            "contact": dict(self.contact), "message": "2", "fingerprint": "message-2"}
        failed = self.controller.send_reply({"session_id": prepared["data"]["session_id"]})
        self.assertEqual(failed["code"], 0)
        self.assertFalse(any(call.kwargs["send"] for call in self.adapter.write_reply.call_args_list))

    def test_consumed_candidate_is_verified_before_reply(self):
        self.adapter.unread_candidate.return_value = {"name": "其他客户"}
        self.assertEqual(self.controller.prepare_reply()["code"], 0)
        self.ai.chat.assert_not_called()

    def test_contact_poll_updates_name_and_emits_without_reply_or_reload(self):
        changed = Mock()
        self.controller.on_contact_changed = changed
        self.controller.poll()
        self.contact["name"] = "下一个客户?"
        self.controller.poll()
        self.assertEqual(changed.call_args.args[0], "下一个客户")
        self.ai.chat.assert_not_called()
        self.adapter.write_reply.assert_not_called()

    def test_name_cache_retries_failed_write_without_claiming_persistence(self):
        with patch("mac_assistant.name_cache.os.replace", side_effect=PermissionError("test")):
            self.assertFalse(self.cache.remember_name("客户?"))
        self.assertTrue(self.cache.remember_name("客户"))
        self.assertEqual(NameCache(self.cache.path).names(), ["客户"])

    def test_same_message_is_not_auto_replied_twice(self):
        self.controller.prepare_reply({"mode": "auto"})
        response = self.controller.prepare_reply({"mode": "auto"})
        self.assertEqual(response["data"]["reason"], "duplicate")
        self.ai.chat.assert_called_once()

    def test_status_includes_window_geometry_without_platform_objects(self):
        from mac_assistant.models import WindowInfo
        self.adapter.window_geometry.return_value = WindowInfo(77, 22, "微信", -900, 35, 850, 700)
        response = self.controller.status()
        self.assertEqual(response["data"]["window"], {"x": -900, "y": 35, "width": 850, "height": 700})

    def test_question_changed_during_generation_never_prefills_stale_answer(self):
        def new_question(*args, **kwargs):
            self.adapter.read_conversation.side_effect = lambda: {
                "contact": dict(self.contact), "message": "2", "fingerprint": "message-2"}
            return "旧答案"
        self.ai.chat.side_effect = new_question
        self.assertEqual(self.controller.prepare_reply()["code"], 0)
        self.adapter.write_reply.assert_not_called()


class ConfigurationTests(unittest.TestCase):
    def test_bom_and_url_values_are_read_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("\ufeffmain_url = 'https://example.com/page#login'\nAPI_KEY='$(no-execution)'\n", encoding="utf-8")
            values = read_env(path)
            self.assertEqual(values["main_url"], "https://example.com/page#login")
            self.assertEqual(values["API_KEY"], "$(no-execution)")

    def test_installed_configuration_overrides_bundle_and_migrates_legacy_url(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / ".env").write_text("main_url='https://yufabu.saizhuge.net/wechat_helper/main.html'\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True), patch("mac_assistant.config.resource_dir", return_value=data / "bundle"):
                config = load_config(data)
            self.assertEqual(config.main_url, DEFAULT_URL)
            self.assertEqual(config.width, 360)


if __name__ == "__main__":
    unittest.main()
