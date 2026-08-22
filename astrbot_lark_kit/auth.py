"""登录态(auth)状态解析与健康判定。

数据来源:`lark-cli auth status` 的 JSON 输出。字段可能缺失,
解析必须防御式,不做结构性假设。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .errors import CliInvalidOutputError


class Health(StrEnum):
    HEALTHY = "HEALTHY"
    EXPIRING_SOON = "EXPIRING_SOON"
    EXPIRED = "EXPIRED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class UserIdentity:
    """user 身份的可观察状态。"""

    available: bool = False
    open_id: str = ""
    user_name: str = ""
    token_status: str = ""  # CLI 原样值,如 "valid"
    expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None


@dataclass
class AuthStatus:
    bot_available: bool = False
    user: UserIdentity = None  # type: ignore[assignment]  # __post_init__ 兜底
    raw: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.user is None:
            self.user = UserIdentity()
        if self.raw is None:
            self.raw = {}

    @property
    def user_display(self) -> str:
        return self.user.user_name or self.user.open_id or "未知用户"


def _parse_ts(value: object) -> datetime | None:
    """解析 ISO8601 时间戳(如 "2026-08-22T23:22:07+08:00"),失败返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_auth_status(raw_json: str) -> AuthStatus:
    try:
        obj = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise CliInvalidOutputError(f"auth status 输出不是合法 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise CliInvalidOutputError("auth status 输出顶层不是对象")
    return auth_status_from_dict(obj)


def auth_status_from_dict(obj: dict) -> AuthStatus:
    identities = obj.get("identities") if isinstance(obj.get("identities"), dict) else {}
    bot_raw = identities.get("bot") if isinstance(identities.get("bot"), dict) else {}
    user_raw = identities.get("user") if isinstance(identities.get("user"), dict) else {}

    bot_available = bool(bot_raw.get("available")) or (
        str(bot_raw.get("status", "")).lower() == "ready"
    )
    user = UserIdentity(
        available=bool(user_raw.get("available")),
        open_id=str(user_raw.get("openId") or ""),
        user_name=str(user_raw.get("userName") or ""),
        token_status=str(user_raw.get("tokenStatus") or ""),
        expires_at=_parse_ts(user_raw.get("expiresAt")),
        refresh_expires_at=_parse_ts(user_raw.get("refreshExpiresAt")),
    )
    return AuthStatus(bot_available=bot_available, user=user, raw=obj)


def health_of(
    status: AuthStatus,
    *,
    warning_hours: float = 48.0,
    now: datetime | None = None,
) -> Health:
    """判定登录态健康度(spec §6)。

    规则(按可判定的最高优先级):
    - user 身份不存在/不可用 → UNAVAILABLE
    - refreshExpiresAt 已过 → EXPIRED
    - refreshExpiresAt 距今 <= warning_hours → EXPIRING_SOON
    - 有未来时间戳或 token_status == valid → HEALTHY
    - 其余(信息不足且 token 非 valid)→ UNAVAILABLE
    """
    user = status.user
    if not user.available and not user.token_status:
        return Health.UNAVAILABLE

    current = now or datetime.now(UTC)
    if user.refresh_expires_at is not None:
        if user.refresh_expires_at <= current:
            return Health.EXPIRED
        hours_left = (user.refresh_expires_at - current).total_seconds() / 3600.0
        if hours_left <= warning_hours:
            return Health.EXPIRING_SOON
        return Health.HEALTHY

    if user.expires_at is not None:
        if user.expires_at <= current:
            return Health.EXPIRED
        hours_left = (user.expires_at - current).total_seconds() / 3600.0
        if hours_left <= warning_hours:
            return Health.EXPIRING_SOON
        return Health.HEALTHY

    if user.token_status.lower() == "valid":
        return Health.HEALTHY
    if user.available:
        # 身份在但无法确认时效:保守视为需要关注
        return Health.EXPIRING_SOON
    return Health.UNAVAILABLE
