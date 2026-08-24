"""AuthKeeper — 登录态健康监控状态机(spec §7)。

去重规则:同一健康态只提醒一次,状态变化才再次提醒,
恢复 HEALTHY 后记录 recovered 并允许未来的新一轮提醒。
健康判定与状态解析复用 astrbot_lark_kit 的纯逻辑(无子进程);
实际查询经 GatewayClient 转发到 lark_cli 平台网关。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

try:
    from astrbot_lark_kit import Health
except ImportError:  # 打包分发时 kit 以子包形式随插件提供
    from ..astrbot_lark_kit import Health  # type: ignore

from .gateway import REAUTH_HINT, GatewayClient

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
    adapter: GatewayClient
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
        except Exception as exc:  # 网关缺失/调用失败 → 视为不可用但不抛出
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
            return f"[飞书QA] 无法确认登录态(lark_cli 平台未加载或调用失败)。\n{REAUTH_HINT}"
        return f"[飞书QA] 登录态异常: {health.value}"
