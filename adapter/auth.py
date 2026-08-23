"""AuthKeeper — LarkAdapter 内置的登录态健康监控状态机(spec §7)。

不做成独立插件;去重规则:同一健康态只提醒一次,状态变化才再次提醒,
恢复 HEALTHY 后记录 recovered 并允许未来的新一轮提醒。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

try:
    from astrbot_lark_kit import Health
    from astrbot_lark_kit import run_lark_cli_json as run_lark_cli_json_raw
except ImportError:  # 打包分发时 kit 以子包形式随插件提供
    from ..astrbot_lark_kit import Health  # type: ignore
    from ..astrbot_lark_kit import run_lark_cli_json as run_lark_cli_json_raw  # type: ignore

from .lark import REAUTH_HINT, LarkAdapter

logger = logging.getLogger("feishu_qa.auth")

Notifier = Callable[[str], Awaitable[None]]


@dataclass
class KeeperRecord:
    """一次检查的结果快照(供 /qa_status 展示与测试断言)。"""

    checked_at: datetime
    health: Health
    notified: bool


@dataclass
class AuthKeeper:
    adapter: LarkAdapter
    warning_hours: float = 48.0
    _last_health: Health | None = None
    _last_notified_key: str | None = None
    _history: list[KeeperRecord] = field(default_factory=list)

    @property
    def last_health(self) -> Health | None:
        return self._last_health

    @property
    def history(self) -> list[KeeperRecord]:
        return list(self._history)

    def _should_notify(self, health: Health) -> bool:
        if health in (Health.HEALTHY,):
            return False  # 恢复通知单独处理
        return self._last_notified_key != health.value

    async def check(self, notify: Notifier) -> tuple[Health, bool]:
        """执行一次健康检查。

        Returns:
            (当前健康态, 本次是否发送了通知)
        """
        now = datetime.now(UTC)
        try:
            health = await self.adapter.auth_health(warning_hours=self.warning_hours)
        except Exception as exc:  # CLI 缺失/超时等 → 视为不可用但不抛出
            logger.warning("[AuthKeeper] 检查失败: %s", exc)
            health = Health.UNAVAILABLE

        notified = False
        previous = self._last_health

        if health is Health.HEALTHY and previous is not None and previous is not Health.HEALTHY:
            # 状态恢复:记录 recovered,允许未来新一轮提醒
            logger.info("[AuthKeeper] 登录态已恢复 (%s → HEALTHY)", previous.value)
            self._last_notified_key = None

        if self._should_notify(health):
            message = self._build_message(health)
            try:
                await notify(message)
                notified = True
                self._last_notified_key = health.value
            except Exception:
                # 通知失败不改变状态键,下次仍会重试提醒
                logger.warning("[AuthKeeper] 提醒发送失败,下次检查将重试")

        self._last_health = health
        self._history.append(KeeperRecord(checked_at=now, health=health, notified=notified))
        return health, notified

    @staticmethod
    def _build_message(health: Health) -> str:
        if health is Health.EXPIRING_SOON:
            return (
                f"[飞书QA] 登录态将在 {48} 小时内过期,请及时重新登录。\n{REAUTH_HINT}"
            )
        if health is Health.EXPIRED:
            return f"[飞书QA] 登录态已失效,文档同步暂停(旧语料继续服务)。\n{REAUTH_HINT}"
        if health is Health.UNAVAILABLE:
            return f"[飞书QA] 无法确认登录态(CLI 缺失或调用失败)。\n{REAUTH_HINT}"
        return f"[飞书QA] 登录态异常: {health.value}"


# ── 一键重登(spec 与 Modu feishu-auth 同构)──


def build_auth_card(
    verification_url: str,
    *,
    expires_in_min: int = 10,
    reason: str = "",
) -> dict:
    """构建重授权交互卡片(Card 2.0,与 Modu 生产版同构)。"""
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "header": {
            "title": {"tag": "plain_text", "content": "飞书QA 授权"},
            "template": "orange",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "\n".join(
                        [
                            "**文档同步需要重新授权**",
                            "",
                            f"触发原因:{reason}",
                            f"请在 {expires_in_min} 分钟内完成,超时需重新触发",
                        ]
                    ),
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "点击授权"},
                    "type": "primary",
                    "width": "fill",
                    "behaviors": [{"type": "open_url", "default_url": verification_url}],
                },
            ]
        },
    }


async def initiate_reauth_flow(
    adapter: LarkAdapter, *, domain: str = "docs"
) -> dict | None:
    """发起 Device Flow(非阻塞),返回 {device_code, verification_url, expires_in}。

    失败返回 None。
    """
    try:
        obj = await run_lark_cli_json_raw(
            ["auth", "login", "--domain", domain, "--no-wait", "--json"],
            timeout_s=adapter.timeout_s,
            limiter=adapter.limiter,
            env=adapter.env,
            bin_path=adapter.bin_path,
            extra_env=adapter.extra_env,
        )
    except Exception as exc:
        logger.warning("[AuthKeeper] 发起授权失败: %s", exc)
        return None
    if not obj.get("device_code") or not obj.get("verification_url"):
        logger.warning("[AuthKeeper] 授权响应缺字段: %s", list(obj))
        return None
    return obj


async def poll_auth_completion(
    adapter: LarkAdapter, device_code: str, *, timeout_s: float = 660.0
) -> bool:
    """阻塞轮询直到管理员完成授权或超时。返回是否成功。"""
    import asyncio as _asyncio

    try:
        await _asyncio.wait_for(
            run_lark_cli_json_raw(
                ["auth", "login", "--device-code", device_code, "--json"],
                timeout_s=timeout_s,
                limiter=adapter.limiter,
                env=adapter.env,
                bin_path=adapter.bin_path,
                extra_env=adapter.extra_env,
            ),
            timeout=timeout_s,
        )
        return True
    except Exception:
        return False
