"""Small, best-effort customer timing log, available in source and frozen runs."""
import json
import math
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager

_LOCK = threading.Lock()
_MAX_BYTES = 5 * 1024 * 1024
_LOG_DIRECTORY = None
_TEXT_FIELDS = {
    "request_id", "recognition_id", "source", "stage", "name", "hash",
    "alias", "display_method", "status", "path", "reason", "error_type",
    "event",
}
_BOOL_FIELDS = {"ok", "stale", "known_before", "persisted", "fallback", "migrated"}
_NUMBER_FIELDS = {"generation", "duration_ms", "elapsed_ms", "names_count"}


def timing_log_path():
    directory = _LOG_DIRECTORY or os.path.join(os.path.expanduser("~"),
        "Library", "Application Support", "ZhongBenAssistant", "logs")
    return os.path.join(directory, "customer_timing.log")


def configure_diagnostics(directory):
    global _LOG_DIRECTORY
    _LOG_DIRECTORY = os.fspath(directory)


def clean_event(payload):
    """Whitelist metadata; never write bridge arguments, tokens or API responses."""
    if not isinstance(payload, dict):
        raise ValueError("timing event must be an object")
    result = {}
    for key, value in payload.items():
        if key in _TEXT_FIELDS and isinstance(value, str):
            result[key] = value[:512 if key == "path" else 160]
        elif key in _BOOL_FIELDS and isinstance(value, bool):
            result[key] = value
        elif (key in _NUMBER_FIELDS and isinstance(value, (int, float))
              and not isinstance(value, bool) and math.isfinite(value) and value >= 0):
            result[key] = round(value, 3)
    if not result.get("stage"):
        raise ValueError("timing event requires stage")
    return result


def write_customer_event(payload, path=None):
    try:
        event = clean_event(payload)
        event["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        destination = path or timing_log_path()
        with _LOCK:
            os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
            if os.path.isfile(destination) and os.path.getsize(destination) >= _MAX_BYTES:
                os.replace(destination, destination + ".1")
            with open(destination, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        try:
            print("[CustomerTiming] " + line, flush=True)
        except (OSError, ValueError):
            pass
        return True
    except Exception:
        # Diagnostics must never prevent recognition, switching or replying.
        return False


class CustomerTrace:
    def __init__(self, source):
        self.started = time.perf_counter()
        self.recognition_id = uuid.uuid4().hex[:16]
        self.source = source

    def event(self, stage, duration_ms=0, ok=True, **details):
        write_customer_event(dict(details, stage=stage, duration_ms=duration_ms,
                                  ok=ok, source=self.source,
                                  recognition_id=self.recognition_id))

    @contextmanager
    def span(self, stage):
        started = time.perf_counter()
        try:
            yield
        except Exception as exc:
            self.event(stage, (time.perf_counter() - started) * 1000,
                       ok=False, error_type=type(exc).__name__)
            raise
        else:
            self.event(stage, (time.perf_counter() - started) * 1000)

    def finish(self, ok, **details):
        duration = round((time.perf_counter() - self.started) * 1000, 3)
        self.event("recognition_total", duration, ok=ok, **details)
        return {"recognition_id": self.recognition_id, "duration_ms": duration,
                "source": self.source}
