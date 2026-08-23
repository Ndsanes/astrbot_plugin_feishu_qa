"""测试共用:NormalizedLarkMessage 构造与假 messenger/事件流。"""

from __future__ import annotations

from astrbot_lark_kit import NormalizedLarkMessage


def make_msg(**overrides) -> NormalizedLarkMessage:
    base = dict(
        message_id="om_1",
        sender_id="ou_user",
        sender_name="张三",
        sender_type="user",
        chat_id="oc_chat",
        chat_type="group",
        message_type="text",
        text="你好",
        timestamp="1700000000000",
        raw={"schema": "2.0"},
    )
    base.update(overrides)
    return NormalizedLarkMessage(**base)


class FakeMessenger:
    """记录 bot 身份发送调用;可注入失败。"""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or {}

    async def send_text(self, target, text):
        self.calls.append(("text", target, text))
        exc = self.fail_on.get("text")
        if exc is not None:
            raise exc

    async def send_image(self, target, path):
        self.calls.append(("image", target, str(path)))
        exc = self.fail_on.get("image")
        if exc is not None:
            raise exc


class FakeStream:
    """替代 kit EventStream:按脚本逐条产出消息。"""

    def __init__(self, messages):
        self._messages = list(messages)

    async def stream(self):
        for m in self._messages:
            yield m


class RaisingStream:
    """stream() 立即抛错的假流(模拟 CLI 无法启动)。"""

    def __init__(self, exc):
        self.exc = exc

    async def stream(self):
        raise self.exc
        yield  # pragma: no cover — 使其成为 async generator
