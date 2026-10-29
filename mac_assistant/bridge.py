"""Nonblocking WebChannel RPC adapter for the existing hosted assistant page."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import threading
import time
import uuid

from PySide6.QtCore import QObject, Signal, Slot

from .diagnostics import write_customer_event

LOG = logging.getLogger(__name__)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def parameters(raw):
    if not raw:
        return {}
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("参数必须是 JSON 对象")
    return value


class Backend(QObject):
    rpc_result = Signal(str)
    reply_event = Signal(str)
    customer_contact_changed = Signal(str)
    stream_chunk = Signal(str, bool, str)
    status_updated = Signal(object)

    def __init__(self, controller, config, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.config = config
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wechat-native")
        self._state_lock = threading.Lock()
        self._pending = 0
        self._closing = False
        self._poll_pending = False
        self._cycle_running = False
        controller.on_event = self.publish_event
        controller.on_contact_changed = self.customer_contact_changed.emit

    def publish_event(self, payload):
        event = dict(payload) if isinstance(payload, dict) else {"message": str(payload)}
        event.setdefault("type", event.get("event", event.get("state", "status")))
        event.setdefault("timestamp", time.time())
        self.reply_event.emit(encoded(event))

    @Slot(str, str, str, result=str)
    def call(self, request_id, method, argument):
        """Return only the queue acknowledgement; rpc_result carries the final value."""
        if not request_id or len(request_id) > 160 or len(argument) > 262144:
            return encoded({"code": 0, "msg": "本地请求格式无效", "data": {}})
        is_cycle = method in ("start_assist_reply_task", "start_auto_reply_task")
        with self._state_lock:
            if self._closing or self._pending >= 32:
                return encoded({"code": 0, "msg": "本地任务繁忙，请稍后重试", "data": {}})
            if is_cycle and self._cycle_running:
                return encoded({"code": 0, "msg": "已有回复任务正在执行", "data": {"reason": "reply_task_busy"}})
            self._pending += 1
            if is_cycle:
                self._cycle_running = True
        # A stop request must be able to cancel the in-flight generation. It is
        # deliberately not queued behind a potentially slow network request.
        if is_cycle:
            mode = "auto" if method == "start_auto_reply_task" else "assist"
            task_id = mode + "_" + uuid.uuid4().hex
            try:
                params = parameters(argument)
                params["mode"] = mode
            except Exception as exc:
                with self._state_lock:
                    self._pending -= 1
                    self._cycle_running = False
                return encoded(self._error(exc))
            self.rpc_result.emit(encoded({"request_id": request_id,
                "result": {"code": 1, "msg": "回复任务已启动", "data": {"task_id": task_id}}}))
            self._worker.submit(self._execute_cycle, task_id, params)
        elif method in ("stop", "stop_reply_task", "stop_auto_reply_task", "stop_assist_reply_task"):
            try:
                result = self.controller.stop()
            except Exception as exc:
                result = self._error(exc)
            self.rpc_result.emit(encoded({"request_id": request_id, "result": result}))
            with self._state_lock:
                self._pending -= 1
        else:
            self._worker.submit(self._execute, request_id, method, argument)
        return encoded({"code": 1, "data": {"pending": True, "request_id": request_id}})

    def _execute_cycle(self, task_id, params):
        mode = params["mode"]
        self.publish_event({"type": "task_started" if mode == "auto" else "assist_detecting",
                            "mode": mode, "task_id": task_id})
        try:
            result = self.controller.prepare_reply(params)
        except Exception as exc:
            result = self._error(exc)
        finally:
            with self._state_lock:
                self._pending -= 1
                self._cycle_running = False
        if not self._closing:
            self.publish_event({"type": mode + "_task_finished", "mode": mode,
                                "task_id": task_id, "result": result})

    @staticmethod
    def _error(exc):
        LOG.warning("Native operation stopped: %s", type(exc).__name__)
        return {"code": 0, "msg": str(exc)[:300], "data": {"reason": type(exc).__name__}}

    def _execute(self, request_id, method, argument):
        started = time.perf_counter()
        try:
            result = self._dispatch(method, argument)
            if not isinstance(result, dict):
                result = {"code": 0, "msg": "本地结果格式无效", "data": {}}
        except Exception as exc:
            result = self._error(exc)
        finally:
            with self._state_lock:
                self._pending -= 1
        write_customer_event({"stage": "bridge_operation", "source": "macos:" + method,
                              "request_id": request_id, "ok": result.get("code") == 1,
                              "duration_ms": (time.perf_counter() - started) * 1000},
                             path=str(self.config.log_dir / "customer_timing.log"))
        if not self._closing:
            self.rpc_result.emit(encoded({"request_id": request_id, "result": result}))

    def _dispatch(self, method, argument):
        if method == "get_wechat_friend":
            return self.controller.get_friend()
        if method in ("prepare_assist_reply", "get_wechat_window_message"):
            params = parameters(argument)
            params["mode"] = "assist"
            return self.controller.prepare_reply(params)
        if method in ("auto_reply", "check_wechat_unread_message"):
            params = parameters(argument)
            params["mode"] = "auto"
            return self.controller.prepare_reply(params)
        if method == "check_unread":
            return self.controller.check_unread()
        if method == "send_assist_reply":
            params = parameters(argument)
            if "text" not in params and "answer" in params:
                params["text"] = params["answer"]
            return self.controller.send_reply(params)
        if method in ("send_message", "send_wechat_message"):
            params = parameters(argument)
            if not params.get("session_id"):
                return {"code": 0, "msg": "请先获取回复，确认当前联系人后再发送", "data": {}}
            return self.controller.send_message(params)
        if method in ("read_messages", "get_chat_message"):
            return self.controller.read_messages(parameters(argument))
        if method == "remember_customer_name":
            saved = self.controller.cache.remember_name(argument)
            return {"code": 1 if saved else 0, "msg": "success" if saved else "名称保存失败"}
        if method == "log_customer_timing":
            event = parameters(argument)
            event["source"] = "frontend:" + str(event.get("source", "unknown"))[:100]
            saved = write_customer_event(event, path=str(self.config.log_dir / "customer_timing.log"))
            return {"code": 1 if saved else 0}
        if method in ("status", "get_status"):
            return self.controller.status()
        if method == "permissions":
            return {"code": 1, "data": self.controller.adapter.permission_status(prompt=True)}
        return {"code": 0, "msg": "Mac 适配版尚未实现此功能：" + method[:60],
                "data": {"reason": "unsupported_on_macos"}}

    def poll(self):
        """Skip polling while business work is running; never queue stale polls."""
        with self._state_lock:
            if self._closing or self._pending or self._poll_pending:
                return
            self._poll_pending = True
        self._worker.submit(self._poll)

    def _poll(self):
        try:
            self.controller.poll()
            state = self.controller.status()
            if not self._closing:
                self.status_updated.emit(state)
        except Exception as exc:
            if not self._closing:
                self.status_updated.emit(self._error(exc))
        finally:
            with self._state_lock:
                self._poll_pending = False

    def close(self):
        self._closing = True
        self.controller.stop()
        self._worker.shutdown(wait=False, cancel_futures=True)
