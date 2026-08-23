"""一键重登卡片与 Device Flow 测试。"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from astrbot_plugin_feishu_qa.adapter.auth import (
    build_auth_card,
    initiate_reauth_flow,
)
from astrbot_plugin_feishu_qa.adapter.lark import LarkAdapter


def make_cli(tmp_path: Path, payload: dict) -> Path:
    script = tmp_path / "auth.sh"
    script.write_text(
        f"#!/bin/sh\nprintf '%s' '{json.dumps(payload, ensure_ascii=False)}'\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class TestCardBuilder:
    def test_card_shape_matches_modu_pattern(self) -> None:
        card = build_auth_card("https://example.com/verify", expires_in_min=10, reason="EXPIRED")
        assert card["schema"] == "2.0"
        assert card["header"]["template"] == "orange"
        elements = card["body"]["elements"]
        assert elements[0]["tag"] == "markdown"
        assert "重新授权" in elements[0]["content"]
        button = elements[1]
        assert button["tag"] == "button" and button["type"] == "primary"
        assert button["behaviors"][0]["default_url"] == "https://example.com/verify"
        assert json.dumps(card, ensure_ascii=False)  # 可序列化

    def test_reason_rendered(self) -> None:
        card = build_auth_card("u", reason="登录态已失效")
        assert "登录态已失效" in card["body"]["elements"][0]["content"]


class TestInitiateFlow:
    async def test_parses_device_flow(self, tmp_path: Path) -> None:
        fake = make_cli(
            tmp_path,
            {
                "device_code": "dev123",
                "verification_url": "https://accounts.feishu.cn/verify?c=xyz",
                "expires_in": 600,
            },
        )
        adapter = LarkAdapter(doc_ref="w", bin_path=fake)
        flow = await initiate_reauth_flow(adapter)
        assert flow["device_code"] == "dev123"
        assert flow["verification_url"].startswith("https://")

    async def test_missing_fields_returns_none(self, tmp_path: Path) -> None:
        fake = make_cli(tmp_path, {"device_code": "only"})
        adapter = LarkAdapter(doc_ref="w", bin_path=fake)
        assert await initiate_reauth_flow(adapter) is None
