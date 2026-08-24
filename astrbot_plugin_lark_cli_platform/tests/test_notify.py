"""notify_umos 通知与授权卡片闭环(不发真实子进程调用)。"""

from __future__ import annotations

import asyncio

import pytest

from astrbot_plugin_lark_cli_platform.platform_adapter import LarkCliPlatform


def make_adapter(config=None) -> LarkCliPlatform:
    return LarkCliPlatform(
        {
            "lark_cli_home": "",
            "notify_umos": [],
            **(config or {}),
        },
        {},
        event_queue=None,
    )


class FakeGateway:
    def __init__(self) -> None:
        self.cards: list[tuple[str, dict]] = []
        self.status_obj: dict = {}
        self.login_start: dict = {}

    async def send_card(self, target: str, card: dict) -> None:
        self.cards.append((target, card))

    async def auth_status(self) -> dict:
        return self.status_obj

    async def auth_login_start(self, domains: str = "docs,drive,wiki") -> dict:
        self.login_domains = domains
        return self.login_start

    async def auth_login_finish(self, device_code: str) -> None:
        return None


async def test_send_admin_card按UMO末段分发():
    adapter = make_adapter({"notify_umos": ["lark_cli:FriendMessage:oc_p2p", "bad_entry"]})
    gw = FakeGateway()
    adapter.gateway = gw
    sent = await adapter.send_admin_card({"elements": []})
    # bad_entry 无 oc_/ou_ 末段,跳过并告警
    assert sent == 1 and gw.cards[0][0] == "oc_p2p"


async def test_send_admin_card网关未就绪返回0():
    adapter = make_adapter({"notify_umos": ["lark_cli:FriendMessage:oc_p2p"]})
    assert await adapter.send_admin_card({}) == 0


def test_reauth卡片含按钮链接():
    card = LarkCliPlatform._build_reauth_card("登录态已过期", "https://example/verify")
    assert card["header"]["template"] == "red"
    action = card["elements"][1]["actions"][0]
    assert action["url"] == "https://example/verify"


async def test_begin_reauth缺通知目标时拒绝():
    adapter = make_adapter()
    adapter.gateway = FakeGateway()
    result = await adapter.begin_reauth("手动触发")
    assert "notify_umos" in result


async def test_begin_reauth推卡并带链接(monkeypatch):
    adapter = make_adapter({"notify_umos": ["lark_cli:FriendMessage:oc_p2p"]})
    gw = FakeGateway()
    gw.login_start = {"device_code": "dev1", "verification_url": "https://v/x"}
    adapter.gateway = gw
    monkeypatch.setattr(adapter, "_finish_reauth", lambda code: _noop(code))
    result = await adapter.begin_reauth("手动触发")
    assert "https://v/x" in result and gw.cards[0][0] == "oc_p2p"


async def _noop(code: str) -> None:
    return None

async def test_begin_reauth使用配置的授权域(monkeypatch):
    adapter = make_adapter({
        "notify_umos": ["lark_cli:FriendMessage:oc_p2p"],
        "auth_login_domains": "docs, wiki , im",
    })
    gw = FakeGateway()
    gw.login_start = {"device_code": "dev1", "verification_url": "https://v/x"}
    adapter.gateway = gw
    monkeypatch.setattr(adapter, "_finish_reauth", _noop)
    await adapter.begin_reauth("手动触发")
    assert gw.login_domains == "docs,wiki,im"


async def test_登录态缺失时启动即推卡(monkeypatch):
    """冷启动(从未授权):首查立即发起授权,不等检查间隔。"""
    adapter = make_adapter({
        "notify_umos": ["lark_cli:FriendMessage:oc_p2p"],
        "auth_check_hours": 10 ** 6,
    })
    gw = FakeGateway()
    gw.status_obj = {"identities": {"user": {"status": "missing", "available": False}}}
    gw.login_start = {"device_code": "dev1", "verification_url": "https://v/x"}
    adapter.gateway = gw
    monkeypatch.setattr(adapter, "_finish_reauth", _noop)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(adapter._auth_loop()), timeout=2.0)
    assert gw.login_domains == "docs,drive,wiki"
    assert gw.cards and gw.cards[0][0] == "oc_p2p"


async def test_健康状态不发卡且循环存活(monkeypatch):
    adapter = make_adapter({"notify_umos": ["lark_cli:FriendMessage:oc_p2p"]})
    gw = FakeGateway()
    gw.status_obj = {
        "identities": {
            "user": {"available": True, "tokenStatus": "valid",
                     "refreshExpiresAt": "2099-01-01T00:00:00+08:00"},
        },
    }
    adapter.gateway = gw
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(adapter._auth_loop()), timeout=1.0)
    assert gw.cards == []


async def test_检查抛异常不终止循环(monkeypatch):
    adapter = make_adapter({"notify_umos": ["lark_cli:FriendMessage:oc_p2p"]})

    class BoomGateway(FakeGateway):
        async def auth_status(self):
            raise RuntimeError("cli gone")

    gw = BoomGateway()
    adapter.gateway = gw
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(adapter._auth_loop()), timeout=1.0)
