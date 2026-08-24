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

try:
    from astrbot_lark_kit import (
        Health,
        auth_status_from_dict,
        health_of,
    )
except ImportError:  # 打包分发时 kit 以子包形式随插件提供
    from ..astrbot_lark_kit import (  # type: ignore
        Health,
        auth_status_from_dict,
        health_of,
    )

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
            saved = Path(
                await self._require().download_media(token, output_path.parent)
            )
        except Exception:
            return None
        if not saved.is_file() or saved.stat().st_size == 0:
            return None
        if saved != output_path:
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                saved.replace(output_path)
            except OSError:
                return saved
        return output_path if output_path.is_file() else saved

    # ── 登录态 ──

    async def auth_status(self):
        """查询登录态,返回 kit 的 AuthStatus(便于复用健康判定)。"""
        obj = await self._require().auth_status()
        # 网关契约返回 dict(lark-cli auth status JSON);防御式兼容已解析形态
        if isinstance(obj, dict):
            return auth_status_from_dict(obj)
        return obj

    async def auth_health(self, *, warning_hours: float = 48.0) -> Health:
        status = await self.auth_status()
        return health_of(status, warning_hours=warning_hours)

    async def auth_login_start(self) -> dict | None:
        """发起设备授权,返回 {device_code, verification_url, expires_in};失败 None。"""
        obj = await self._require().auth_login_start()
        if not isinstance(obj, dict):
            return None
        if not obj.get("device_code") or not obj.get("verification_url"):
            return None
        return obj

    async def auth_login_finish(self, device_code: str) -> bool:
        """完成设备授权轮询(网关内部阻塞等待);成功 True。"""
        try:
            await self._require().auth_login_finish(device_code)
        except Exception:
            return False
        return True

    # ── 发送 ──

    async def send_text(self, target: str, text: str) -> bool:
        """以 bot 身份向 oc_/ou_ 会话发送文本;成功 True。"""
        try:
            await self._require().send_text(target, text)
        except Exception:
            return False
        return True


# 登录态异常时的管理员引导提示(授权闭环入口在会话指令与主动推送)
REAUTH_HINT = "管理员请在会话中发送 /qa_auth_login 发起扫码授权(或等待推送的授权链接)"
