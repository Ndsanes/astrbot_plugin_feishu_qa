"""CLI 调用层测试。

技巧:通过 LARK_CLI_PATH 指向临时生成的假可执行脚本,
无需网络即可覆盖全部成功/失败路径。
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from astrbot_lark_kit.cli import resolve_cli_bin, run_lark_cli
from astrbot_lark_kit.envelope import parse_envelope
from astrbot_lark_kit.errors import (
    AuthRequiredError,
    CliExecutionError,
    CliInvalidOutputError,
    CliNotFoundError,
    CliTimeoutError,
)

OK_ENVELOPE = {"ok": True, "identity": "user", "data": {"document": {"revision_id": 8268}}}
AUTH_FAIL_ENVELOPE = {
    "ok": False,
    "error": {"type": "auth", "subtype": "login_required", "message": "user token expired"},
}
API_FAIL_ENVELOPE = {
    "ok": False,
    "error": {"type": "api", "subtype": "invalid_parameters", "message": "Invalid parameter"},
}


def make_fake_cli(
    tmp_path: Path,
    *,
    stdout_obj: object | None = None,
    exit_code: int = 0,
    stderr_text: str = "",
    sleep_s: float = 0,
) -> Path:
    """生成一个假 lark-cli:输出指定 JSON 并以指定码退出。"""
    lines = ["#!/bin/sh"]
    if sleep_s:
        lines.append(f"sleep {sleep_s}")
    if stdout_obj is not None:
        payload = json.dumps(stdout_obj, ensure_ascii=False)
        lines.append(f"printf '%s' '{payload}'")
    if stderr_text:
        escaped = stderr_text.replace("'", "'\\''")
        lines.append(f"printf '%s' '{escaped}' 1>&2")
    lines.append(f"exit {exit_code}")
    script = tmp_path / "fake-lark-cli.sh"
    script.write_text("\n".join(lines) + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class TestResolveBin:
    def test_env_override(self, tmp_path: Path) -> None:
        fake = make_fake_cli(tmp_path)
        assert resolve_cli_bin(env={"LARK_CLI_PATH": str(fake)}) == fake

    def test_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CliNotFoundError):
            resolve_cli_bin(env={"LARK_CLI_PATH": str(tmp_path / "nope.sh")})

    def test_not_executable_raises(self, tmp_path: Path) -> None:
        plain = tmp_path / "not-exec"
        plain.write_text("#!/bin/sh\n")
        with pytest.raises(CliNotFoundError):
            resolve_cli_bin(env={"LARK_CLI_PATH": str(plain)})


class TestRunCli:
    async def test_success_envelope(self, tmp_path: Path) -> None:
        fake = make_fake_cli(tmp_path, stdout_obj=OK_ENVELOPE)
        envelope = await run_lark_cli(["docs", "+fetch"], env={"LARK_CLI_PATH": str(fake)})
        assert envelope.ok is True
        assert envelope.document["revision_id"] == 8268

    async def test_auth_failure_classified(self, tmp_path: Path) -> None:
        fake = make_fake_cli(tmp_path, stdout_obj=AUTH_FAIL_ENVELOPE)
        with pytest.raises(AuthRequiredError):
            await run_lark_cli(["docs", "+fetch"], env={"LARK_CLI_PATH": str(fake)})

    async def test_api_failure_classified_as_execution(self, tmp_path: Path) -> None:
        fake = make_fake_cli(tmp_path, stdout_obj=API_FAIL_ENVELOPE)
        with pytest.raises(CliExecutionError, match="invalid_parameters"):
            await run_lark_cli(["docs", "+fetch"], env={"LARK_CLI_PATH": str(fake)})

    async def test_nonzero_exit_without_envelope(self, tmp_path: Path) -> None:
        fake = make_fake_cli(tmp_path, stderr_text="boom", exit_code=2)
        with pytest.raises(CliExecutionError, match="boom"):
            await run_lark_cli(["x"], env={"LARK_CLI_PATH": str(fake)})

    async def test_invalid_json_output(self, tmp_path: Path) -> None:
        script = tmp_path / "garbage.sh"
        script.write_text("#!/bin/sh\nprintf 'not json'\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        with pytest.raises(CliInvalidOutputError):
            await run_lark_cli(["x"], env={"LARK_CLI_PATH": str(script)})

    async def test_timeout_kills_process(self, tmp_path: Path) -> None:
        fake = make_fake_cli(tmp_path, stdout_obj=OK_ENVELOPE, sleep_s=5)
        with pytest.raises(CliTimeoutError):
            await run_lark_cli(
                ["slow"],
                timeout_s=0.3,
                env={"LARK_CLI_PATH": str(fake)},
            )

    async def test_binary_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent-cli"
        with pytest.raises(CliNotFoundError):
            await run_lark_cli(["x"], bin_path=missing)


class TestParseEnvelope:
    def test_rejects_missing_ok(self) -> None:
        with pytest.raises(CliInvalidOutputError):
            parse_envelope('{"data": {}}')

    def test_error_defaults(self) -> None:
        envelope = parse_envelope('{"ok": true}')
        assert envelope.data == {}
        assert envelope.error.message == ""
