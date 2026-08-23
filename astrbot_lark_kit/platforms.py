"""平台标识解析 —— 纯解析层。

把 AstrBot 统一消息标识(UMO, unified message origin)解析为结构化的平台身份。
典型 UMO 形如::

    default_1905473952:GroupMessage:6CCC18AB28098F241B44FF1A41F6668F
    └── 平台实例ID ──┘ └─ 消息类型 ─┘ └──────── 会话 ID ────────┘

设计边界(刻意收窄):
- 只做确定性解析、归一化与校验;
- 不 import AstrBot,不接受 Event 对象,不读配置与凭据,不发网络请求;
- 不决定使用哪个 app/token,不包含任何业务领域逻辑。

关于"未知平台":本模块刻意不维护平台注册表——任何非空的实例 ID 都会被接受,
平台名是否合法由调用方(掌握运行时上下文的一方)判断。这是明确行为而非遗漏。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import UmoParseError

__all__ = ["PlatformIdentity", "resolve_platform_instance"]

@dataclass(frozen=True, slots=True)
class PlatformIdentity:
    """从 UMO 解析出的平台身份。

    Attributes:
        umo: 归一化后的完整 UMO(各段去除首尾空白后以 ":" 重连)。
        instance_id: UMO 首段,平台实例 ID(与 event.get_platform_id() 同源),
            多 bot 场景下用于精确定位归属客户端。
        message_type: 第二段,消息类型(如 GroupMessage / FriendMessage)。
        session_id: 第三段及以后重连的会话 ID(openid 等;会话 ID 本身可含 ":",
            因此只要求前两段严格为单段,剩余部分全部归入 session_id)。
    """

    umo: str
    instance_id: str
    message_type: str
    session_id: str


def resolve_platform_instance(umo: str) -> PlatformIdentity:
    """把 UMO 字符串确定性解析为 :class:`PlatformIdentity`。

    规则:
    - 输入先整体去首尾空白;各段再去一次首尾空白;
    - 至少需要 ``instance:message:session`` 三段信息;会话 ID 内部的 ":" 不拆分;
    - instance 或 message 为空、段数不足 2、输入为空 → :class:`UmoParseError`;
    - 未知平台名不拒绝(无注册表,见模块 docstring)。

    Raises:
        UmoParseError: 空值、段数不足或必需段为空。
    """
    if not isinstance(umo, str):
        raise UmoParseError(f"UMO 必须是字符串,得到 {type(umo).__name__}")
    text = umo.strip()
    if not text:
        raise UmoParseError("UMO 为空")

    parts = [p.strip() for p in text.split(":")]
    if len(parts) < 3 or not parts[-1]:
        raise UmoParseError(f"UMO 至少需要 实例ID:消息类型:会话ID 三段: {text!r}")
    instance_id, message_type = parts[0], parts[1]
    session_id = ":".join(parts[2:])
    if not instance_id:
        raise UmoParseError(f"UMO 首段(平台实例 ID)为空: {text!r}")
    if not message_type:
        raise UmoParseError(f"UMO 第二段(消息类型)为空: {text!r}")

    return PlatformIdentity(
        umo=":".join((instance_id, message_type, session_id)),
        instance_id=instance_id,
        message_type=message_type,
        session_id=session_id,
    )
