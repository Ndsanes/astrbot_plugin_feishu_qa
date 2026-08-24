"""ensure_short_home 回归测试 —— 深路径登录态目录的 AF_UNIX socket 长度兜底。

背景:数据卷挂载点过深时,``<HOME>/.lark-cli/events/<appId>/bus.sock`` 超过
Linux AF_UNIX 108 字节上限,lark-cli 事件总线 bind 失败,consumer 稳定报
``did not become ready within 3s``(exit 5)。修复方式是给子进程 HOME 一个
/tmp 下的 symlink 别名(bind 只检查传入字符串长度,不解析 symlink)。
"""

from __future__ import annotations

import json
from pathlib import Path

from astrbot_lark_kit.events import EventStream
from astrbot_lark_kit.state import (
    _SOCKET_PATH_RESERVE,
    ensure_bot_credentials,
    ensure_short_home,
)

# 实测实例上的两种真实路径形态(2026-08-24,app cli_a728800c9f789013)
DEEP_FEISHU_QA = "/AstrBot/data/plugin_data/astrbot_plugin_feishu_qa/lark_cli_home"
DEEP_PLATFORM = "/AstrBot/data/plugin_data/astrbot_plugin_lark_cli_platform/lark_cli_home"


class TestShortPathPassthrough:
    def test_short_home_returned_unchanged(self, tmp_path: Path) -> None:
        home = tmp_path / "lark_cli_home"
        home.mkdir()
        # 放宽上限到不触发别名的程度,验证直通语义
        assert ensure_short_home(home, alias_root=tmp_path / "aliases", limit=10**6) == home

    def test_default_limit_math_keeps_normal_linux_paths(self) -> None:
        # 常规容器内短路径(如 /root/lh)不应触发别名:
        # /root/lh(8) + 余量(59) <= 108
        assert len("/root/lh") + _SOCKET_PATH_RESERVE <= 108


class TestDeepPathAlias:
    def test_deep_path_gets_alias(self, tmp_path: Path) -> None:
        home = tmp_path / "deep" / "nested" / "lark_cli_home"
        home.mkdir(parents=True)
        alias = ensure_short_home(home, alias_root=tmp_path / "aliases")
        assert alias != home
        assert alias.parent == tmp_path / "aliases"
        assert alias.is_symlink()
        assert alias.name.startswith("lark_home_")
        assert Path(alias.resolve()) == home.resolve()

    def test_instance_real_paths_would_trigger_alias(self) -> None:
        # 实例真实 socket 全长必须超限(否则本回归失去意义)
        for deep in (DEEP_FEISHU_QA, DEEP_PLATFORM):
            sock = f"{deep}/.lark-cli/events/cli_a728800c9f789013/bus.sock"
            assert len(sock) > 108, sock
        # 而别名后的 socket 全长必须留有余量
        alias = "/tmp/lark_home_0123456789ab"
        sock = f"{alias}/.lark-cli/events/cli_a728800c9f789013/bus.sock"
        assert len(sock) <= 108

    def test_alias_name_deterministic_and_idempotent(self, tmp_path: Path) -> None:
        home = tmp_path / "deep" / "lark_cli_home"
        home.mkdir(parents=True)
        root = tmp_path / "aliases"
        a1 = ensure_short_home(home, alias_root=root)
        a2 = ensure_short_home(home, alias_root=root)
        assert a1 == a2
        assert a1.is_symlink()
        assert len(list(root.iterdir())) == 1

    def test_stale_alias_pointing_elsewhere_is_repaired(self, tmp_path: Path) -> None:
        home = tmp_path / "deep" / "lark_cli_home"
        home.mkdir(parents=True)
        root = tmp_path / "aliases"
        other = tmp_path / "elsewhere"
        other.mkdir()
        first = ensure_short_home(home, alias_root=root)
        first.unlink()
        first.symlink_to(other, target_is_directory=True)
        repaired = ensure_short_home(home, alias_root=root)
        assert repaired == first
        assert Path(repaired.resolve()) == home.resolve()



class TestEnsureBotCredentials:
    def test_seeds_config_and_secret_file(self, tmp_path: Path) -> None:
        home = tmp_path / "lark_cli_home"
        home.mkdir()
        assert ensure_bot_credentials(home, app_id="cli_x", app_secret="s3cret") is True
        cfg = json.loads((home / ".lark-cli/config.json").read_text())
        app = cfg["apps"][0]
        assert app["appId"] == "cli_x"
        secret_file = Path(app["appSecret"]["id"])
        assert app["appSecret"]["source"] == "file"
        assert secret_file.read_text() == "s3cret"
        assert secret_file.stat().st_mode & 0o777 == 0o600

    def test_missing_credentials_returns_false(self, tmp_path: Path) -> None:
        home = tmp_path / "lark_cli_home"
        home.mkdir()
        assert ensure_bot_credentials(home, app_id="", app_secret="x") is False
        assert ensure_bot_credentials(home, app_id="cli_x", app_secret="  ") is False
        assert not (home / ".lark-cli/config.json").exists()

    def test_changed_credentials_overwritten(self, tmp_path: Path) -> None:
        import json

        home = tmp_path / "lark_cli_home"
        cli_dir = home / ".lark-cli"
        cli_dir.mkdir(parents=True)
        existing = {"apps": [{"appId": "cli_old", "users": []}]}
        (cli_dir / "config.json").write_text(json.dumps(existing))
        assert (
            ensure_bot_credentials(home, app_id="cli_new", app_secret="s") is True
        )
        cfg = json.loads((cli_dir / "config.json").read_text())
        assert cfg["apps"][0]["appId"] == "cli_new"  # 配置为权威,变更即覆盖

    def test_corrupted_config_rebuilt(self, tmp_path: Path) -> None:
        home = tmp_path / "lark_cli_home"
        cli_dir = home / ".lark-cli"
        cli_dir.mkdir(parents=True)
        (cli_dir / "config.json").write_text("{broken")
        assert ensure_bot_credentials(home, app_id="cli_x", app_secret="s") is True
        cfg = json.loads((cli_dir / "config.json").read_text())
        assert cfg["apps"][0]["appId"] == "cli_x"

class TestEventStreamIntegration:
    async def test_event_stream_uses_alias_for_deep_home(self, tmp_path: Path) -> None:
        home = tmp_path / "deep" / "lark_cli_home"
        home.mkdir(parents=True)
        logs: list[str] = []
        stream = EventStream(
            binary=tmp_path / "fake-cli",
            state_home=home,
            alias_root=tmp_path / "aliases",
            log_cb=logs.append,
        )
        assert stream._env["HOME"] != str(home)
        assert Path(stream._env["HOME"]).is_symlink()
        assert any("别名" in line for line in logs)

    async def test_event_stream_home_matches_pure_function(self, tmp_path: Path) -> None:
        home = tmp_path / "lh"
        home.mkdir()
        stream = EventStream(
            binary=tmp_path / "fake-cli",
            state_home=home,
            alias_root=tmp_path / "aliases",
        )
        expected = ensure_short_home(home, alias_root=tmp_path / "aliases")
        assert stream._env["HOME"] == str(expected)
        # 短路径(macOS tmp 之外的真实场景)应直通原目录
        assert (stream._env["HOME"] == str(home)) == (expected == home)
