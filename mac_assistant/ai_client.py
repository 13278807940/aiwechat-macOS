"""Local zhinengkefu bot API client used by the desktop WeChat assistant."""

from __future__ import annotations

import json
import threading
from typing import Callable

import requests


StreamCallback = Callable[[str, bool], None]


class ZhinengkefuClientError(RuntimeError):
    """Raised when the customer-service bot cannot return a usable reply."""


class ZhinengkefuClient:
    def __init__(self, base_url: str, bot_id: str, timeout_seconds: float = 180):
        base_url = str(base_url or "").strip().rstrip("/")
        bot_id = str(bot_id or "").strip()
        if not base_url:
            raise ValueError("未配置智能客服 API 地址")
        if not bot_id:
            raise ValueError("未配置智能客服机器人 ID")

        self.chat_url = f"{base_url}/api/v1/conv/{bot_id}/chat"
        self.timeout = (10, float(timeout_seconds))
        self.session = requests.Session()
        # Local desktop calls must not be redirected through a system proxy.
        self.session.trust_env = False
        self._conversation_ids: dict[str, str] = {}
        self._conversation_lock = threading.Lock()

    def chat(
        self,
        message: str,
        external_user_id: str,
        reuse_conversation: bool = True,
    ) -> str:
        final_text = ""

        def collect(text: str, is_done: bool) -> None:
            nonlocal final_text
            if is_done:
                final_text = text

        self.chat_stream(
            message,
            external_user_id,
            collect,
            reuse_conversation=reuse_conversation,
        )
        return final_text

    def chat_stream(
        self,
        message: str,
        external_user_id: str,
        callback: StreamCallback,
        reuse_conversation: bool = True,
    ) -> str:
        message = str(message or "").strip()
        external_user_id = str(external_user_id or "").strip() or "anonymous"
        if not message:
            raise ValueError("消息内容不能为空")

        conversation_id = None
        if reuse_conversation:
            with self._conversation_lock:
                conversation_id = self._conversation_ids.get(external_user_id)

        payload = {
            "message": message,
            "external_user_id": external_user_id,
            "conversation_id": conversation_id,
            "stream": True,
        }

        try:
            response = self.session.post(
                self.chat_url,
                json=payload,
                headers={"Accept": "text/event-stream"},
                timeout=self.timeout,
                stream=True,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ZhinengkefuClientError(f"智能客服服务连接失败：{exc}") from exc

        streamed_text = ""
        final_reply = ""
        pending_error = ""
        done_received = False

        try:
            for raw_line in response.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue

                data_text = line[5:].strip()
                if not data_text or data_text == "[DONE]":
                    continue
                if data_text.startswith("[ERROR]"):
                    pending_error = data_text[len("[ERROR]"):].strip()
                    continue 

                try:
                    event = json.loads(data_text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                event_conversation_id = event.get("conversation_id")
                if event_conversation_id and reuse_conversation:
                    with self._conversation_lock:
                        self._conversation_ids[external_user_id] = str(event_conversation_id)

                if event.get("error"):
                    pending_error = str(event["error"])

                chunk = event.get("chunk")
                if isinstance(chunk, str) and chunk:
                    streamed_text += chunk
                    callback(chunk, False)

                if event.get("done"):
                    done_received = True
                    reply = event.get("reply")
                    # The completed reply is authoritative. Some servers emit
                    # retrieval/debug text as chunks before returning the real
                    # customer-facing answer in the done event.
                    if isinstance(reply, str) and reply.strip():
                        final_reply = reply.strip()
                    # Keep consuming until EOF. The server persists the
                    # conversation after yielding the done event; closing the
                    # stream here would cancel that commit.
                    continue
        finally:
            response.close()

        full_text = final_reply or streamed_text

        # Never treat a partial stream as a successful reply after the server
        # explicitly reported an error. A completed reply may still be a valid
        # server-side fallback, so it takes precedence over pending_error.
        if pending_error and not final_reply:
            raise ZhinengkefuClientError(f"智能客服返回错误：{pending_error}")

        if not full_text:
            detail = pending_error or ("响应流未正常结束" if not done_received else "未返回回复内容")
            raise ZhinengkefuClientError(f"智能客服未返回有效回复：{detail}")

        callback(full_text, True)
        return full_text

