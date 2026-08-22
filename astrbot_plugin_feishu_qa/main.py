"""astrbot_plugin_feishu_qa — 飞书 Q&A 领域问答机器人。

架构(spec):LarkAdapter(含 auth keeper)→ Corpus → 确定性检索 →
直答(0 LLM)/ 主 Agent search_feishu_qa 工具。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools

PLUGIN_NAME = "astrbot_plugin_feishu_qa"
DEFAULT_WIKI_URL = "https://my.feishu.cn/wiki/O9fcwP1PviPuOSkGBekc7B7xn4c"

try:
    from astrbot_plugin_feishu_qa.adapter.auth import AuthKeeper
    from astrbot_plugin_feishu_qa.adapter.lark import LarkAdapter
    from astrbot_plugin_feishu_qa.answer.router import AnswerRouter
    from astrbot_plugin_feishu_qa.corpus.builder import build_manifest, diff_manifests
    from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
    from astrbot_plugin_feishu_qa.retrieval.scorer import Retriever
    from astrbot_plugin_feishu_qa.storage.snapshot import SnapshotStore
except ImportError:  # pragma: no cover - 直接以目录加载时的兜底
    from adapter.auth import AuthKeeper  # type: ignore
    from adapter.lark import LarkAdapter  # type: ignore
    from answer.router import AnswerRouter  # type: ignore
    from corpus.builder import build_manifest, diff_manifests  # type: ignore
    from corpus.parser import parse_xml  # type: ignore
    from retrieval.scorer import Retriever  # type: ignore
    from storage.snapshot import SnapshotStore  # type: ignore


@register(
    PLUGIN_NAME,
    "NDsans",
    "飞书 Q&A 文档驱动的领域问答机器人(高置信直答零 LLM)",
    "0.1.0",
    "https://github.com/Ndsanes/astrbot_plugin_feishu_qa",
)
class FeishuQaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_root = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.store = SnapshotStore(self.data_root)

        self.adapter = LarkAdapter(doc_ref=self._cfg("WIKI_URL") or DEFAULT_WIKI_URL)
        self.keeper = AuthKeeper(
            self.adapter, warning_hours=float(self._cfg("AUTH_WARNING_HOURS", 48))
        )

        self._entries: list = []
        self._retriever: Retriever | None = None
        self._router: AnswerRouter | None = None
        self._load_corpus()

        self._sync_task: asyncio.Task | None = None
        self._auth_task: asyncio.Task | None = None

    # ── 配置 ──

    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default)
        return default if value is None else value

    # ── 语料装载 ──

    def _load_corpus(self) -> bool:
        """从本地快照装载检索器;失败保留内存旧值(spec §16/§47)。"""
        manifest = self.store.load()
        if not manifest:
            logger.warning("[FeishuQA] 本地无有效语料快照,请先 /qa_sync")
            return False
        try:
            entries = [
                e for e in self._manifest_to_entries(manifest) if e.body or e.images
            ]
            self._entries = entries
            self._retriever = Retriever(
                entries,
                high_threshold=float(self._cfg("HIGH_CONFIDENCE_THRESHOLD", 9.0)),
                medium_threshold=float(self._cfg("MEDIUM_CONFIDENCE_THRESHOLD", 3.0)),
            )
            self._router = AnswerRouter(
                self._retriever,
                store=self.store,
                enabled_groups=list(self._cfg("ENABLED_GROUPS", [])),
                max_images=int(self._cfg("MAX_IMAGES", 3)),
            )
            logger.info(
                "[FeishuQA] 语料已装载 revision=%s entries=%d",
                manifest.get("revision_id"),
                len(entries),
            )
            return True
        except Exception as exc:
            logger.error("[FeishuQA] 语料装载失败: %s", exc)
            return False

    @staticmethod
    def _manifest_to_entries(manifest: dict) -> list:
        from astrbot_plugin_feishu_qa.corpus.model import QaEntry, QaImage

        entries = []
        for raw in manifest.get("entries", []):
            images = [QaImage(**img) for img in raw.get("images", [])]
            entries.append(QaEntry(**{**raw, "images": images}))
        return entries

    # ── 生命周期 ──

    async def initialize(self) -> None:
        if int(self._cfg("SYNC_INTERVAL_HOURS", 12)) > 0:
            self._sync_task = asyncio.create_task(self._sync_loop())
        if int(self._cfg("AUTH_CHECK_HOURS", 12)) > 0:
            self._auth_task = asyncio.create_task(self._auth_loop())

    async def terminate(self) -> None:
        for task in (self._sync_task, self._auth_task):
            if task:
                task.cancel()

    # ── 后台任务 ──

    async def _sync_loop(self) -> None:
        interval = int(self._cfg("SYNC_INTERVAL_HOURS", 12)) * 3600
        await asyncio.sleep(30)  # 启动后稍等平台就绪
        while True:
            try:
                result = await self.sync_once()
                logger.info("[FeishuQA] 定时同步: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[FeishuQA] 同步失败(旧语料继续服务): %s", exc)
            await asyncio.sleep(interval)

    async def _auth_loop(self) -> None:
        interval = int(self._cfg("AUTH_CHECK_HOURS", 12)) * 3600

        async def notify(message: str) -> None:
            # v1:记录日志;/qa_status 可查。推送渠道后续接入 bot 身份 IM。
            logger.warning("[FeishuQA][AUTH] %s", message)

        while True:
            try:
                health, notified = await self.keeper.check(notify)
                if notified:
                    logger.info("[FeishuQA] 已发送登录态提醒: %s", health.value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[FeishuQA] auth 检查异常: %s", exc)
            await asyncio.sleep(interval)

    async def sync_once(self) -> dict:
        """抓取→解析→下载缺失图→原子提交→热替换检索器。"""
        doc = await self.adapter.fetch_doc(fmt="xml")
        parsed = parse_xml(doc.content, source_revision=doc.revision_id)
        manifest = build_manifest(
            parsed, revision_id=doc.revision_id, document_id=doc.document_id
        )
        old = self.store.load()
        diff = diff_manifests(old, manifest)

        if old and not diff["changed"] and old.get("revision_id") == doc.revision_id:
            return {"status": "unchanged", "revision_id": doc.revision_id}

        # 下载缺失图片(单张失败不阻断)
        failures = 0
        for entry in manifest["entries"]:
            for img in entry["images"]:
                target = self.store.image_path(img["local_path"])
                if target.is_file() and target.stat().st_size > 0:
                    continue
                path = await self.adapter.download_media(img["file_token"], target)
                if path is None:
                    failures += 1

        store = SnapshotStore(self.data_root)
        store.commit(manifest)
        self._load_corpus()
        return {
            "status": "synced",
            "revision_id": doc.revision_id,
            "added": diff["added"],
            "updated": diff["updated"],
            "removed": diff["removed"],
            "image_failures": failures,
        }

    # ── 指令 ──

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admins = list(self._cfg("ADMIN_USERS", []))
        return str(event.get_sender_id()) in {str(a) for a in admins} or event.is_admin()

    @filter.command("问", alias={"qa", "Q&A"})
    async def ask(self, event: AstrMessageEvent):
        """/问 <问题>:确定性直答入口(高置信不调用 LLM)。"""
        question = self._strip_command(event.message_str)
        group_id = event.get_group_id()
        router = self._router
        if router is None:
            yield event.plain_result("语料尚未就绪,请联系管理员执行 /qa_sync")
            return
        plan = router.route(question, group_id=group_id)
        if plan.kind == "denied":
            event.stop_event()
            return
        if plan.kind == "direct" and plan.direct:
            await self._send_direct(event, plan.direct)
            event.stop_event()  # 高置信已回答,阻断 LLM 流水线
            return
        yield event.plain_result(
            "没有在 Q&A 文档里找到足够相关的问题。\n"
            "可以换个说法试试,或直接描述具体报错信息。"
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """@机器人 自然语言入口:高置信直接拦截,其余放行给主 Agent。"""
        if not event.is_at_or_wake_command:
            return
        text = self._strip_wake(event.message_str)
        if not text or text.startswith(("/问", "/qa")):
            return  # 指令路径交给 command handler
        router = self._router
        if router is None:
            return
        plan = router.route(text, group_id=event.get_group_id())
        if plan.kind == "direct" and plan.direct:
            await self._send_direct(event, plan.direct)
            event.stop_event()
        # miss/medium:不回复、不阻断 → 主 Agent 正常接管(可调 search tool)

    @filter.llm_tool(name="search_feishu_qa")
    async def search_feishu_qa(self, event: AstrMessageEvent, query: str):
        """在飞书 Q&A 文档中搜索相关问答,返回原文片段。

        Args:
            query: 用户的实际问题或关键词
        """
        if self._retriever is None:
            yield event.plain_result("search_feishu_qa: 语料未就绪")
            return
        results = self._retriever.search(query, top_k=3)
        if not results or results[0].confidence == "LOW":
            payload = {"matches": [], "note": "没有找到足够相关的 QA"}
        else:
            payload = {
                "matches": [
                    {
                        "title": r.entry.raw_title,
                        "section": " > ".join(r.entry.section_path),
                        "symptoms": r.entry.symptom_tags,
                        "body": r.entry.body[:1500],
                        "images_count": len(r.entry.images),
                        "confidence": round(r.score, 2),
                    }
                    for r in results
                    if r.confidence != "LOW"
                ],
                "note": "只允许依据以上原文回答;不足时明确告知用户资料中没有。",
            }
        yield event.plain_result(json.dumps(payload, ensure_ascii=False))

    # ── 管理指令 ──

    @filter.command("qa_status")
    async def qa_status(self, event: AstrMessageEvent):
        """查看同步与登录态状态(管理员)。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可用")
            return
        manifest = self.store.load() or {}
        lines = [
            f"revision: {manifest.get('revision_id', '无')}",
            f"QA 条目: {manifest.get('entry_count', 0)}",
            f"图片数量: {manifest.get('image_count', 0)}",
            f"最后构建: {manifest.get('built_at', '无')}",
        ]
        try:
            status = await self.adapter.auth_status()
            token = status.user.token_status or "未知"
            lines.append(f"user 登录态: {token} ({status.user_display})")
            lines.append(f"bot 身份: {'可用' if status.bot_available else '不可用'}")
        except Exception as exc:
            lines.append(f"登录态检查失败: {exc}")
        yield event.plain_result("\n".join(lines))

    @filter.command("qa_sync")
    async def qa_sync(self, event: AstrMessageEvent):
        """手动触发一次同步(管理员)。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可用")
            return
        yield event.plain_result("开始同步飞书文档…")
        try:
            result = await self.sync_once()
            yield event.plain_result(f"同步完成: {result}")
        except Exception as exc:
            yield event.plain_result(f"同步失败(旧语料继续服务): {exc}")

    @filter.command("qa_reload")
    async def qa_reload(self, event: AstrMessageEvent):
        """重新加载本地语料(管理员)。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可用")
            return
        ok = self._load_corpus()
        yield event.plain_result("语料已重新加载" if ok else "语料加载失败")

    # ── 发送辅助 ──

    async def _send_direct(self, event: AstrMessageEvent, direct) -> None:
        """优先 OneBot 合并转发;失败回退普通消息(spec §23)。"""
        import astrbot.api.message_components as Comp
        from astrbot.api.event import MessageChain

        images = direct.image_paths if self._cfg("ATTACH_IMAGES", True) else []
        try:
            uin = int(event.get_self_id() or 10000)
            content = [Comp.Plain(direct.text)]
            for path in images:
                content.append(Comp.Image.fromFileSystem(path))
            node = Comp.Node(uin=uin, name="Q&A 助手", content=content)
            chain = MessageChain(chain=[node])
        except Exception as exc:
            logger.warning("[FeishuQA] 合并转发构建失败,回退普通消息: %s", exc)
            chain = MessageChain()
            chain.message(direct.text)
            for path in images:
                chain.file_image(path)
        await event.send(chain)

    # ── 文本工具 ──

    @staticmethod
    def _strip_command(message_str: str) -> str:
        text = (message_str or "").strip()
        for prefix in ("/问", "问:", "问:", "问 "):
            if text.startswith(prefix):
                return text[len(prefix) :].strip()
        return text

    @staticmethod
    def _strip_wake(message_str: str) -> str:
        return (message_str or "").strip()

