"""CLI 调用层测试。

技巧:通过 LARK_CLI_PATH 指向临时生成的假可执行脚本,
无需网络即可覆盖全部成功/失败路径。
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from astrbot_lark_kit.cli import (
    find_bundled_cli,
    resolve_cli_bin,
    run_lark_cli,
    run_lark_cli_json,
)
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



class TestBundledCli:
    def test_platform_mapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import astrbot_lark_kit.cli as mod

        cases = [
            ("linux", "x86_64", "linux-amd64"),
            ("linux", "aarch64", "linux-arm64"),
            ("darwin", "arm64", "darwin-arm64"),
        ]
        for sys_pf, machine, expect in cases:
            monkeypatch.setattr(mod.sys, "platform", sys_pf)
            monkeypatch.setattr(mod.platform, "machine", lambda m=machine: m)
            assert mod.bundled_cli_platform() == expect
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setattr(mod.platform, "machine", lambda: "AMD64")
        assert mod.bundled_cli_platform() is None

    def test_find_bundled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import astrbot_lark_kit.cli as mod

        monkeypatch.setattr(mod, "bundled_cli_platform", lambda: "linux-amd64")
        assert find_bundled_cli(tmp_path) is None  # 目录不存在
        bin_dir = tmp_path / "linux-amd64"
        bin_dir.mkdir()
        (bin_dir / "lark-cli").write_text("#!/bin/sh\n")
        (bin_dir / "lark-cli").chmod(0o755)
        found = find_bundled_cli(tmp_path)
        assert found == bin_dir / "lark-cli"
        (bin_dir / "lark-cli").chmod(0o644)  # 不可执行 → 视为缺失
        assert find_bundled_cli(tmp_path) is None


class TestExtraEnv:
    async def test_extra_env_reaches_child(self, tmp_path: Path) -> None:
        script = tmp_path / "show-home.sh"
        script.write_text('#!/bin/sh\nprintf \'{"home":"%s"}\' "$HOME"\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        obj = await run_lark_cli_json(
            ["x"],
            env={"LARK_CLI_PATH": str(script)},
            extra_env={"HOME": "/custom/state"},
        )
        assert obj["home"] == "/custom/state"


class TestParseEnvelope:
    def test_rejects_missing_ok(self) -> None:
        with pytest.raises(CliInvalidOutputError):
            parse_envelope('{"data": {}}')

    def test_error_defaults(self) -> None:
        envelope = parse_envelope('{"ok": true}')
        assert envelope.data == {}
        assert envelope.error.message == ""


class TestRunCliJson:
    async def test_plain_json_object(self, tmp_path: Path) -> None:
        fake = make_fake_cli(
            tmp_path,
            stdout_obj={"appId": "cli_x", "identities": {"bot": {"available": True}}},
        )
        from astrbot_lark_kit.cli import run_lark_cli_json

        obj = await run_lark_cli_json(["auth", "status"], env={"LARK_CLI_PATH": str(fake)})
        assert obj["appId"] == "cli_x"

    async def test_rejects_non_object(self, tmp_path: Path) -> None:
        script = tmp_path / "array.sh"
        script.write_text("#!/bin/sh\nprintf '[1,2]'\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        from astrbot_lark_kit.cli import run_lark_cli_json

        with pytest.raises(CliInvalidOutputError):
            await run_lark_cli_json(["x"], env={"LARK_CLI_PATH": str(script)})
