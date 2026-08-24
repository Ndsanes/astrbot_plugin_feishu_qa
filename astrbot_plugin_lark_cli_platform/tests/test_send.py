"""发送路径:Plain/Image/不支持组件/CLI 失败/bot 身份与 target 分派。"""

from __future__ import annotations

import pytest
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.core.platform.astr_message_event import MessageSesion

from astrbot_plugin_lark_cli_platform.platform_adapter import LarkCliPlatform
from astrbot_plugin_lark_cli_platform.platform_event import (
    LarkCliPlatformEvent,
    deliver_chain,
)

from .helpers import FakeMessenger


class Record:
    """测试用不支持组件桩。"""

    def __init__(self, file=""):
        self.file = file


class CliBoom(Exception):
    pass


def make_event(messenger):
    return LarkCliPlatformEvent(
        message_str="",
        message_obj=None,
        platform_meta=None,
        session_id="oc_chat",
        messenger=messenger,
        chat_id="oc_chat",
    )


async def test_send_plain_uses_chat_id():
    messenger = FakeMessenger()
    await make_event(messenger).send(MessageChain(chain=[Plain("hi"), Plain("there")]))
    assert messenger.calls == [("text", "oc_chat", "hi"), ("text", "oc_chat", "there")]


async def test_send_image(tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG")
    messenger = FakeMessenger()
    comp = Image.fromFileSystem(str(img))
    await make_event(messenger).send(MessageChain(chain=[comp]))
    assert messenger.calls == [("image", "oc_chat", str(img))]


async def test_unsupported_component_skipped_not_fatal():
    messenger = FakeMessenger()
    chain = MessageChain(chain=[Record(file="x.amr"), Plain("after")])
    await make_event(messenger).send(chain)  # 不抛异常
    assert ("text", "oc_chat", "after") in messenger.calls
    assert all(c[0] != "record" for c in messenger.calls)


async def test_cli_failure_in_event_send_propagates_to_caller_policy():
    """事件 send:组件失败向上抛(AstrBot 侧统一处理);不产生半条重复调用。"""
    messenger = FakeMessenger(fail_on={"text": CliBoom("cli down")})
    with pytest.raises(CliBoom):
        await make_event(messenger).send(MessageChain(chain=[Plain("boom")]))


async def test_deliver_chain_swallows_cli_failure(caplog):
    messenger = FakeMessenger(fail_on={"text": CliBoom("cli down")})
    await deliver_chain(messenger, "oc_chat", MessageChain(chain=[Plain("boom"), Plain("next")]))
    # 失败被记录且后续组件不再发送(首个失败即终止本轮投递)
    assert messenger.calls == [("text", "oc_chat", "boom")]


def _adapter_with_messenger(messenger):
    adapter = LarkCliPlatform(
        {"lark_cli_home": "", "bootstrap_cli": False}, {}, event_queue=None
    )
    adapter._messenger = messenger
    return adapter


async def test_send_by_session_group_target():
    messenger = FakeMessenger()
    adapter = _adapter_with_messenger(messenger)
    session = MessageSesion(session_id="oc_group1")
    await adapter.send_by_session(session, MessageChain(chain=[Plain("hello")]))
    # oc_ 前缀 → --chat-id 分派(FakeMessenger 记录的 target 即分派结果)
    assert messenger.calls == [("text", "oc_group1", "hello")]


async def test_send_by_session_p2p_target():
    messenger = FakeMessenger()
    adapter = _adapter_with_messenger(messenger)
    session = MessageSesion(session_id="ou_user9")
    await adapter.send_by_session(session, MessageChain(chain=[Plain("dm")]))
    assert messenger.calls == [("text", "ou_user9", "dm")]


async def test_send_streaming聚合全文一次性下发():
    async def stream():
        yield MessageChain(chain=[Plain("你"), Plain("好")])
        yield MessageChain(chain=[Plain("！")])

    messenger = FakeMessenger()
    await make_event(messenger).send_streaming(stream())
    assert messenger.calls == [("text", "oc_chat", "你好！")]


async def test_send_streaming空聚合跳过():
    async def empty_stream():
        yield MessageChain(chain=[])

    messenger = FakeMessenger()
    await make_event(messenger).send_streaming(empty_stream())
    assert messenger.calls == []
