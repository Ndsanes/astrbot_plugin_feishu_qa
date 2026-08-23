"""platforms.py 纯解析层测试:合法 UMO / 异常输入 / 归一化行为。"""

from __future__ import annotations

import dataclasses

import pytest

from astrbot_lark_kit.errors import LarkKitError, UmoParseError
from astrbot_lark_kit.platforms import (
    PlatformIdentity,
    resolve_platform_instance,
)

QQ_UMO = "default_1905473952:GroupMessage:6CCC18AB28098F241B44FF1A41F6668F"


def test_合法完整_umo():
    ident = resolve_platform_instance(QQ_UMO)
    assert isinstance(ident, PlatformIdentity)
    assert ident.instance_id == "default_1905473952"
    assert ident.message_type == "GroupMessage"
    assert ident.session_id == "6CCC18AB28098F241B44FF1A41F6668F"
    assert ident.umo == QQ_UMO


def test_会话_id_内含冒号时整体保留():
    ident = resolve_platform_instance("webchat:FriendMessage:uid:extra")
    assert ident.instance_id == "webchat"
    assert ident.message_type == "FriendMessage"
    assert ident.session_id == "uid:extra"
    assert ident.umo == "webchat:FriendMessage:uid:extra"


def test_各段首尾空白被归一化():
    ident = resolve_platform_instance("  aiocqhttp : GroupMessage : 12345 \n")
    assert ident.instance_id == "aiocqhttp"
    assert ident.message_type == "GroupMessage"
    assert ident.session_id == "12345"
    assert ident.umo == "aiocqhttp:GroupMessage:12345"


def test_未知平台名不拒绝_无注册表是明确行为():
    # 平台名合法性由调用方判断;解析层只要求非空
    ident = resolve_platform_instance("totally_unknown_platform:Something:id-1")
    assert ident.instance_id == "totally_unknown_platform"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "\n\t",
        "no-colon-at-all",
        "only:two",
        ":GroupMessage:openid",  # 缺 instance
        "inst::openid",  # 缺消息类型
        "inst:GroupMessage:",  # 缺会话 ID
        "inst:GroupMessage:   ",  # 会话 ID 仅空白
    ],
)
def test_异常输入统一抛_umo_parse_error(bad: str):
    with pytest.raises(UmoParseError) as ei:
        resolve_platform_instance(bad)
    assert ei.value.code == "UMO_PARSE_FAILED"


def test_非字符串输入拒绝():
    with pytest.raises(LarkKitError):
        resolve_platform_instance(None)  # type: ignore[arg-type]
    with pytest.raises(LarkKitError):
        resolve_platform_instance(12345)  # type: ignore[arg-type]


def test_identity_不可变():
    ident = resolve_platform_instance(QQ_UMO)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ident.instance_id = "hacked"  # type: ignore[misc]
