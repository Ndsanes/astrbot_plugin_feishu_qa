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

from .adapter.auth import AuthKeeper
from .adapter.lark import LarkAdapter
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

        # 登录态落盘:插件数据目录下的 lark_cli_home(随 data 卷持久化)
        self.adapter = LarkAdapter(
            doc_ref=self._cfg("WIKI_URL") or DEFAULT_WIKI_URL,
            state_home=self.data_root / "lark_cli_home",
        )
        logger.info("[FeishuQA] %s", self.adapter.cli_diagnostics())
        self.keeper = AuthKeeper(
            self.adapter, warning_hours=float(self._cfg("AUTH_WARNING_HOURS", 48))
        )

        self._entries: list = []
        self._retriever: Retriever | None = None
        self._router: AnswerRouter | None = None
        self._load_corpus()

        self._pending_learn: dict[str, dict] = {}  # user_id -> candidate
        self._pending_learn: dict[str, dict] = {}
        self._sync_task: asyncio.Task | None = None
        self._auth_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None

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

    async def _ensure_cli_configured(self) -> str:
        """确保 lark-cli 已完成应用配置;凭据来自 FEISHU_APP_ID/SECRET。

        Returns:
            "" 表示已配置或配置成功;否则返回失败原因。
        """
        try:
            if await self.adapter.is_configured():
                return ""
        except Exception:
            pass
        app_id = str(self._cfg("FEISHU_APP_ID", "") or "")
        app_secret = str(self._cfg("FEISHU_APP_SECRET", "") or "")
        if not app_id or not app_secret:
            return (
                "lark-cli 未完成应用配置;请在插件配置里填写 "
                "FEISHU_APP_ID / FEISHU_APP_SECRET 后重载"
            )
        try:
            await self.adapter.configure_app(app_id, app_secret)
        except Exception as exc:
            return f"lark-cli config init 失败: {exc}"
        logger.info("[FeishuQA] lark-cli 应用配置完成(app_id=%s...)", app_id[:8])
        return ""

    async def initialize(self) -> None:
        config_err = await self._ensure_cli_configured()
        if config_err:
            logger.warning("[FeishuQA][CONFIG] %s", config_err)
        if int(self._cfg("SYNC_INTERVAL_HOURS", 12)) > 0:
            self._sync_task = asyncio.create_task(self._sync_loop())
        if int(self._cfg("AUTH_CHECK_HOURS", 12)) > 0:
            self._auth_task = asyncio.create_task(self._auth_loop())

    async def terminate(self) -> None:
        for task in (self._sync_task, self._auth_task, getattr(self, "_poll_task", None)):
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
            logger.warning("[FeishuQA][AUTH] %s", message)
            await self._send_reauth_card(message)

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

    async def _send_reauth_card(self, reason: str) -> bool:
        """发起 Device Flow 并以 bot 身份推交互式卡片(一键授权按钮)。"""
        admin_open_id = str(self._cfg("ADMIN_OPEN_ID", "") or "")
        if not admin_open_id:
            logger.info("[FeishuQA] 未配置 ADMIN_OPEN_ID,跳过卡片推送")
            return False

        from .adapter.auth import (
            build_auth_card,
            initiate_reauth_flow,
            poll_auth_completion,
        )

        initiated = await initiate_reauth_flow(self.adapter)
        if not initiated:
            return False

        expires_in = int(initiated.get("expires_in") or 600)
        card = build_auth_card(
            initiated["verification_url"],
            expires_in_min=max(1, expires_in // 60),
            reason=reason.split("\n")[0],
        )
        import astrbot.api.message_components  # noqa: F401 确保包可用

        returncode, stdout = await self.adapter.run_raw(
            [
                "im",
                "+messages-send",
                "--as",
                "bot",
                "--user-id",
                admin_open_id,
                "--msg-type",
                "interactive",
                "--content",
                json.dumps(card, ensure_ascii=False),
            ],
        )
        sent_ok = returncode == 0 and b'"ok": true' in stdout
        logger.info(
            "[FeishuQA] 授权卡片推送%s {message=%s}",
            "成功" if sent_ok else "失败",
            stdout[:120],
        )
        if not sent_ok:
            return False

        # 后台轮询等管理员点击完成;成功即恢复同步能力
        async def _poll() -> None:
            ok = await poll_auth_completion(self.adapter, initiated["device_code"])
            logger.info("[FeishuQA] 授权流程%s", "已完成" if ok else "超时未完成")

        self._poll_task = asyncio.create_task(_poll())
        return True

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
            query (str): 用户的实际问题或关键词
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
            self.adapter.cli_diagnostics(),
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

    @filter.command("qa_auth_login")
    async def qa_auth_login(self, event: AstrMessageEvent):
        """/qa_auth_login:发起 Device Flow 扫码授权(管理员)。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可用")
            return
        from .adapter.auth import initiate_reauth_flow, poll_auth_completion

        if self._poll_task is not None and not self._poll_task.done():
            yield event.plain_result("已有授权流程进行中,请先完成或稍后再试")
            return

        config_err = await self._ensure_cli_configured()
        if config_err:
            yield event.plain_result(config_err)
            return

        initiated = await initiate_reauth_flow(self.adapter)
        if not initiated:
            yield event.plain_result("发起授权失败,请查看服务端日志")
            return

        expires_in = int(initiated.get("expires_in") or 600)
        url = initiated["verification_url"]
        yield event.plain_result(
            f"请在 {max(1, expires_in // 60)} 分钟内打开链接并扫码/确认授权:\n{url}\n"
            "完成后发送 /qa_status 确认登录态。"
        )

        async def _poll() -> None:
            ok = await poll_auth_completion(self.adapter, initiated["device_code"])
            logger.info("[FeishuQA] 授权流程%s", "已完成" if ok else "超时未完成")

        self._poll_task = asyncio.create_task(_poll())

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
            records = []
            if pending_path.is_file():
                try:
                    records = json.loads(pending_path.read_text())
                except (json.JSONDecodeError, OSError):
                    records = []
            records.append(record)
            pending_path.write_text(
                json.dumps(records, ensure_ascii=False, indent=1)
            )
            yield event.plain_result(
                "已收录为待写入候选(写入飞书文档将在下个版本开放,"
                "当前由管理员人工同步)。"
            )
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

    def _get_aiocqhttp_client(self, event: AstrMessageEvent):
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            return getattr(platform, "client", None)
        except Exception:
            return None

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

