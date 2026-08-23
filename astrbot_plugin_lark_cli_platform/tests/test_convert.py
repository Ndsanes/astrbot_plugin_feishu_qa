"""convert_message 映射:group/private/sender/消息链/raw。"""

from __future__ import annotations

from astrbot.api.platform import MessageType

from astrbot_plugin_lark_cli_platform.platform_adapter import LarkCliPlatform

from .helpers import make_msg


def make_adapter(enabled_chats=None) -> LarkCliPlatform:
    return LarkCliPlatform(
        {"lark_cli_home": "", "bootstrap_cli": False, "enabled_chats": enabled_chats or []},
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
    meta = make_adapter().meta()
    assert meta.name == "lark_cli"
