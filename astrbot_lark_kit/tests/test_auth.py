"""auth 解析与健康判定测试(spec §58)。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from astrbot_lark_kit.auth import (
    Health,
    auth_status_from_dict,
    health_of,
    parse_auth_status,
)
from astrbot_lark_kit.errors import CliInvalidOutputError


def status_json(
    *,
    user_available: bool = True,
    token_status: str = "valid",
    expires_at: str | None = "2099-01-01T00:00:00+08:00",
    refresh_expires_at: str | None = "2099-01-08T00:00:00+08:00",
    bot_available: bool = True,
    include_user: bool = True,
) -> str:
    identities: dict = {
        "bot": {"status": "ready" if bot_available else "expired", "available": bot_available}
    }
    if include_user:
        identities["user"] = {
            "status": "ready",
            "available": user_available,
            "openId": "ou_test",
            "userName": "脑袋",
            "tokenStatus": token_status,
        }
        if expires_at:
            identities["user"]["expiresAt"] = expires_at
        if refresh_expires_at:
            identities["user"]["refreshExpiresAt"] = refresh_expires_at
    return json.dumps({"appId": "cli_x", "identities": identities}, ensure_ascii=False)


class TestParse:
    def test_full_payload(self) -> None:
        status = parse_auth_status(status_json())
        assert status.bot_available is True
        assert status.user.open_id == "ou_test"
        assert status.user.refresh_expires_at is not None
        assert status.user_display == "脑袋"

    def test_missing_fields_tolerated(self) -> None:
        status = auth_status_from_dict({})
        assert status.user.available is False
        assert status.user.expires_at is None

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(CliInvalidOutputError):
            parse_auth_status("nope")


def hours_from_now(h: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=h)).isoformat()


class TestHealth:
    def test_healthy(self) -> None:
        status = auth_status_from_dict(
            json.loads(status_json(refresh_expires_at=hours_from_now(96)))
        )
        assert health_of(status) is Health.HEALTHY

    def test_expiring_soon(self) -> None:
        status = auth_status_from_dict(
            json.loads(status_json(refresh_expires_at=hours_from_now(24)))
        )
        assert health_of(status) is Health.EXPIRING_SOON

    def test_expired(self) -> None:
        status = auth_status_from_dict(
            json.loads(status_json(refresh_expires_at=hours_from_now(-1)))
        )
        assert health_of(status) is Health.EXPIRED

    def test_unavailable_when_no_user(self) -> None:
        raw = json.loads(status_json(include_user=False))
        assert health_of(auth_status_from_dict(raw)) is Health.UNAVAILABLE

    def test_token_valid_but_no_timestamps_is_healthy(self) -> None:
        raw = json.loads(
            status_json(expires_at=None, refresh_expires_at=None, token_status="valid")
        )
        # available=True 且 tokenStatus=valid:信息不足但身份在线 → 保守 HEALTHY 由
        # valid 分支承接;若连 available 都没有才 UNAVAILABLE
        assert health_of(auth_status_from_dict(raw)) is Health.HEALTHY

    def test_warning_hours_configurable(self) -> None:
        status = auth_status_from_dict(
            json.loads(status_json(refresh_expires_at=hours_from_now(72)))
        )
        assert health_of(status, warning_hours=48.0) is Health.HEALTHY
        assert health_of(status, warning_hours=96.0) is Health.EXPIRING_SOON
