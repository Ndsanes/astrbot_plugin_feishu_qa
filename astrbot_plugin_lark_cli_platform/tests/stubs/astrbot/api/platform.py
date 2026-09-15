"""最小桩:Platform API,仅供离线单测导入插件模块。"""

from __future__ import annotations

from enum import Enum

ADAPTER_REGISTRY: dict[str, type] = {}


class MessageType(Enum):
    GROUP_MESSAGE = "GroupMessage"
    FRIEND_MESSAGE = "FriendMessage"


class MessageMember:
    def __init__(self, user_id="", nickname=""):
        self.user_id = user_id
        self.nickname = nickname


class AstrBotMessage:
    def __init__(self):
        self.type = None
        self.group_id = ""
        self.message_str = ""
        self.sender = None
        self.message = []
        self.raw_message = None
        self.self_id = ""
        self.session_id = ""
        self.message_id = ""
        self.timestamp = None


class PlatformMetadata:
    def __init__(self, name="", description="", id="", **kwargs):
        self.name = name
        self.description = description
        self.id = id

class Platform:
    def __init__(self, config, event_queue):
        self.config = config
        self.event_queue = event_queue

    def meta(self) -> PlatformMetadata:
        raise NotImplementedError

    async def run(self):
        raise NotImplementedError

    async def send_by_session(self, session, message_chain):
        raise NotImplementedError

    def commit_event(self, event):
        if self.event_queue is not None:
            self.event_queue.put_nowait(event)


def register_platform_adapter(
    name: str,
    description: str,
    default_config_tmpl=None,
    adapter_display_name: str | None = None,
    logo_path: str | None = None,
    support_streaming_message: bool = True,
    i18n_resources: dict | None = None,
    config_metadata: dict | None = None,
):
    def deco(cls):
        ADAPTER_REGISTRY[name] = cls
        cls._adapter_meta = (name, description, default_config_tmpl)
        return cls

    return deco
