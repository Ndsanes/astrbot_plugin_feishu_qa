"""astrbot_plugin_feishu_qa — 飞书 Q&A 领域问答机器人。

架构(spec):GatewayClient(lark_cli 平台网关薄客户端,含 auth keeper)→
Corpus → 确定性检索 → 直答(0 LLM)/ 主 Agent search_feishu_qa 工具。
飞书访问全部经 lark_cli 平台适配器的 gateway 转发;网关未就绪时降级为
本地语料继续服务。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools

from .adapter.gateway import DOC_FORMAT_XML, GatewayClient
from .answer.direct import DirectAnswer
from .answer.router import AnswerRouter
from .corpus.builder import build_manifest, diff_manifests
from .corpus.parser import parse_xml
from .learn.candidate import (
    build_learn_prompt,
    candidate_to_pending_record,
    find_duplicate,
    format_candidate_display,
    parse_candidate,
)
from .retrieval.scorer import Retriever
from .storage.snapshot import SnapshotStore

PLUGIN_NAME = "astrbot_plugin_feishu_qa"
DEFAULT_WIKI_URL = "https://my.feishu.cn/wiki/O9fcwP1PviPuOSkGBekc7B7xn4c"

# FAQ 引用规范:随 on_llm_request 注入的静态短文本(恒定内容不破坏提示词缓存)。
_FAQ_CITATION_GUIDANCE = (
    "\n[FAQ 引用规范] 知识库结果分两类,处理方式不同:"
    "(A)「【全家桶FAQ >」开头的条目是精选问答原文——命中时调用 qa_send_answer"
    "(entry_ids=条目标记里的 id,多条相关就逗号分隔一并传入),插件会把飞书文档"
    "章节直达链接列表发给用户;之后只做简短衔接或追问,不要复述正文。"
    "(B)其他来源(如 Cakewalk sonar 手册)没有可跳转的文档,直接依据知识块"
    "组织回答并翻译要点。"
    "统一要求:回答用到 FAQ 条目内容时,末尾另起一行注明"
    "📄 出处:《有福同享全家桶Q&A汇总》条目标题方括号里的章节路径;"
    "不要臆测知识块里「参考图N」「如图」指代的图片内容;"
    "不要把 [配图 qa_xxx] 标记原样输出给用户。"
)




@register(
    PLUGIN_NAME,
    "NDsans",
    "飞书 Q&A 文档驱动的领域问答机器人(高置信直答零 LLM)",
    "0.7.5",
    "https://github.com/Ndsanes/astrbot_plugin_feishu_qa",
)
class FeishuQaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_root = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.store = SnapshotStore(self.data_root)

        # 飞书访问全部经 lark_cli 平台适配器的 gateway 转发;resolver 每次操作
        # 前重新解析,平台适配器晚于本插件启动时自动恢复可用。
        self.adapter = GatewayClient(
            doc_ref=self._cfg("WIKI_URL") or DEFAULT_WIKI_URL,
            resolver=self._get_gateway,
        )

        self._entries: list = []
        self._retriever: Retriever | None = None
        self._router: AnswerRouter | None = None
        self._load_corpus()

        self._pending_learn: dict[str, dict] = {}  # user_id -> candidate
        self._sync_task: asyncio.Task | None = None

    def _get_gateway(self):
        """返回 lark_cli 平台适配器的网关对象;未就绪时返回 None(调用方优雅降级)。"""
        try:
            for p in self.context.platform_manager.get_insts():
                if p.meta().name == "lark_cli":
                    return getattr(p, "gateway", None)
        except Exception:
            pass
        return None


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
        from .corpus.model import QaEntry, QaImage

        entries = []
        for raw in manifest.get("entries", []):
            images = [QaImage(**img) for img in raw.get("images", [])]
            entries.append(QaEntry(**{**raw, "images": images}))
        return entries

    # ── 生命周期 ──

    async def initialize(self) -> None:
        if not self.adapter.available:
            logger.warning(
                "[FeishuQA] lark_cli 平台未加载,飞书拉取/授权不可用,本地语料继续服务"
            )
        if int(self._cfg("SYNC_INTERVAL_HOURS", 12)) > 0:
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def terminate(self) -> None:
        for task in (self._sync_task,):
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


    async def sync_once(self) -> dict:
        """抓取→解析→下载缺失图→原子提交→热替换检索器。"""
        doc = await self.adapter.fetch_doc(
            fmt=DOC_FORMAT_XML, detail="with-ids"
        )
        parsed = parse_xml(doc.content, source_revision=doc.revision_id)
        manifest = build_manifest(
            parsed, revision_id=doc.revision_id, document_id=doc.document_id
        )
        old = self.store.load()
        diff = diff_manifests(old, manifest)

        # 章节定位(块 ID)会随文档结构编辑整体重生:即使正文一字未改,
        # 也可能全部换新。快照必须无条件刷新,否则章节直达链接静默失效
        #(故不设 unchanged 快速路径;图片按文件存在性短路,代价可忽略)。
        old_locs = (
            {e["id"]: e.get("source_locator", "") for e in old["entries"]}
            if old
            else {}
        )
        relinked = [
            e
            for e in manifest["entries"]
            if old_locs and old_locs.get(e["id"]) != e.get("source_locator", "")
        ]

        # 下载缺失图片(已存在的按文件短路,单张失败不阻断)
        failures = 0
        redownloaded = 0
        for entry in manifest["entries"]:
            for img in entry["images"]:
                target = self.store.image_path(img["local_path"])
                if target.is_file() and target.stat().st_size > 0:
                    continue
                path = await self.adapter.download_media(img["file_token"], target)
                if path is None:
                    failures += 1
                else:
                    redownloaded += 1

        store = SnapshotStore(self.data_root)
        store.commit(manifest)
        self._load_corpus()
        if relinked:
            samples = ";".join(
                f"{e['id']}:{old_locs.get(e['id'], '?')}→{e['source_locator']}"
                for e in relinked[:3]
            )
            logger.warning(
                "[FeishuQA] 章节定位已刷新 %s 条(文档结构编辑会使旧块链接失效) 样例:%s",
                len(relinked),
                samples,
            )
        status = (
            "synced"
            if diff["changed"] or failures or redownloaded or relinked
            else "unchanged"
        )
        return {
            "status": status,
            "revision_id": doc.revision_id,
            "added": diff["added"],
            "updated": diff["updated"],
            "removed": diff["removed"],
            "image_failures": failures,
            "relinked": len(relinked),
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
        plan = router.route(
            question, group_id=group_id, umo=event.unified_msg_origin
        )
        if plan.kind == "denied":
            # 管理员可见诊断:便于排查群号/平台形态问题;普通用户保持零响应
            if self._is_admin(event):
                platform_name = ""
                with contextlib.suppress(Exception):
                    platform_name = str(event.get_platform_name() or "")
                umo = str(getattr(event, "unified_msg_origin", "") or "")
                yield event.plain_result(
                    f"[FeishuQA] 当前会话不在白名单,已忽略。"
                    f"platform={platform_name} group_id={group_id!r}\n"
                    f"如需启用本群,可将 UMO 加入 ENABLED_GROUPS: {umo!r}"
                )
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
        plan = router.route(
            text, group_id=event.get_group_id(), umo=event.unified_msg_origin
        )
        if plan.kind == "direct" and plan.direct:
            await self._send_direct(event, plan.direct)
            event.stop_event()
        # miss/medium:不回复、不阻断 → 主 Agent 正常接管(可调 search tool)

    @filter.llm_tool(name="search_feishu_qa")
    async def search_feishu_qa(self, event: AstrMessageEvent, query: str):
        """在飞书 Q&A 文档中搜索相关问答,返回原文片段。

        Args:
            query (str): 用户的实际问题或关键词
        """
        if self._retriever is None:
            return "search_feishu_qa: 语料未就绪"
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
        return json.dumps(payload, ensure_ascii=False)

    @filter.llm_tool(name="qa_entry_images")
    async def qa_entry_images(self, event: AstrMessageEvent, entry_id: str):
        """把指定 QA 条目的操作截图直接发送给用户。仅当检索结果带截图标记且
        用户需要查看截图时调用;发送成功后不要向用户复述发送过程。

        Args:
            entry_id (str): QA 条目 ID,形如 qa_xxxxxxxxxxxxxxxx,只能取自检索结果中的截图标记
        """
        import astrbot.api.message_components as Comp
        from astrbot.api.event import MessageEventResult

        if not re.fullmatch(r"qa_[0-9a-f]{16}", entry_id or ""):
            return "图片资源不存在:entry_id 格式非法"
        entry = next((e for e in self._entries if e.id == entry_id), None)
        if entry is None:
            return f"图片资源不存在:{entry_id} 不在当前语料中"
        if not entry.images:
            return "该条目没有配图"
        paths: list[str] = []
        for img in entry.images:
            path = self.store.image_path(
                img.local_path or f"images/{img.image_id}.png"
            )
            if path.is_file() and path.stat().st_size > 0:
                paths.append(str(path))
        paths = list(dict.fromkeys(paths))  # 去重保序(同图被条目重复引用时)
        if not paths:
            return "图片资源不存在:本地图片文件缺失"

        platform_name = ""
        with contextlib.suppress(Exception):
            platform_name = str(event.get_platform_name() or "")
        caption = f"【配图】{entry.raw_title}"
        result = None
        if self._supports_merged_forward(platform_name):
            try:
                uin = int(event.get_self_id() or 10000)
                content: list = [Comp.Plain(caption)]
                content.extend(Comp.Image.fromFileSystem(p) for p in paths)
                node = Comp.Node(uin=uin, name="Q&A 助手", content=content)
                result = MessageEventResult(chain=[node])
            except Exception as exc:
                logger.warning("[FeishuQA] 配图合并转发构建失败,回退普通消息: %s", exc)
                result = None
        if result is None:
            result = MessageEventResult().message(caption)
            for p in paths:
                result.file_image(p)
        # 返回 MessageEventResult:核心按 tool_direct_result 直发给用户并结束本轮
        # Agent;返回 str 则作为工具结果回传 LLM 继续。两条路径互斥,见执行器契约。
        return result

    @filter.llm_tool(name="qa_send_answer")
    async def qa_send_answer(self, event: AstrMessageEvent, entry_ids: str):
        """把与用户问题匹配的一个或多个 QA 条目整理成飞书文档章节直达链接列表发送给用户。

        当【全家桶FAQ】条目命中问题时优先使用;链接列表发出后只需简短衔接,
        不要复述条目内容。

        Args:
            entry_ids (str): QA 条目 ID,形如 qa_xxxxxxxxxxxxxxxx,多个用英文逗号分隔,取自条目标记
        """
        ids = [s for s in re.split(r"[,，、;\s]+", entry_ids or "") if s.strip()]
        entries, skipped, seen = [], [], set()
        for raw in ids:
            if not re.fullmatch(r"qa_[0-9a-f]{16}", raw) or raw in seen:
                skipped.append(raw)
                continue
            seen.add(raw)
            entry = next((e for e in self._entries if e.id == raw), None)
            if entry is None:
                skipped.append(raw)
            else:
                entries.append(entry)
        if not entries:
            bad = ",".join(skipped) if skipped else "未提供有效条目"
            return f"发送失败:没有可投递的条目({bad})"

        # 实测(2026-08-25):QQ 官方 markdown(msg_type=2)渲染会剥掉 href 的
        # #fragment——裸文本 URL 反而走客户端识别路径完整保留锚点。
        # 故所有平台统一用"标题行 + 👉 裸链接",不使用 [标题](url) 形态。
        lines = [f"📖 命中 {len(entries)} 条官方整理的解答(点链接直达文档对应章节):"]
        for i, entry in enumerate(entries, 1):
            lines.append(f"\n{i}、【{entry.raw_title}】")
            lines.append(f"👉 {self._wiki_block_url(entry)}")
        lines.append("\n📄 来源:《有福同享全家桶Q&A汇总》(肖闻 Xiaowenn 整理)")
        direct = DirectAnswer(
            text="\n".join(lines), image_paths=[], entry_id=entries[0].id
        )
        # 单次 event.send 投出整块;经此而非 set_result:Agent 循环保持存活。
        await self._send_direct(event, direct)
        note = f";已跳过:{','.join(skipped)}" if skipped else ""
        titles = "》《".join(e.raw_title for e in entries)
        return (
            f"已投递{len(entries)}条章节直达链接:《{titles}》{note};"
            "请勿复述条目正文,链接里含图文步骤。"
        )

    def _wiki_block_url(self, entry) -> str:
        """构造飞书文档锚点直达链接(WIKI_URL#block_id);无定位时退回整篇。"""
        base = str(self._cfg("WIKI_URL", "") or "").strip().rstrip("/")
        if not base:
            return "(未配置 WIKI_URL)"
        if entry.source_locator:
            return f"{base}#{entry.source_locator}"
        return base

    @filter.on_llm_request()
    async def add_faq_citation_guidance(self, event: AstrMessageEvent, req) -> None:
        """注入 FAQ 引用规范与命中预判。

        系统提示词只追加恒定文本(前缀缓存安全);随消息变化的命中指令
        注入 req.prompt 尾部——尾部本就逐轮变化,不破坏缓存(AGENTS.md
        检索内容不进系统提示词之约束)。
        """
        req.system_prompt = (req.system_prompt or "") + _FAQ_CITATION_GUIDANCE

        query = self._strip_command(event.message_str or "")
        if not query or self._retriever is None:
            return
        results = self._retriever.search(query, top_k=2)
        hits = [r for r in results if r.confidence != "LOW"]
        if not hits:
            return
        ids = ",".join(r.entry.id for r in hits)
        titles = "；".join(r.entry.raw_title for r in hits)
        req.prompt = (
            (req.prompt or "")
            + f"\n[语料匹配] 本条消息已命中精选问答:{titles}(entry_ids={ids})。"
            "回答前必须先调用 qa_send_answer(entry_ids=上述值)发送章节直达链接,"
            "再视需要简短补充;不要转述条目正文。"
        )

    # ── 管理指令 ──

    @filter.command("qa_status")
    async def qa_status(self, event: AstrMessageEvent):
        """查看同步与网关接入状态(管理员)。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可用")
            return
        manifest = self.store.load() or {}
        if self.adapter.available:
            gateway_line = "lark_cli 网关: 已接入"
        else:
            gateway_line = "lark_cli 网关: 未接入(飞书拉取不可用)"

        def _fmt_revision(value: object) -> str:
            if isinstance(value, str):
                return value
            return "未知" if value == -1 else str(value)

        lines = [
            gateway_line,
            f"revision: {_fmt_revision(manifest.get('revision_id', '无'))}",
            f"QA 条目: {manifest.get('entry_count', 0)}",
            f"图片数量: {manifest.get('image_count', 0)}",
            f"最后构建: {manifest.get('built_at', '无')}",
        ]
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


    @filter.command("learn")
    async def learn(self, event: AstrMessageEvent):
        """/learn:分析最近群聊,提取候选 QA(管理员;确认后才生效)。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可用")
            return

        text = self._strip_command(event.message_str)
        user_id = str(event.get_sender_id())

        if text.lower() in ("ok", "确认", "yes"):
            pending = self._pending_learn.pop(user_id, None)
            if not pending:
                yield event.plain_result("当前没有待确认的候选。先运行 /learn 分析群聊。")
                return
            record = candidate_to_pending_record(
                pending,
                group_id=event.get_group_id() or "",
                approved_by=user_id,
            )
            pending_path = self.data_root / "pending_learn.json"
            # 落盘先行:写回失败/未配置时候选都不丢

            wb_url = str(self._cfg("LEARNING_WRITEBACK_DOC_URL") or "").strip()
            if not wb_url:
                records = self._read_pending_records(pending_path)
                records.append(record)
                self._write_pending_records(pending_path, records)
                yield event.plain_result(
                    "已收录为待写入候选(未配置写回目标文档,"
                    "当前由管理员人工同步)。"
                )
                return

            yield event.plain_result(await self._writeback_record(record, wb_url))
            return

        if text.lower() in ("no", "取消", "放弃"):
            dropped = self._pending_learn.pop(user_id, None)
            yield event.plain_result("已放弃。" if dropped else "没有待确认的候选。")
            return

        # 1) 取最近群聊历史(aiocqhttp 专属能力,失败即告知)
        history_texts = await self._fetch_recent_history(event)
        if history_texts is None:
            yield event.plain_result(
                "当前平台不支持读取群历史消息,/learn 仅在 aiocqhttp 下可用。"
            )
            return
        if not history_texts:
            yield event.plain_result("最近没有可分析的消息。")
            return

        # 2) LLM 提取(唯一一次调用,spec §39)
        prompt = build_learn_prompt(history_texts)
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id, prompt=prompt
            )
            raw = resp.completion_text
        except Exception as exc:
            yield event.plain_result(f"LLM 调用失败: {exc}")
            return

        # 3) 严格解析 + 查重
        candidate = parse_candidate(raw or "")
        if candidate is None:
            yield event.plain_result("最近的群聊中没有值得加入 FAQ 的新问答。")
            return

        duplicate = find_duplicate(candidate, self._retriever) if self._retriever else None
        if duplicate is not None:
            yield event.plain_result(
                f"与既有条目高度相似,不新增:\n【{duplicate.raw_title}】\n"
                f"如需更新该条目内容,请直接编辑飞书文档。"
            )
            return

        self._pending_learn[user_id] = candidate
        yield event.plain_result(format_candidate_display(candidate))

    async def _fetch_recent_history(self, event: AstrMessageEvent) -> list[str] | None:
        """读最近 N 条群消息文本;平台不支持返回 None。"""
        limit = int(self._cfg("LEARN_CONTEXT_MESSAGES", 50))
        try:
            bot = getattr(event, "bot", None)
            if bot is None:
                client = self._get_aiocqhttp_client(event)
            else:
                client = bot
            if client is None:
                return None
            result = await client.api.call_action(
                "get_group_msg_history",
                group_id=int(event.get_group_id()),
                count=limit,
            )
            messages = result.get("messages", [])
            texts = [
                str(m.get("message", "")).strip()
                for m in messages
                if isinstance(m, dict) and m.get("message")
            ]
            return [t for t in texts if t][-limit:]
        except Exception as exc:
            logger.debug("[FeishuQA] 读取群历史失败: %s", exc)
            return None

    # ── /learn 写回(pending_learn.json 读写 + 副本文档追加)──

    def _read_pending_records(self, path: Path) -> list[dict]:
        """读取待写入候选列表;文件缺失/损坏返回空列表。"""
        if not Path(path).is_file():
            return []
        try:
            data = json.loads(Path(path).read_text())
        except (json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []

    def _write_pending_records(self, path: Path, records: list[dict]) -> None:
        """原子语义落盘:先写临时文件再替换,避免中断产生半截 JSON。"""
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1))
        tmp.replace(path)

    async def _writeback_record(self, record: dict, wb_url: str) -> str:
        """/learn ok 确认后的写回流程(只允许副本文档,配置项显式给出)。

        原子性:本地构造完整 block → 查重 → 单次 append;
        任一步失败候选保留在 pending_learn.json,不标记已学习。
        """
        from .learn.writeback import (
            build_entry_markdown,
            derive_record_key,
            duplicate_guard_result,
            extract_h3_titles,
        )

        pending_path = self.data_root / "pending_learn.json"
        records = self._read_pending_records(pending_path)
        record_key = derive_record_key(record)

        # 写回目标用独立客户端(同一网关,不同文档引用)
        wb_client = GatewayClient(doc_ref=wb_url, resolver=self._get_gateway)
        try:
            doc = await wb_client.fetch_doc(fmt="markdown")
            titles = extract_h3_titles(doc.content)
            synced_keys = {
                str(r.get("record_key"))
                for r in records
                if r.get("status") == "synced" and r.get("record_key")
            }
            if (
                duplicate_guard_result(record, titles, already_synced_keys=synced_keys)
                == "already_exists"
            ):
                record.update(
                    {"status": "duplicate", "record_key": record_key}
                )
                records.append(record)
                self._write_pending_records(pending_path, records)
                return "该问答已存在于目标文档中(already exists),未重复写入。"

            block = build_entry_markdown(record)
            revision = await wb_client.append_doc_content(block)
        except Exception as exc:
            # 失败降级:候选保留,交管理员人工处理
            logger.warning("[FeishuQA] /learn 写回失败(候选保留): %s", exc)
            records.append({**record, "status": "failed"})
            self._write_pending_records(pending_path, records)
            return f"写回失败,候选已保留待人工处理:{exc}"

        record.update({"status": "synced", "record_key": record_key})
        if revision >= 0:
            record["revision"] = revision
        records.append(record)
        self._write_pending_records(pending_path, records)
        rev_note = f"(revision {revision})" if revision >= 0 else ""
        return f"已写入副本文档{rev_note}。同步验证通过后即可检索到新条目。"

    def _get_aiocqhttp_client(self, event: AstrMessageEvent):
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            return getattr(platform, "client", None)
        except Exception:
            return None

    # ── 发送辅助 ──

    async def _send_direct(self, event: AstrMessageEvent, direct) -> None:
        """直答发送:支持合并转发的平台走 Node 转发,其余发普通多图消息。

        能力探测见 _supports_merged_forward;构建失败仍会回退普通消息。
        """
        import astrbot.api.message_components as Comp
        from astrbot.api.event import MessageChain

        images = direct.image_paths if self._cfg("ATTACH_IMAGES", True) else []
        platform_name = ""
        with contextlib.suppress(Exception):
            platform_name = str(event.get_platform_name() or "")

        chain = None
        if self._supports_merged_forward(platform_name):
            try:
                uin = int(event.get_self_id() or 10000)
                content = [Comp.Plain(direct.text)]
                for path in images:
                    content.append(Comp.Image.fromFileSystem(path))
                node = Comp.Node(uin=uin, name="Q&A 助手", content=content)
                chain = MessageChain(chain=[node])
            except Exception as exc:
                logger.warning("[FeishuQA] 合并转发构建失败,回退普通消息: %s", exc)
                chain = None
        if chain is None:
            chain = MessageChain()
            chain.message(direct.text)
            for path in images:
                chain.file_image(path)

        try:
            await event.send(chain)
        except Exception as exc:
            # 平台对图片/富组件支持不全时,至少把答案文本送出去
            logger.warning("[FeishuQA] 发送失败,回退纯文本: %s", exc)
            fallback = MessageChain()
            fallback.message(direct.text)
            await event.send(fallback)

    @staticmethod
    def _supports_merged_forward(platform_name: str) -> bool:
        """合并转发能力探测(VERIFICATION_REPORT 已知限制 #4 的收口)。

        - qq_official 官方网关不提供 OneBot 语义的 Node.uin 合并转发,
          明确返回 False,走普通多图消息;
        - 其余平台默认尝试构建,构建失败由调用方回退普通消息。
        未来接入新平台时只需在此登记实测结论,不改发送逻辑。
        """
        return platform_name != "qq_official"

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

