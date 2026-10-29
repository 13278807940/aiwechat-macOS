"""Platform-independent reply pipeline; native operations are serialized by the bridge."""
from collections import OrderedDict
import hashlib
import threading
import time
import uuid

from .models import AssistantError, clean_name, valid_name
from .diagnostics import CustomerTrace, configure_diagnostics
from .name_cache import NameCache


def result(data=None, msg="success", code=1):
    return {"code": code, "msg": msg, "data": data or {}}


def identity(contact):
    return (contact.get("pid"), contact.get("window_id"), clean_name(contact.get("name")),
            str(contact.get("conversation_id") or ""))


class AssistantController:
    def __init__(self, adapter, cache, ai_client=None, user_id="aiwechat-mac"):
        self.adapter = adapter
        self.cache = cache
        self.ai_client = ai_client
        self.user_id = user_id
        self.on_event = lambda event: None
        self.on_contact_changed = lambda name: None
        self._operation_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._generation = 0
        self._sessions = {}
        self._handled = OrderedDict()
        self._last_contact = None
        self._running = False
        self._mode = "assist"

    def _emit(self, event_type, **fields):
        try:
            self.on_event(dict(fields, type=event_type, event=event_type, timestamp=time.time()))
        except Exception:
            pass

    def _ensure_active(self, generation):
        with self._state_lock:
            if generation != self._generation:
                raise AssistantError("任务已停止，本轮内容不会发送")

    def _remember_contact(self, contact):
        contact = dict(contact)
        contact["name"] = clean_name(contact.get("name"))
        if not valid_name(contact["name"]):
            raise AssistantError("未识别到当前聊天联系人，请先在微信里选中联系人")
        changed = self._last_contact is None or identity(contact) != identity(self._last_contact)
        self._last_contact = contact
        self.cache.remember_name(contact["name"])
        if changed:
            try:
                self.on_contact_changed(contact["name"])
            except Exception:
                pass
        return contact

    def get_friend(self):
        with self._operation_lock:
            trace = CustomerTrace("mac:contact")
            try:
                with trace.span("contact_recognition"):
                    contact = self._remember_contact(self.adapter.contact())
                response = result(contact)
                response["timing"] = trace.finish(True, name=contact["name"])
                return response
            except Exception as exc:
                timing = trace.finish(False, error_type=type(exc).__name__)
                response = result(msg=str(exc), code=0)
                response["timing"] = timing
                return response

    def poll(self):
        # Do not queue behind a long AI request. The next idle tick will sample.
        if not self._operation_lock.acquire(blocking=False):
            return result({"status": "busy"})
        try:
            return self.get_friend()
        finally:
            self._operation_lock.release()

    def status(self):
        with self._state_lock:
            running, mode = self._running, self._mode
        geometry = None
        try:
            window = self.adapter.window_geometry()
            if window is not None:
                geometry = {key: getattr(window, key) for key in ("x", "y", "width", "height")}
        except Exception:
            pass
        return result({"running": running, "mode": mode,
                       "window": geometry,
                       "last_contact": dict(self._last_contact or {}),
                       "permissions": self.adapter.permission_status(prompt=False)})

    def stop(self):
        with self._state_lock:
            self._generation += 1
            self._running = False
        self._emit("stopped", mode=self._mode, message="已停止，等待中的生成结果不会发送")
        return result({"status": "stopped"})

    def _guard_contact(self, expected):
        current = self.adapter.contact()
        if identity(current) != identity(expected):
            raise AssistantError("当前微信联系人已切换，请重新生成回复")

    def prepare_reply(self, params=None):
        params = params or {}
        # No truthiness conversions: the literal mode is the only auto opt-in.
        mode = "auto" if params.get("mode") == "auto" else "assist"
        if not self._operation_lock.acquire(blocking=False):
            return result({"status": "busy"}, "已有任务正在进行", code=0)
        trace = CustomerTrace("mac:" + mode)
        session_id = None
        failed = False
        try:
            with self._state_lock:
                generation = self._generation
                self._mode, self._running = mode, True
            for old_id, old_session in list(self._sessions.items()):
                if old_session["generation"] != generation:
                    self._sessions.pop(old_id, None)
                    self._handled.pop(old_session["key"], None)
            self._emit("assist_detecting" if mode == "assist" else "detecting", mode=mode)
            # While an assisted draft is waiting, keep its session instead of
            # replacing it with another contact's reply on the next page poll.
            pending = next((session for session in self._sessions.values()
                            if session["state"] == "draft"), None)
            if pending:
                return result(self._public_session(pending, "awaiting_send"), "等待人工确认发送")
            with trace.span("unread_locate"):
                candidate = self.adapter.unread_candidate()
            # The active conversation may already have consumed the badge.
            # Read it too; native reader rejects outgoing/ambiguous last items.
            with trace.span("message_recognition"):
                conversation = self.adapter.read_conversation()
            if not conversation or not str(conversation.get("message") or "").strip():
                return result({"status": "idle"}, "暂无可回复的客户消息")
            contact = self._remember_contact(conversation["contact"])
            if contact.get("type") not in ("friend", None, ""):
                return result({"status": "skipped", "reason": "group_or_service"}, "已跳过群聊或服务号")
            # Keep the pre-click unread target. A consumed red badge must not
            # become the authority for selecting a different contact later.
            if candidate and candidate.get("name") and clean_name(candidate["name"]) != contact["name"]:
                raise AssistantError("未读消息定位与当前联系人不一致，已暂停本轮")
            question = str(conversation["message"]).strip()
            fingerprint = str(conversation.get("fingerprint") or "")
            if not fingerprint:
                raise AssistantError("消息没有可验证的身份，已暂停本轮")
            key = (identity(contact), fingerprint)
            if key in self._handled:
                return result({"status": "skipped", "reason": "duplicate"}, "该条消息已处理")
            if self.ai_client is None:
                raise AssistantError("请配置 ZHINENGKEFU_API_BASE 和 ZHINENGKEFU_BOT_ID 后再生成回复")
            self._ensure_active(generation)
            self._emit("assist_message_detected" if mode == "assist" else "message_detected",
                       mode=mode, contact_name=contact["name"], question=question)
            user_key = hashlib.sha256((self.user_id + repr(identity(contact))).encode("utf-8")).hexdigest()
            with trace.span("ai_request"):
                answer = self.ai_client.chat(question, external_user_id=user_key, reuse_conversation=True)
            self._ensure_active(generation)
            self._guard_contact(contact)
            if not str(answer or "").strip():
                raise AssistantError("智能客服没有返回有效回复")
            current = self.adapter.read_conversation()
            if (not current or identity(current.get("contact", {})) != identity(contact)
                    or str(current.get("fingerprint") or "") != fingerprint):
                raise AssistantError("生成期间客户消息发生变化，请重新获取回复")
            session_id = uuid.uuid4().hex
            session = {"session_id": session_id, "contact": contact, "question": question,
                       "answer": answer, "fingerprint": fingerprint, "key": key,
                       "state": "prepared", "generation": generation, "mode": mode}
            self._sessions[session_id] = session
            with trace.span("draft_write"):
                self.adapter.write_reply(contact["name"], answer, send=False)
            self._ensure_active(generation)
            session["state"] = "draft"
            self._handled[key] = None
            while len(self._handled) > 500:
                self._handled.popitem(last=False)
            if mode == "auto":
                return self._send_session(session, answer, generation)
            self._emit("assist_ready", mode=mode, **self._public_session(session, "awaiting_send"))
            return result(self._public_session(session, "awaiting_send"))
        except Exception as exc:
            failed = True
            if session_id:
                removed = self._sessions.pop(session_id, None)
                if removed:
                    self._handled.pop(removed["key"], None)
            self._emit("assist_error" if mode == "assist" else "error", mode=mode, message=str(exc))
            trace.event("reply_error", ok=False, error_type=type(exc).__name__)
            return result(msg=str(exc), code=0)
        finally:
            trace.finish(not failed)
            self._operation_lock.release()

    @staticmethod
    def _public_session(session, status):
        return {"session_id": session["session_id"], "contact_name": session["contact"]["name"],
                "question": session["question"], "answer": session["answer"],
                "status": status, "draft_in_wechat": True}

    def _send_session(self, session, answer, generation):
        self._ensure_active(generation)
        self._guard_contact(session["contact"])
        # Verify the last customer message again, so a reply generated before
        # a new incoming question cannot be sent as if it answered that question.
        current = self.adapter.read_conversation()
        if (not current or identity(current.get("contact", {})) != identity(session["contact"])
                or str(current.get("fingerprint") or "") != session["fingerprint"]):
            raise AssistantError("客户消息已变化，请重新生成后发送")
        self._ensure_active(generation)
        self.adapter.write_reply(session["contact"]["name"], answer, send=True)
        session["state"] = "sent"
        self._sessions.pop(session["session_id"], None)
        self._emit("assist_sent" if session["mode"] == "assist" else "sent",
                   mode=session["mode"], **self._public_session(session, "sent"))
        return result(self._public_session(session, "sent"))

    def send_reply(self, params=None):
        params = params or {}
        with self._operation_lock:
            session = self._sessions.get(str(params.get("session_id") or ""))
            if not session or session["state"] != "draft":
                return result(msg="没有待发送的辅助回复，请重新生成", code=0)
            answer = str(params.get("answer") or params.get("text") or session["answer"]).strip()
            if not answer:
                return result(msg="回复不能为空", code=0)
            try:
                return self._send_session(session, answer, session["generation"])
            except Exception as exc:
                self._sessions.pop(session["session_id"], None)
                self._handled.pop(session["key"], None)
                return result(msg=str(exc), code=0)

    def check_unread(self):
        return self.prepare_reply({"mode": "auto"})

    def read_messages(self, params=None):
        with self._operation_lock:
            return result(self.adapter.read_conversation())

    def send_message(self, params=None):
        # Plain text has no confirmed recipient/session and must not be sent.
        if not isinstance(params, dict) or not params.get("session_id"):
            return result(msg="发送必须携带辅助回复 session_id", code=0)
        return self.send_reply(params)


def build_controller(config):
    from .platform_macos import MacWeChat
    from .ai_client import ZhinengkefuClient
    configure_diagnostics(config.log_dir)
    client = ZhinengkefuClient(config.ai_base, config.bot_id) if config.ai_base and config.bot_id else None
    return AssistantController(MacWeChat(), NameCache(config.data_dir / "wechat_friend_cache.json"),
                               client, config.user_id)
