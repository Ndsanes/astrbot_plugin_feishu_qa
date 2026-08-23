"""lark_cli 平台适配器 — 把 lark-cli 的 bot 消息能力接入 AstrBot Platform API。

身份固定 bot(收发均为 ``--as bot``),不提供任何身份选择配置。
事件链:lark-cli NDJSON → kit EventStream(归一化/去重/自消息过滤)→
convert_message → LarkCliPlatformEvent → commit_event。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.star.star_tools import StarTools

try:  # 优先使用插件内 vendored 副本(与打包版本严格一致),缺失再退回安装版
    from .astrbot_lark_kit import (
        CliNotFoundError,
        EventStream,
        LarkMessenger,
        NormalizedLarkMessage,
        ensure_bundled_cli,
        find_bundled_cli,
        resolve_cli_bin,
        resolve_state_home,
    )
except ImportError:  # 开发环境:工作区源码或 pip 安装版
    from astrbot_lark_kit import (
        CliNotFoundError,
        EventStream,
        LarkMessenger,
        NormalizedLarkMessage,
        ensure_bundled_cli,
        find_bundled_cli,
        resolve_cli_bin,
        resolve_state_home,
    )

from .platform_event import LarkCliPlatformEvent, deliver_chain

PLUGIN_DIR = Path(__file__).resolve().parent
VENDOR_DIR = PLUGIN_DIR / "vendor" / "lark-cli"


@register_platform_adapter(
    "lark_cli",
    "lark-cli 平台适配器(bot 身份收发)",
    default_config_tmpl={"lark_cli_home": "", "bootstrap_cli": True, "enabled_chats": []},
)
class LarkCliPlatform(Platform):
    def __init__(self, platform_config: dict, platform_settings: dict, event_queue) -> None:
        super().__init__(event_queue)
        self.config = platform_config
        self.settings = platform_settings
        self._stream: EventStream | None = None
        self._messenger: LarkMessenger | None = None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata("lark_cli", "lark-cli 平台适配器")

    async def run(self):
        data_dir = StarTools.get_data_dir("astrbot_plugin_lark_cli_platform")
        home_cfg = str(self.config.get("lark_cli_home") or "").strip()
        state_home = Path(home_cfg) if home_cfg else resolve_state_home(data_dir)

        binary = await self._aresolve_binary()
        if binary is None:
            return

        self._messenger = LarkMessenger(binary=binary)
        self._stream = EventStream(binary=binary, state_home=state_home)
        async for msg in self._stream.stream():
            if not self._chat_enabled(msg.chat_id, msg.chat_type):
                continue
            abm = await self.convert_message(msg)
            await self.handle_msg(abm, msg.chat_id)
    async def _aresolve_binary(self) -> Path | None:
        binary = find_bundled_cli(VENDOR_DIR)
        if binary is not None:
            return binary
        if self.config.get("bootstrap_cli", True):
            try:
                from astrbot_lark_kit import bundled_cli_platform
                plat = bundled_cli_platform()
                plats = (plat,) if plat else ()
                if plats:
                    await asyncio.to_thread(ensure_bundled_cli, VENDOR_DIR, platforms=plats)
                binary = find_bundled_cli(VENDOR_DIR)
                if binary is not None:
                    return binary
            except Exception as exc:  # noqa: BLE001 — 自举失败降级 PATH 查找
                logger.warning(f"lark_cli 自举下载失败:{exc}")
        try:
            return resolve_cli_bin()
        except CliNotFoundError as exc:
            logger.error(f"lark_cli 平台启动失败:找不到 lark-cli 二进制:{exc}")
            return None

    # ---- 接收 ----------------------------------------------------------

    def _chat_enabled(self, chat_id: str, chat_type: str) -> bool:
        """enabled_chats 白名单;空列表 = 不限制。

        条目支持标准 UMO(``lark_cli:GroupMessage:oc_xx``)或裸 chat_id。
        """
        allowed = [str(c).strip() for c in self.config.get("enabled_chats") or []]
        if not allowed:
            return True
        msg_type = (
            MessageType.GROUP_MESSAGE if chat_type == "group" else MessageType.FRIEND_MESSAGE
        )
        umo = f"{self.meta().name}:{msg_type.value}:{chat_id}"
        return chat_id in allowed or umo in allowed

    async def convert_message(self, msg: NormalizedLarkMessage) -> AstrBotMessage:
        abm = AstrBotMessage()
        abm.type = (
            MessageType.GROUP_MESSAGE if msg.chat_type == "group" else MessageType.FRIEND_MESSAGE
        )
        if msg.chat_type == "group":
            abm.group_id = msg.chat_id
        abm.message_str = msg.text
        abm.sender = MessageMember(user_id=msg.sender_id, nickname=msg.sender_name or msg.sender_id)
        abm.message = [Plain(text=msg.text)]
        abm.raw_message = msg.raw
        abm.self_id = "lark_cli_bot"
        abm.session_id = msg.chat_id
        abm.message_id = msg.message_id
        abm.timestamp = int(msg.timestamp) if str(msg.timestamp).isdigit() else None
        return abm

    async def handle_msg(self, message: AstrBotMessage, chat_id: str) -> None:
        event = LarkCliPlatformEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            messenger=self._messenger,
            chat_id=chat_id,
        )
        self.commit_event(event)

    # ---- 发送 ----------------------------------------------------------

    async def send_by_session(self, session: MessageSesion, message_chain: MessageChain):
        """按会话发送:session_id 即 chat target(oc_/ou_ 前缀分派)。"""
        if self._messenger is None:
            logger.error("lark_cli send_by_session:messenger 未初始化(平台未 run)")
            return
        await deliver_chain(self._messenger, session.session_id, message_chain)


