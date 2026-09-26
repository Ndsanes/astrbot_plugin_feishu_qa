"""GatewayClient 测试(spec §57)。

全部通过注入假网关对象驱动(记录调用的方法与参数),无子进程、无网络依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from astrbot_plugin_feishu_qa.adapter.gateway import (
    DOC_FORMAT_XML,
    DocContent,
    GatewayClient,
    GatewayUnavailableError,
)


@dataclass
class FakeGateway:
    """假网关:记录调用面,可编程返回值/异常。"""

    fetch_results: dict[str, str] = field(default_factory=dict)
    download_target: Path | None = None
    append_revision: int = 42
    send_ok: bool = True

    calls: list[tuple] = field(default_factory=list)

    async def fetch_doc(
        self, doc_ref: str, fmt: str = "markdown", detail: str = "simple"
    ) -> str:
        self.calls.append(("fetch_doc", doc_ref, fmt, detail))
        if doc_ref not in self.fetch_results:
            raise RuntimeError(f"no such doc: {doc_ref}")
        return self.fetch_results[doc_ref]

    async def append_doc(self, doc_ref: str, content_markdown: str) -> int:
        self.calls.append(("append_doc", doc_ref, content_markdown))
        return self.append_revision

    async def download_media(self, file_token: str, dest_dir: Path) -> Path:
        self.calls.append(("download_media", file_token, dest_dir))
        if self.download_target is not None:
            return self.download_target
        # 模拟网关按 dest_dir 自行命名的落盘行为
        out = Path(dest_dir) / f"{file_token}.bin"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"PNGDATA")
        return out

    async def send_text(self, target: str, text: str) -> None:
        self.calls.append(("send_text", target, text))
        if not self.send_ok:
            raise RuntimeError("send failed")


def make_client(gateway: FakeGateway | None, doc_ref: str = "wiki/abc") -> GatewayClient:
    return GatewayClient(doc_ref=doc_ref, resolver=lambda: gateway)


class TestAvailability:
    def test_available_when_gateway_present(self) -> None:
        assert make_client(FakeGateway()).available is True

    def test_unavailable_when_none(self) -> None:
        client = make_client(None)
        assert client.available is False
        assert client._resolver() is None

    async def test_operations_raise_when_unresolved(self) -> None:
        client = make_client(None)
        with pytest.raises(GatewayUnavailableError):
            await client.fetch_doc()
        with pytest.raises(GatewayUnavailableError):
            await client.append_doc_content("hi")
        # download_media 的契约:任何失败都归一为 None(不阻断 corpus 构建)
        assert await client.download_media("tok", Path("/tmp/x.png")) is None


class TestFetchDoc:
    async def test_forwards_doc_ref_and_fmt(self) -> None:
        gw = FakeGateway(fetch_results={"wiki/abc": "# 标题\n正文"})
        client = make_client(gw)

        doc = await client.fetch_doc()

        assert ("fetch_doc", "wiki/abc", "markdown", "simple") in gw.calls
        assert isinstance(doc, DocContent)
        assert doc.content == "# 标题\n正文"
        # 网关只回文本:revision/document_id 以 -1/"" 占位
        assert doc.revision_id == -1 and doc.document_id == ""

    async def test_xml_fmt_forwarded(self) -> None:
        gw = FakeGateway(fetch_results={"w": "<doc/>"})
        client = make_client(gw, doc_ref="w")

        await client.fetch_doc(fmt=DOC_FORMAT_XML)

        assert ("fetch_doc", "w", "xml", "simple") in gw.calls


class TestDownloadMedia:
    async def test_moves_into_requested_path(self, tmp_path: Path) -> None:
        saved = tmp_path / "saved-by-gateway.bin"
        saved.write_text("PNGDATA")
        target = tmp_path / "images" / "img_01.png"
        gw = FakeGateway(download_target=saved)
        client = make_client(gw)

        result = await client.download_media("tok123", target)

        assert result == target and target.read_text() == "PNGDATA"
        assert not saved.exists(), "网关产物应搬移到约定路径"

    async def test_skip_existing_no_gateway_call(self, tmp_path: Path) -> None:
        out = tmp_path / "out.png"
        out.write_text("OLD")
        gw = FakeGateway(download_target=tmp_path / "never.png")
        client = make_client(gw)

        result = await client.download_media("tok", out)

        assert result == out and result.read_text() == "OLD"
        assert gw.calls == [], "已存在文件不应触发网关调用"

    async def test_failure_returns_none(self, tmp_path: Path) -> None:
        @dataclass
        class BrokenGateway(FakeGateway):
            async def download_media(self, file_token: str, dest_dir: Path) -> Path:
                raise RuntimeError("download failed")

        client = make_client(BrokenGateway())
        assert await client.download_media("tok", tmp_path / "never.png") is None


class TestAppendDoc:
    async def test_forwards_content_returns_revision(self) -> None:
        gw = FakeGateway(append_revision=8301)
        client = make_client(gw, doc_ref="wiki/wb")

        revision = await client.append_doc_content("### 标题\n\n正文\n")

        assert revision == 8301
        assert ("append_doc", "wiki/wb", "### 标题\n\n正文\n") in gw.calls
