"""convert_message 映射:group/private/sender/消息链/raw。"""

from __future__ import annotations

from astrbot.api.platform import MessageType

from astrbot_plugin_lark_cli_platform.platform_adapter import LarkCliPlatform

from .helpers import make_msg


def make_adapter() -> LarkCliPlatform:
    return LarkCliPlatform(
        {"lark_cli_home": "", "bootstrap_cli": False},
        {},
        event_queue=None,
    )


async def test_group_mapping():
    adapter = make_adapter()
    abm = await adapter.convert_message(make_msg())
    assert abm.type == MessageType.GROUP_MESSAGE
    assert abm.group_id == "oc_chat"
    assert abm.session_id == "oc_chat"
    assert abm.message_id == "om_1"
    assert abm.self_id == "lark_cli_bot"
    assert abm.message_str == "你好"
    assert abm.sender.user_id == "ou_user"
    assert abm.sender.nickname == "张三"
    assert len(abm.message) == 1 and abm.message[0].text == "你好"
    assert abm.raw_message == {"schema": "2.0"}


async def test_private_mapping():
    adapter = make_adapter()
    abm = await adapter.convert_message(
        make_msg(chat_id="ou_peer", chat_type="p2p", sender_name="")
    )
    assert abm.type == MessageType.FRIEND_MESSAGE
    assert not getattr(abm, "group_id", None)
    assert abm.session_id == "ou_peer"
    # nickname 缺失时以 user_id 兜底,事件不丢弃
    assert abm.sender.nickname == "ou_user"


def test_meta():
    adapter = make_adapter()
    meta = adapter.meta()
    assert meta.name == "lark_cli"
    # AstrBot 4.27+ 需要唯一实例 id
    assert meta.id == "lark_cli"


async def test_group_message_strips_leading_mention():
    from astrbot_lark_kit import NormalizedLarkMessage

    adapter = make_adapter()
    msg = NormalizedLarkMessage(
        message_id="om_1",
        sender_id="ou_u",
        sender_name="小明",
        sender_type="user",
        chat_id="oc_chat1",
        chat_type="group",
        message_type="text",
        text="@插件Bot /sid",
        timestamp="1700000000000",
    )
    abm = await adapter.convert_message(msg)
    assert abm.message_str == "/sid"
    assert abm.message[0].text == "/sid"
    # 私聊不剥离
    msg_p2p = NormalizedLarkMessage(
        message_id="om_2", sender_id="ou_u", sender_name="小明", sender_type="user",
        chat_id="oc_chat2", chat_type="p2p", message_type="text",
        text="@插件Bot /sid", timestamp="1700000000000",
    )
    abm2 = await adapter.convert_message(msg_p2p)
    assert abm2.message_str == "@插件Bot /sid"
