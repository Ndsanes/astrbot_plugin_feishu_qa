"""GatewayClient — lark_cli 平台网关的薄客户端。

本插件不再自管 lark-cli(无子进程、无凭据、无登录态目录);所有飞书访问
转发给 astrbot_plugin_lark_cli_platform 平台适配器暴露的 ``gateway`` 对象
(LarkGateway)。网关实例通过注入的 resolver 延迟解析——平台适配器可能晚于
本插件启动,每次操作前重新查找,未就绪时抛 :class:`GatewayUnavailableError`,
由调用方降级(本地语料继续服务)。

网关方法签名见契约文档(local://lark-gateway-contract.md);这里按鸭子类型
调用,不做 isinstance 检查,也不 import 网关内部符号。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

DOC_FORMAT_XML = "xml"
DOC_FORMAT_MARKDOWN = "markdown"


class GatewayUnavailableError(Exception):
    """lark_cli 平台适配器未加载或其 gateway 尚未就绪。"""


@dataclass
class DocContent:
    """fetch_doc 的规范化结果。

    网关 fetch_doc 只返回正文文本,document_id/revision_id 不再可得,
    以空串/-1 占位;同步的"未变化"判定退化为内容 diff(diff_manifests)。
    """

    content: str
    document_id: str = ""
    revision_id: int = -1


class GatewayClient:
    """对网关公开方法的薄封装,保持原 LarkAdapter 的对外接口形态。

    resolver:每次调用时执行的 ``() -> gateway | None``;返回 None 表示
    网关尚未就绪。不做缓存,天然支持适配器晚启动的场景。
    """

    def __init__(self, *, doc_ref: str, resolver: Callable[[], object | None]) -> None:
        self.doc_ref = doc_ref
        self._resolver = resolver

    # ── 解析 ──

    @property
    def available(self) -> bool:
        """网关当前是否就绪(供 /qa_status 展示与测试断言)。"""
        try:
            return self._resolver() is not None
        except Exception:
            return False

    def _require(self):
        gateway = self._resolver()
        if gateway is None:
            raise GatewayUnavailableError("lark_cli 平台网关未就绪")
        return gateway

    # ── 文档 ──

    async def fetch_doc(self, *, fmt: str = DOC_FORMAT_MARKDOWN) -> DocContent:
        """抓取整份文档正文(fmt: markdown/xml)。"""
        text = await self._require().fetch_doc(self.doc_ref, fmt)
        return DocContent(content=str(text or ""))

    async def append_doc_content(self, content_markdown: str) -> int:
        """向文档末尾追加一段 markdown(/learn 写回),返回新 revision(-1 未知)。"""
        revision = await self._require().append_doc(self.doc_ref, content_markdown)
        try:
            return int(revision)
        except (TypeError, ValueError):
            return -1

    # ── 媒体 ──

    async def download_media(
        self,
        token: str,
        output_path: Path,
        *,
        overwrite: bool = False,
    ) -> Path | None:
        """下载文档媒体到本地指定路径。

        已存在且非空时默认跳过(幂等)。失败返回 None 不抛异常——
        单张图片失败不得阻断 corpus 构建(spec §12)。
        网关按 dest_dir 落盘并自行命名,这里统一搬移到 output_path,
        保证与 manifest 的 local_path 约定一致。
        """
        output_path = Path(output_path)
        if not overwrite and output_path.is_file() and output_path.stat().st_size > 0:
            return output_path
        try:
            saved_path = await self._require().download_media(
                token, output_path.parent
            )
        except Exception as exc:
            # 单张失败不阻断,但必须留痕(含堆栈)——否则缺图自愈循环会静默空转
            logger.warning(
                "[FeishuQA] media-download 调用异常 token=%s: %s",
                token,
                exc,
                exc_info=True,
            )
            return None
        if saved_path is None:
            # 网关契约:失败返回 None(不抛异常);底层 CLI 报错由平台侧日志承载
            logger.warning("[FeishuQA] media-download 失败(网关返回 None) token=%s", token)
            return None
        saved = Path(saved_path)
        if not saved.is_file() or saved.stat().st_size == 0:
            return None
        if saved != output_path:
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                saved.replace(output_path)
            except OSError:
                return saved
        return output_path if output_path.is_file() else saved

