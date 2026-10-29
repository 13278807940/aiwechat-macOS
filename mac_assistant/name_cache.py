"""Only names are persisted. This file never authorizes a send or CRM identity."""
import json
from pathlib import Path
import os
import tempfile
import threading
import time

from .models import clean_name, valid_name
from .diagnostics import write_customer_event


class NameCache:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._names = {}
        self._dirty = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if data.get("version") == 2:
                for value in data.get("names", []):
                    if isinstance(value, str) and valid_name(value):
                        self._names[clean_name(value)] = None
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    def names(self):
        with self._lock:
            return list(self._names)

    def remember_name(self, value):
        started = time.perf_counter()
        name = clean_name(value)
        if not valid_name(name):
            return False
        with self._lock:
            known = name in self._names
            if known and not self._dirty:
                return True
            self._names[name] = None
            self._dirty = True
            temporary = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".names-", suffix=".tmp", dir=str(self.path.parent))
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"version": 2, "names": list(self._names)}, stream,
                              ensure_ascii=False, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                self._dirty = False
                persisted = True
            except OSError:
                persisted = False
            finally:
                if temporary and os.path.exists(temporary):
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
            write_customer_event({"stage": "native_name_persist", "source": "mac:name_cache",
                                  "name": name, "known_before": known, "persisted": persisted,
                                  "ok": persisted, "path": str(self.path),
                                  "duration_ms": (time.perf_counter() - started) * 1000})
            return persisted
