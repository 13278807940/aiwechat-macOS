from dataclasses import dataclass


class AssistantError(RuntimeError):
    """An actionable failure; callers must stop the current operation."""


@dataclass(frozen=True)
class WindowInfo:
    window_id: int
    pid: int
    title: str
    x: float
    y: float
    width: float
    height: float


def clean_name(value):
    name = str(value or "").strip()
    while name and (name[-1] in "?？\ufffd" or name[-1].isspace()):
        name = name[:-1]
    return name.strip()


def valid_name(value):
    name = clean_name(value)
    return bool(name and len(name) <= 160 and name.lower() not in {
        "微信", "wechat", "weixin", "设置", "搜索", "搜索联系人", "聊天", "通讯录",
        "朋友圈", "登录", "登录微信", "微信登录", "消息", "服务号", "公众号",
    })
