"""LarkAdapter 与 AuthKeeper 测试(spec §57/§58)。

全部通过假 lark-cli 可执行文件驱动,无网络依赖。
keeper 测试用可变 status 文件驱动健康态切换。
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astrbot_lark_kit import Health
from astrbot_lark_kit.errors import CliInvalidOutputError
from astrbot_plugin_feishu_qa.adapter.auth import AuthKeeper
from astrbot_plugin_feishu_qa.adapter.lark import DOC_FORMAT_XML, LarkAdapter

DOC_ENVELOPE = json.dumps(
    {
        "ok": True,
        "identity": "user",
        "data": {
            "document": {
                "content": "# 标题\n正文",
                "document_id": "HzxwdOdKloIhNdx7nQTcVJXfnBe",
                "revision_id": 8268,
            }
        },
    },
    ensure_ascii=False,
)


def make_cli(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class TestFetchDoc:
    async def test_markdown_and_revision(self, tmp_path: Path) -> None:
        fake = make_cli(tmp_path, "cli.sh", f"printf '%s' '{DOC_ENVELOPE}'")
        adapter = LarkAdapter(doc_ref="wiki/abc", bin_path=fake)
        doc = await adapter.fetch_doc()
        assert doc.revision_id == 8268
        assert doc.content.startswith("# 标题")
        assert doc.document_id == "HzxwdOdKloIhNdx7nQTcVJXfnBe"

    async def test_xml_format_passed_through(self, tmp_path: Path) -> None:
        spy = tmp_path / "spy.txt"
        payload = DOC_ENVELOPE.replace("'", "'\\''")
        body = (
            'for a in "$@"; do printf "[%s]" "$a"; done > "'
            + str(spy)
            + '"\n'
            + f"printf '%s' '{payload}'\n"
        )
        fake = make_cli(tmp_path, "spy.sh", body)
        adapter = LarkAdapter(doc_ref="wiki/abc", bin_path=fake)
        await adapter.fetch_doc(fmt=DOC_FORMAT_XML)
        recorded = spy.read_text()
        assert "[--doc-format][xml]" in recorded
        assert "[--as][user]" in recorded

    async def test_missing_content_raises(self, tmp_path: Path) -> None:
        bad = json.dumps({"ok": True, "data": {"document": {}}})
        fake = make_cli(tmp_path, "bad.sh", f"printf '%s' '{bad}'")
        adapter = LarkAdapter(doc_ref="wiki/abc", bin_path=fake)
        with pytest.raises(CliInvalidOutputError):
            await adapter.fetch_doc()


class TestDownloadMedia:
    def _media_cli(self, tmp_path: Path, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        envelope = json.dumps(
            {
                "ok": True,
                "data": {
                    "saved_path": str(out),
                    "size_bytes": 7,
                    "content_type": "image/png",
                },
            }
        )
        body = f'printf "PNGDATA" > "{out}"\nprintf \'%s\' \'{envelope}\'\n'
        return make_cli(tmp_path, "media.sh", body)

    async def test_download_writes_file(self, tmp_path: Path) -> None:
        out = tmp_path / "img" / "a.png"
        fake = self._media_cli(tmp_path, out)
        adapter = LarkAdapter(doc_ref="wiki/abc", bin_path=fake)
        result = await adapter.download_media("tok123", out)
        assert result is not None and result.read_text() == "PNGDATA"

    async def test_skip_existing_no_cli_call(self, tmp_path: Path) -> None:
        out = tmp_path / "exists.png"
        out.write_text("OLD")
        marker = tmp_path / "called"

        # 这个假 CLI 若被调用会留下标记
        envelope = json.dumps({"ok": True, "data": {}})
        body = (
            f'touch "{marker}"\n'
            f"printf '%s' '{envelope}'\n"
        )
        fake = make_cli(tmp_path, "should-not-run.sh", body)
        adapter = LarkAdapter(doc_ref="wiki/abc", bin_path=fake)
        result = await adapter.download_media("tok", out)
        assert result is not None and result.read_text() == "OLD"
        assert not marker.exists(), "已存在文件不应触发 CLI 调用"

    async def test_failure_returns_none(self, tmp_path: Path) -> None:
        fake = make_cli(tmp_path, "fail.sh", "exit 1")
        adapter = LarkAdapter(doc_ref="wiki/abc", bin_path=fake)
        assert await adapter.download_media("tok", tmp_path / "never.png") is None


# ── auth ──


def status_json(refresh_hours_from_now: float | None) -> dict:
    obj: dict = {
        "identities": {
            "bot": {"status": "ready", "available": True},
        }
    }
    user: dict = {"available": True, "tokenStatus": "valid", "openId": "ou_x"}
    if refresh_hours_from_now is not None:
        ts = datetime.now(UTC) + timedelta(hours=refresh_hours_from_now)
        user["refreshExpiresAt"] = ts.isoformat()
    obj["identities"]["user"] = user
    return obj


def write_status_file(path: Path, refresh_hours: float | None) -> None:
    path.write_text(json.dumps(status_json(refresh_hours)))


class TestAuthStatus:
    async def test_parses_health(self, tmp_path: Path) -> None:
        status_file = tmp_path / "auth.json"
        write_status_file(status_file, 96)
        fake = make_cli(tmp_path, "auth.sh", f'cat "{status_file}"')
        adapter = LarkAdapter(doc_ref="wiki/abc", bin_path=fake)

        assert await adapter.auth_health() is Health.HEALTHY


class TestAuthKeeper:
    """状态机行为(spec §58):首提醒、去重、升级提醒、恢复复位。"""

    def _make(self, tmp_path: Path):
        status_file = tmp_path / "auth.json"
        write_status_file(status_file, 96)
        fake = make_cli(tmp_path, "auth.sh", f'cat "{status_file}"')
        adapter = LarkAdapter(doc_ref="wiki/abc", bin_path=fake)
        sent: list[str] = []

        async def notify(message: str) -> None:
            sent.append(message)

        return status_file, AuthKeeper(adapter=adapter), sent, notify

    async def test_healthy_first_check_no_notify(self, tmp_path: Path) -> None:
        status_file, keeper, sent, notify = self._make(tmp_path)
        health, notified = await keeper.check(notify)
        assert health.value == "HEALTHY"
        assert not notified and sent == []
        assert keeper.last_health is Health.HEALTHY
        assert status_file.exists()

    async def test_expiring_notifies_once_then_dedupes(self, tmp_path: Path) -> None:
        status_file, keeper, sent, notify = self._make(tmp_path)

        write_status_file(status_file, 24)
        health1, notified1 = await keeper.check(notify)
        assert health1 is Health.EXPIRING_SOON and notified1 and len(sent) == 1

        # 状态未变化 → 不重复提醒
        _, notified2 = await keeper.check(notify)
        assert not notified2 and len(sent) == 1

    async def test_escalation_to_expired_notifies_again(self, tmp_path: Path) -> None:
        status_file, keeper, sent, notify = self._make(tmp_path)

        write_status_file(status_file, 24)
        await keeper.check(notify)

        write_status_file(status_file, -1)  # 恶化 → 新一轮提醒
        health2, notified2 = await keeper.check(notify)
        assert health2 is Health.EXPIRED
        assert notified2 and len(sent) == 2

    async def test_recovery_resets_dedupe_key(self, tmp_path: Path) -> None:
        status_file, keeper, sent, notify = self._make(tmp_path)

        write_status_file(status_file, -1)
        await keeper.check(notify)
        assert len(sent) == 1

        write_status_file(status_file, 96)  # 恢复
        health3, notified3 = await keeper.check(notify)
        assert health3 is Health.HEALTHY and not notified3

        # 再次临近过期 → 因为恢复已复位,允许新一轮提醒
        write_status_file(status_file, 12)
        _, notified4 = await keeper.check(notify)
        assert notified4 and len(sent) == 2

    async def test_cli_failure_treated_as_unavailable(self, tmp_path: Path) -> None:
        status_file = tmp_path / "auth.json"
        write_status_file(status_file, 96)
        fake = make_cli(tmp_path, "broken.sh", "exit 3")
        keeper = AuthKeeper(adapter=LarkAdapter(doc_ref="w", bin_path=fake))
        sent: list[str] = []

        async def notify(message: str) -> None:
            sent.append(message)

        health, notified = await keeper.check(notify)
        assert health in (Health.UNAVAILABLE, Health.EXPIRED) or health is Health.UNAVAILABLE
        assert health is Health.UNAVAILABLE
