"""Configuration stays outside the read-only signed app bundle after install."""
from dataclasses import dataclass
import os
from pathlib import Path
import sys

DEFAULT_URL = "https://yufabu.saizhuge.net/wxassistant/mobile/login"
LEGACY_URLS = {"https://yufabu.saizhuge.net/wechat_helper/main.html",
               "http://yufabu.saizhuge.net/wechat_helper/main.html"}


def resource_dir():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def read_env(path):
    values = {}
    if not Path(path).is_file():
        return values
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (item.strip() for item in line.split("=", 1))
        if not key.isidentifier():
            continue
        if value.startswith(("'", '"')):
            quote = value[0]
            end = value.rfind(quote)
            if end > 0:
                value = value[1:end]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


@dataclass(frozen=True)
class Config:
    main_url: str
    data_dir: Path
    log_dir: Path
    width: int = 360
    min_width: int = 300
    max_width: int = 720
    poll_interval: float = 0.8
    ai_base: str = ""
    bot_id: str = ""
    user_id: str = "aiwechat-mac"
    app_version: str = "2.0.3"


def load_config(data_dir=None):
    directory = Path(data_dir or os.getenv("MAC_ASSISTANT_DATA_DIR") or
                     (Path.home() / "Library/Application Support/ZhongBenAssistant"))
    directory.mkdir(parents=True, exist_ok=True)
    logs = directory / "logs"
    logs.mkdir(exist_ok=True)
    values = read_env(resource_dir() / ".env")
    values.update(read_env(directory / ".env"))
    # Explicit environment values have highest priority, including CI injection.
    for key in ("main_url", "ZHINENGKEFU_API_BASE", "ZHINENGKEFU_BOT_ID", "user_id",
                "MAC_ASSISTANT_WIDTH", "MAC_ASSISTANT_POLL_INTERVAL"):
        if key in os.environ:
            values[key] = os.environ[key]
    url = values.get("main_url", DEFAULT_URL).strip() or DEFAULT_URL
    if url.rstrip("/") in LEGACY_URLS:
        url = DEFAULT_URL
    if not url.startswith("https://"):
        raise ValueError("main_url 必须是 HTTPS 地址")
    return Config(main_url=url, data_dir=directory, log_dir=logs,
                  width=max(300, min(720, int(values.get("MAC_ASSISTANT_WIDTH", "360")))),
                  poll_interval=max(0.5, float(values.get("MAC_ASSISTANT_POLL_INTERVAL", "0.8"))),
                  ai_base=values.get("ZHINENGKEFU_API_BASE", "").strip(),
                  bot_id=values.get("ZHINENGKEFU_BOT_ID", "").strip(),
                  user_id=values.get("user_id", "aiwechat-mac").strip())
