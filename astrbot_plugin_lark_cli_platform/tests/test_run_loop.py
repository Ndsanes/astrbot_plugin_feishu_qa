"""run 循环:白名单过滤、事件提交、二进制解析路径。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api.platform import MessageType

import astrbot_plugin_lark_cli_platform.platform_adapter as pa

from .helpers import FakeMessenger, FakeStream, make_msg


def make_adapter(config_overrides=None, queue=None):
    config = {"lark_cli_home": "", "bootstrap_cli": False, "enabled_chats": []}
    config.update(config_overrides or {})
    q = queue if queue is not None else asyncio.Queue()
    return pa.LarkCliPlatform(config, {}, q), q


def install_fakes(monkeypatch, messages, binary=Path("/fake/lark-cli")):
    monkeypatch.setattr(pa, "find_bundled_cli", lambda vendor: binary)
    monkeypatch.setattr(pa, "EventStream", lambda **kw: FakeStream(messages))
    monkeypatch.setattr(pa, "LarkMessenger", lambda **kw: FakeMessenger())


async def test_run_commits_events(monkeypatch):
    adapter, queue = make_adapter()
    install_fakes(
        monkeypatch,
        [
            make_msg(message_id="om_a"),
            make_msg(message_id="om_b", chat_id="ou_peer", chat_type="p2p", text="私聊"),
        ],
    )
    await adapter.run()
    ev1, ev2 = queue.get_nowait(), queue.get_nowait()
    assert queue.empty()
    assert ev1.chat_id == "oc_chat"
    assert ev1.message_obj.type == MessageType.GROUP_MESSAGE
    assert ev1.session_id == "oc_chat"
    assert ev2.message_obj.type == MessageType.FRIEND_MESSAGE
    # UMO 一级验收:platform_id:message_type:session_id
    assert (
        f"lark_cli:{ev1.message_obj.type.value}:{ev1.session_id}"
        == "lark_cli:GroupMessage:oc_chat"
    )


async def test_run_whitelist_filters(monkeypatch):
    adapter, queue = make_adapter({"enabled_chats": ["lark_cli:GroupMessage:oc_chat"]})
    install_fakes(
        monkeypatch,
        [
            make_msg(message_id="om_in"),
            make_msg(message_id="om_out", chat_id="oc_other"),  # 不在白名单
            make_msg(  # 裸 chat_id 条目命中 p2p
                message_id="om_p2p",
                chat_id="ou_dm",
                chat_type="p2p",
                text="hi",
            ),
        ],
    )
    # 白名单只含 group UMO:p2p 也应被挡掉
    adapter.config["enabled_chats"] = ["lark_cli:GroupMessage:oc_chat"]
    await adapter.run()
    assert queue.get_nowait().message_obj.message_id == "om_in"
    assert queue.empty()


async def test_run_whitelist_empty_allows_all(monkeypatch):
    adapter, queue = make_adapter({"enabled_chats": []})
    install_fakes(monkeypatch, [make_msg(), make_msg(chat_id="x", message_id="om_x")])
    await adapter.run()
    assert queue.qsize() == 2


async def test_bootstrap_downloads_when_missing(monkeypatch, tmp_path):
    """vendored 缺失 + bootstrap_cli=true → ensure_bundled_cli 被调用并采用结果。"""
    calls = []

    def fake_ensure(vendor_dir, *, platforms=None):
        calls.append(vendor_dir)
        return {}

    monkeypatch.setattr(pa, "VENDOR_DIR", tmp_path / "vendor")
    monkeypatch.setattr(pa, "find_bundled_cli", lambda v: None)
    monkeypatch.setattr(pa, "ensure_bundled_cli", fake_ensure)
    monkeypatch.setattr(pa, "resolve_cli_bin", lambda env=None: Path("/path/cli"))
    monkeypatch.setattr(pa.StarTools, "get_data_dir", classmethod(lambda cls, n=None: tmp_path))
    monkeypatch.setattr(pa, "EventStream", lambda **kw: FakeStream([make_msg()]))
    messenger = FakeMessenger()
    monkeypatch.setattr(pa, "LarkMessenger", lambda **kw: messenger)

    adapter, queue = make_adapter({"bootstrap_cli": True})
    await adapter.run()
    assert calls == [tmp_path / "vendor"]
    assert isinstance(adapter._messenger, FakeMessenger)


async def test_missing_binary_is_graceful(monkeypatch, tmp_path):
    """三路解析全失败 → 记录错误并安静返回,不崩。"""
    monkeypatch.setattr(pa, "find_bundled_cli", lambda v: None)
    monkeypatch.setattr(
        pa, "ensure_bundled_cli", lambda v: (_ for _ in ()).throw(RuntimeError("no net"))
    )

    def raise_not_found(env=None):
        raise pa.CliNotFoundError("not found")

    monkeypatch.setattr(pa, "resolve_cli_bin", raise_not_found)
    monkeypatch.setattr(pa.StarTools, "get_data_dir", classmethod(lambda cls, n=None: tmp_path))

    adapter, queue = make_adapter({"bootstrap_cli": True})
    await adapter.run()  # 不应抛异常
    assert queue.empty()


async def test_state_home_prefers_config(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(pa, "find_bundled_cli", lambda v: Path("/fake/cli"))
    monkeypatch.setattr(
        pa,
        "EventStream",
        lambda *, binary, state_home=None: captured.update(state_home=state_home) or FakeStream([]),
    )
    monkeypatch.setattr(pa, "LarkMessenger", lambda **kw: FakeMessenger())
    custom = tmp_path / "custom_home"

    adapter, _ = make_adapter({"lark_cli_home": str(custom)})
    await adapter.run()
    assert captured["state_home"] == custom

    adapter2, _ = make_adapter({"lark_cli_home": ""})
    monkeypatch.setattr(
        pa.StarTools, "get_data_dir", classmethod(lambda cls, n=None: tmp_path / "data")
    )
    await adapter2.run()
    assert captured["state_home"].name != "custom_home"


def test_registration_metadata():
    name, desc, tmpl = pa.LarkCliPlatform._adapter_meta
    assert name == "lark_cli"
    forbidden = {"app_id", "app_secret", "send_as", "receive_as", "identity", "token"}
    assert not forbidden & set(tmpl)
    assert set(tmpl) == {"lark_cli_home", "bootstrap_cli", "enabled_chats"}
