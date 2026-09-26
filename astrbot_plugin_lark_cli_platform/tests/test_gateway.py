"""LarkGateway 网关:参数组装、错误包装、降级语义(不发真实子进程调用)。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import astrbot_plugin_lark_cli_platform.gateway as gw
from astrbot_plugin_lark_cli_platform.gateway import LarkGateway, LarkGatewayError


class StubMessenger:
    _env = {"HOME": "/state/home"}

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_text(self, target: str, text: str) -> None:
        self.sent.append(("text", target, text))

    async def send_image(self, target: str, image_path) -> None:
        self.sent.append(("image", target, image_path))


def make_gateway(monkeypatch, responder=None) -> tuple[LarkGateway, StubMessenger, list]:
    m = StubMessenger()
    g = LarkGateway(m)
    calls: list[dict] = []

    async def fake_run(args, **kwargs):
        calls.append({"args": args, **kwargs})
        if responder is not None:
            result = responder(calls[-1])
            if isinstance(result, Exception):
                raise result
            return result
        return SimpleNamespace(ok=True, document={}, data={})

    if monkeypatch is not None:
        monkeypatch.setattr(gw, "run_lark_cli", fake_run)

    async def fake_run_json(args, **kwargs):
        obj = await fake_run(args, **kwargs)
        return getattr(obj, "data", {}) or {}

    if monkeypatch is not None:
        monkeypatch.setattr(gw, "run_lark_cli_json", fake_run_json)
    return g, m, calls


async def test_send_text委托messenger():
    g, m, _ = make_gateway(None)
    await g.send_text("oc_chat1", "hi")
    assert m.sent == [("text", "oc_chat1", "hi")]

async def test_api透传组装参数并返回document(monkeypatch):
    g, _, calls = make_gateway(
        monkeypatch,
        lambda c: SimpleNamespace(ok=True, document={"total": 2}, data={}),
    )
    out = await g.api("post", "/open-apis/bitable/v1/x/records/search", data={"a": 1})
    assert out == {"total": 2}
    args = calls[0]["args"]
    assert args[:3] == ["api", "POST", "/open-apis/bitable/v1/x/records/search"]
    assert json.loads(args[args.index("--data") + 1]) == {"a": 1}


async def test_api无document时返回data(monkeypatch):
    g, _, _ = make_gateway(
        monkeypatch,
        lambda c: SimpleNamespace(ok=True, document=None, data={"items": []}),
    )
    assert await g.api("GET", "/open-apis/im/v1/chats") == {"items": []}


async def test_fetch_doc返回content且缺content报错(monkeypatch):
    g, _, calls = make_gateway(
        monkeypatch,
        lambda c: SimpleNamespace(
            ok=True, document={"content": "# 正文", "revision_id": 7}, data={}
        ),
    )
    assert await g.fetch_doc("https://my.feishu.cn/wiki/x") == "# 正文"
    args = calls[0]["args"]
    assert args[:2] == ["docs", "+fetch"] and args[-2:] == ["--as", "user"]

    bad = make_gateway(
        monkeypatch, lambda c: SimpleNamespace(ok=True, document={}, data={})
    )[0]
    with pytest.raises(LarkGatewayError):
        await bad.fetch_doc("doc_ref")


async def test_append_doc走临时文件并返回revision(monkeypatch, tmp_path: Path):
    seen_cwd: list[str | None] = []

    def respond(call):
        seen_cwd.append(call.get("cwd"))
        content_arg = call["args"][call["args"].index("--content") + 1]
        payload = Path(call["cwd"]) / content_arg.removeprefix("@")
        assert "追加内容" in payload.read_text(encoding="utf-8")
        return SimpleNamespace(ok=True, document={"revision_id": 12}, data={})

    g, _, _ = make_gateway(monkeypatch, respond)
    assert await g.append_doc("doc_ref", "追加内容") == 12
    assert seen_cwd[0]  # 使用了临时 cwd


async def test_download_media_preview失败退回download(monkeypatch, tmp_path: Path):
    saved = tmp_path / "tok.png"

    def respond(call):
        if "+preview" in call["args"]:
            raise gw.LarkKitError("preview failed")
        # 产物必须由本次 CLI 调用产出(而非调用前就存在,那会被预清理当残留删掉)
        saved.write_bytes(b"png")
        return SimpleNamespace(
            ok=True, document={}, data={"saved_path": str(saved)}
        )

    g, _, calls = make_gateway(monkeypatch, respond)
    out = await g.download_media("tok", tmp_path)
    assert out == saved
    joined = [" ".join(c["args"]) for c in calls]
    assert any("+preview" in j for j in joined) and any("+download" in j for j in joined)


async def test_download_media认preview的output_path键(monkeypatch, tmp_path: Path):
    """drive +preview 回报的是 output_path(非 saved_path),漏认即误判失败。"""

    def respond(call):
        saved = Path(call["cwd"]) / "tok.jpg"
        saved.write_bytes(b"jpg")
        return SimpleNamespace(ok=True, document={}, data={"output_path": str(saved)})

    g, _, calls = make_gateway(monkeypatch, respond)
    out = await g.download_media("tok", tmp_path)
    assert out is not None and out.name == "tok.jpg"
    assert calls[0]["args"][:2] == ["drive", "+preview"]


async def test_download_media回捞无扩展名产物(monkeypatch, tmp_path: Path):
    """CLI 只回报 stem+扩展名(或不回报)时,按 stem* 前缀回捞,不静默 None。"""

    def respond(call):
        (Path(call["cwd"]) / "tok.png").write_bytes(b"png")
        return SimpleNamespace(ok=True, document={}, data={})

    g, _, _ = make_gateway(monkeypatch, respond)
    out = await g.download_media("tok", tmp_path)
    assert out is not None and out.name == "tok.png"


async def test_download_media清理带扩展名的残留产物(monkeypatch, tmp_path: Path):
    """上一轮残留的 TOKEN.png 会让本轮三条通道全部 already exists(2026-09-27 事故)。"""
    stale = tmp_path / "tok.png"
    stale.write_bytes(b"stale")

    def respond(call):
        assert not stale.exists(), "残留必须在发起 CLI 前清掉"
        saved = Path(call["cwd"]) / "tok.png"
        saved.write_bytes(b"fresh")
        return SimpleNamespace(ok=True, document={}, data={"output_path": str(saved)})

    g, _, calls = make_gateway(monkeypatch, respond)
    out = await g.download_media("tok", tmp_path)
    assert out is not None and out.read_bytes() == b"fresh"
    assert len(calls) == 1, "清理后首条通道即应成功"


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (["drive", "+preview"], ["--if-exists", "overwrite"]),
        (["drive", "+download"], ["--overwrite"]),
        (["docs", "+media-preview"], ["--overwrite"]),
    ],
)
async def test_download_media覆盖开关逐通道正确(monkeypatch, tmp_path: Path, head, expected):
    seen: list[list[str]] = []

    def respond(call):
        if call["args"][:2] == head:
            seen.append(call["args"])
            raise gw.LarkKitError("boom")
        raise gw.LarkKitError("boom")

    g, _, _ = make_gateway(monkeypatch, respond)
    await g.download_media("tok", tmp_path)
    assert seen, f"{head} 应被调用"
    args = seen[0]
    idx = args.index(expected[0])
    assert args[idx:idx + len(expected)] == expected


async def test_auth_login_start缺device_code报错(monkeypatch):
    g, _, _ = make_gateway(monkeypatch, lambda c: SimpleNamespace(ok=True, document={}, data={}))
    with pytest.raises(LarkGatewayError):
        await g.auth_login_start()


async def test底层kit错误统一包装(monkeypatch):
    g, _, _ = make_gateway(monkeypatch, lambda c: gw.LarkKitError("boom"))
    with pytest.raises(LarkGatewayError, match="boom"):
        await g.fetch_doc("doc_ref")


async def test_api默认钉bot身份(monkeypatch):
    g, _, calls = make_gateway(monkeypatch)
    await g.api("GET", "/open-apis/wiki/v4/spaces")
    args = calls[0]["args"]
    assert args[args.index("--as") + 1] == "bot"


async def test_api可选user身份(monkeypatch):
    g, _, calls = make_gateway(
        monkeypatch, lambda c: SimpleNamespace(ok=True, document={"ok_data": 1}, data={})
    )
    await g.api("GET", "/open-apis/wiki/v4/spaces", as_identity="user")
    assert calls[0]["args"][-2:] == ["--as", "user"]


async def test_call通配层透传并默认钉bot(monkeypatch):
    g, _, calls = make_gateway(
        monkeypatch, lambda c: SimpleNamespace(ok=True, document={}, data={"items": [1]})
    )
    out = await g.call(["wiki", "+node-list", "--space-id", "7"])
    assert out == {"items": [1]}
    args = calls[0]["args"]
    assert args[:2] == ["wiki", "+node-list"]
    assert args[-2:] == ["--as", "bot"]


async def test_call_user身份与cwd透传(monkeypatch, tmp_path: Path):
    g, _, calls = make_gateway(monkeypatch)
    await g.call(["contact", "+search-user", "--query", "x"], as_identity="user",
                 cwd=str(tmp_path), timeout_s=9.0)
    rec = calls[0]
    assert rec["args"][-2:] == ["--as", "user"]
    assert rec["cwd"] == str(tmp_path)
    assert rec["timeout_s"] == 9.0


@pytest.mark.parametrize("head", ["auth", "config", "profile", "update", "doctor",
                                  "event", "help", "__complete"])
async def test_call拒绝保护首命令(monkeypatch, head):
    g, _, _ = make_gateway(monkeypatch)
    with pytest.raises(LarkGatewayError, match="不代理"):
        await g.call([head])


async def test_call拒绝自带as与profile旗标(monkeypatch):
    g, _, _ = make_gateway(monkeypatch)
    with pytest.raises(LarkGatewayError, match="--as"):
        await g.call(["im", "+chat-list", "--as", "user"])
    with pytest.raises(LarkGatewayError, match="--profile"):
        await g.call(["im", "+chat-list", "--profile", "p2"])


async def test_call拒绝空参数与非字符串项(monkeypatch):
    g, _, _ = make_gateway(monkeypatch)
    with pytest.raises(LarkGatewayError, match="非空字符串列表"):
        await g.call([])
    with pytest.raises(LarkGatewayError, match="非空字符串列表"):
        await g.call(["wiki", "+node-list", 7])  # type: ignore[list-item]
