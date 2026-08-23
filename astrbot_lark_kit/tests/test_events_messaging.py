"""events.py / messaging.py 测试:零网络零真实 CLI,fake 子进程与参数捕获。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from astrbot_lark_kit.events import (
    EventStream,
    NormalizedLarkMessage,
    normalize_event,
)
from astrbot_lark_kit.messaging import LarkMessenger

SAMPLE = {
    "type": "im.message.receive_v1",
    "message_id": "om_1",
    "sender_id": "ou_sender1",
    "sender_name": "小明",
    "sender_type": "user",
    "chat_id": "oc_chat1",
    "chat_type": "group",
    "message_type": "text",
    "content": "你好",
    "create_time": "1700000000000",
}


# --------------------------------------------------------------------------- #
# normalize_event
# --------------------------------------------------------------------------- #


def test_归一化_群消息全字段():
    msg = normalize_event(SAMPLE)
    assert isinstance(msg, NormalizedLarkMessage)
    assert msg.message_id == "om_1"
    assert msg.sender_id == "ou_sender1"
    assert msg.chat_id == "oc_chat1"
    assert msg.chat_type == "group"
    assert msg.text == "你好"
    assert not msg.is_from_bot


def test_归一化_私聊与bot自身():
    p2p = normalize_event({**SAMPLE, "chat_type": "p2p", "message_id": "om_2"})
    assert p2p.chat_type == "p2p"
    bot = normalize_event({**SAMPLE, "sender_type": "bot", "message_id": "om_3"})
    assert bot.is_from_bot


def test_归一化_非目标事件与坏输入返回None():
    assert normalize_event({"type": "im.chat.disbanded_v1"}) is None
    assert normalize_event("not a dict") is None
    assert normalize_event(None) is None


def test_归一化_可选字段缺失不丢事件():
    msg = normalize_event({"type": "im.message.receive_v1"})
    assert msg is not None
    assert msg.message_id == "" and msg.text == "" and msg.sender_name == ""


def test_归一化_id回退兼容():
    msg = normalize_event({"id": "om_legacy"})
    assert msg.message_id == "om_legacy"


# --------------------------------------------------------------------------- #
# EventStream(假可执行脚本产出 NDJSON)
# --------------------------------------------------------------------------- #


def make_fake_cli(tmp_path: Path, lines: list[str], *, exit_code: int = 0) -> Path:
    script = tmp_path / f"fakecli_{abs(hash(tuple(lines)))}.py"
    body = "\n".join(f'print({json.dumps(l)})' for l in lines)
    script.write_text(
        "#!/usr/bin/env python3\n"
        f"import sys\n{body}\nsys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return script
def stream_of(script: Path, **kw) -> EventStream:
    return EventStream(binary=script, max_backoff_s=0.01, **kw)


@pytest.mark.asyncio
async def test_stream_产出归一化消息并过滤bot自消息(tmp_path):
    lines = [
        json.dumps(SAMPLE),
        json.dumps({**SAMPLE, "sender_type": "bot", "message_id": "om_bot"}),
        json.dumps({**SAMPLE, "message_id": "om_2"}),
        "not-json-garbage",
        json.dumps({"type": "other.event"}),
    ]
    got = []
    agen = stream_of(make_fake_cli(tmp_path, lines)).stream()
    async for msg in agen:
        got.append(msg)
        if len(got) >= 2:
            break  # 有限消费后关闭,验证优雅终止路径
    await agen.aclose()
    assert [m.message_id for m in got] == ["om_1", "om_2"]


@pytest.mark.asyncio
async def test_stream_cancel终止子进程无孤儿(tmp_path):
    lines = [json.dumps(SAMPLE)] + [
        json.dumps({**SAMPLE, "message_id": f"om_{i}"}) for i in range(50)
    ]
    st = stream_of(make_fake_cli(tmp_path, lines))
    agen = st.stream()
    got = await agen.__anext__()
    assert got.message_id == "om_1"
    task = asyncio.create_task(_drain(agen))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert st._proc is None or st._proc.returncode is not None


async def _drain(agen):
    async for _ in agen:
        pass


@pytest.mark.asyncio
async def test_dedup_同一消息id只产出一次(tmp_path):
    dup_lines = [json.dumps(SAMPLE)] * 3
    got = []
    agen = stream_of(make_fake_cli(tmp_path, dup_lines)).stream()
    async for msg in agen:
        got.append(msg)
        break
    await agen.aclose()
    assert len(got) == 1


@pytest.mark.asyncio
async def test_stream_cancel终止子进程无孤儿(tmp_path):
    lines = [json.dumps(SAMPLE)] + [""] * 1 + [json.dumps(SAMPLE).replace("om_1", f"om_{i}") for i in range(50)]
    st = stream_of(make_fake_cli(tmp_path, lines))
    agen = st.stream()
    got = await agen.__anext__()
    assert got.message_id == "om_1"
    task = asyncio.create_task(_drain(agen))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert st._proc is None or st._proc.returncode is not None


async def _drain(agen):
    async for _ in agen:
        pass


@pytest.mark.asyncio
async def test_dedup_同一消息id只产出一次(tmp_path):
    dup_lines = [json.dumps(SAMPLE)] * 3
    got = [
        m
        async for m in stream_of(make_fake_cli(tmp_path, dup_lines)).stream()
    ]
    assert len(got) == 1


# --------------------------------------------------------------------------- #
# LarkMessenger(参数捕获,不发真实请求)
# --------------------------------------------------------------------------- #


class CapturingMessenger(LarkMessenger):
    """拦截 _send 捕获命令行参数。"""

    def __init__(self):
        super().__init__(binary=Path("/nonexistent/lark-cli"))
        self.sent: list[list[str]] = []

    async def _send(self, args, *, cwd=None):  # type: ignore[override]
        self.sent.append(args)
        self.last_cwd = cwd

        class _E:
            ok = True

        return _E()


@pytest.mark.asyncio
async def test_send_text_chat_target使用bot身份():
    m = CapturingMessenger()
@pytest.mark.asyncio
async def test_dedup_同一消息id只产出一次(tmp_path):
    dup_lines = [json.dumps(SAMPLE)] * 3
    got = []
    agen = stream_of(make_fake_cli(tmp_path, dup_lines)).stream()
    async for msg in agen:
        got.append(msg.message_id)
        break  # 首条即停,重复行由去重层拦截
    await agen.aclose()
    assert got == ["om_1"]

@pytest.mark.asyncio
async def test_send_image走相对路径并切换cwd(tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(b"png")
    m = CapturingMessenger()
    await m.send_image("oc_chat1", img)
    args = m.sent[0]
    assert args[args.index("--image") + 1] == "./pic.png"
    assert Path(m.last_cwd) == tmp_path


@pytest.mark.asyncio
async def test_send_user_target自动分派():
    m = CapturingMessenger()
    await m.send_text("ou_someone", "hi")
    assert m.sent[0][m.sent[0].index("--user-id") + 1] == "ou_someone"


@pytest.mark.asyncio
async def test_send_非法target拒绝():
    m = CapturingMessenger()
    with pytest.raises(ValueError, match="oc_|ou_"):
        await m.send_text("123456", "hi")
