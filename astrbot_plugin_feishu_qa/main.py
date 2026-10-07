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
from datetime import UTC, datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools

from .adapter.gateway import DOC_FORMAT_XML, GatewayClient
from .answer.direct import (
    SOURCE_ATTRIBUTION,
    SOURCE_ATTRIBUTION_FUUUMUSIC,
    SOURCE_ATTRIBUTION_MIXED,
    DirectAnswer,
    format_link_list,
)
from .answer.router import AnswerRouter
from .corpus.builder import build_manifest, diff_manifests, merge_manifests
from .corpus.model import extract_symptom_tags, normalize_title
from .corpus.parser import ParseResult, parse_xml
from .fuuumusic import (
    QA_PAGE_URL,
    SOURCE_FEISHU,
    discover_sections,
    download_manifest_images,
    fetch_url,
    parse_qa_document,
    resolve_sections,
)
from .fuuumusic import SOURCE_NAME as SOURCE_FUUUMUSIC
from .jev.client import DEFAULT_ENDPOINT as DEFAULT_JEV_ENDPOINT
from .jev.client import DEFAULT_MODEL as DEFAULT_JEV_MODEL
from .jev.client import JevClient, noul_question
from .jev.policy import (
    ACTION_ANSWER_SELF,
    ACTION_FALLBACK,
    ANY_QID,
    RANK_QID,
    JevCandidate,
    JevDecision,
    build_questions,
    build_state,
    decide_links,
    decide_takeover,
    option_key,
)
from .learn.candidate import (
    build_learn_prompt,
    candidate_to_pending_record,
    extract_transcript,
    find_duplicate,
    format_candidate_display,
    is_chat_record_transcript,
    parse_candidate,
)
from .retrieval.scorer import Retriever
from .storage.decision_log import (
    CandidateRecord,
    DecisionCache,
    DecisionLog,
    DecisionRecord,
)
from .storage.snapshot import SnapshotStore

PLUGIN_NAME = "astrbot_plugin_feishu_qa"
DEFAULT_WIKI_URL = "https://my.feishu.cn/wiki/O9fcwP1PviPuOSkGBekc7B7xn4c"

# 单轮自动附链上限:链接是承诺不是列表,多则刷屏且冲淡主答案。
_MAX_AUTO_LINKS = 3

# 模糊档(MEDIUM)链接条数:刻意少于直答档,降低错误链接的曝光面。
_MAX_TENTATIVE_LINKS = 2

# 送进 Jev 的候选正文截断长度。jaggedness #5:state 里无关内容越多准确率越低,
# 手册条目正文动辄上千字,不截断会把判断稀释掉。
_JEV_TEXT_CHARS = 600

# 送进 Jev 的候选条数上限。Jev 一次调用并行求值全部候选,条数越多 state 越大;
# 本地检索本来就只取前 _MAX_TENTATIVE_LINKS+1,这里再兜一层底。
_JEV_MAX_CANDIDATES = 3

# 金丝雀探测条数:足够看出分歧率的量级,又不至于在插件加载时打太多次外网。
_JEV_SELFTEST_PROBES = 4

# FAQ 引用规范:随 on_llm_request 注入的静态短文本(恒定内容不破坏提示词缓存)。
_FAQ_CITATION_GUIDANCE = (
    "\n[FAQ 引用规范] 知识库结果分两类:"
    "(A)「【全家桶FAQ >」开头的条目是精选问答原文,系统会自动为其附上"
    "飞书文档章节直达链接——你只需依据条目内容组织文字回答,"
    "不要复述条目正文,也不要输出条目末尾的 [ref:xxxxx] 标记。"
    "(B)其他来源(如 Cakewalk sonar 手册)没有可跳转的文档,直接依据知识块"
    "组织回答并翻译要点;不要臆测知识块里「参考图N」「如图」指代的图片内容。"
)




@register(
    PLUGIN_NAME,
    "NDsans",
    "飞书 Q&A 文档驱动的领域问答机器人(高置信直答零 LLM)",
    "0.9.12",
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
        self._entry_refs: dict = {}
        self._router: AnswerRouter | None = None
        self._revision: int | str | None = None

        # 决策日志与问题级记忆:阈值此前只在 20 条手挑样本上校准过,
        # 真实分布只能靠线上观测回填。两者都是纯观测手段,故障一律静默。
        self._decision_log = DecisionLog(
            self.data_root,
            enabled=bool(self._cfg("DECISION_LOG_ENABLED", True)),
            text_mode=str(self._cfg("DECISION_LOG_TEXT_MODE", "truncate")),
            max_text_chars=int(self._cfg("DECISION_LOG_MAX_TEXT_CHARS", 200)),
        )
        self._decision_cache = DecisionCache(
            ttl_seconds=float(self._cfg("DECISION_CACHE_TTL_MINUTES", 240)) * 60
        )

        # Jev 决策模型:只在拿到 key 时构造。未配置 / 构造失败一律 None,
        # 两个接管点见到 None 就走原本的本地判定,行为与未接入时完全一致。
        self._jev: JevClient | None = self._build_jev()

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

    # ── 决策日志 ──

    def _thresholds(self) -> dict[str, float]:
        """当前生效的阈值,随每条决策一起落盘——事后才能复现"当时按什么判的"。"""
        r = self._retriever
        if r is None:
            return {}
        return {"high": r.high_threshold, "medium": r.medium_threshold}

    def _candidate_records(self, results, query: str) -> list[CandidateRecord]:
        """把检索结果转成日志用的打分明细。

        ``supported`` 必须拿**本次判定基准的那句 query** 重算(与判定本身
        同源),不能记成 True——否则日志会高估自己的支撑闸门命中率。
        """
        if not results or self._retriever is None:
            return []
        return [
            CandidateRecord(
                entry_id=r.entry.id,
                title=r.entry.raw_title,
                score=r.score,
                confidence=r.confidence,
                supported=self._retriever.supports_query(r.entry, query),
            )
            for r in results
        ]

    def _log_decision(
        self,
        *,
        stage: str,
        action: str,
        query: str,
        results=None,
        cached: bool = False,
        extra: dict | None = None,
    ) -> bool:
        """落一条决策。任何异常已被 DecisionLog 内部吞掉。返回是否落盘成功。"""
        return self._decision_log.record(
            DecisionRecord(
                stage=stage,
                action=action,
                query=query,
                candidates=self._candidate_records(results, query),
                thresholds=self._thresholds(),
                revision=self._revision,
                cached=cached,
                extra=extra or {},
            )
        )

    def _tentative_enabled(self) -> bool:
        return bool(self._cfg("TENTATIVE_ANSWER_ENABLED", False))

    # ── Jev 决策模型 ──

    def _build_jev(self) -> JevClient | None:
        """按配置构造客户端;未启用或缺 key 时返回 None(调用方走本地判定)。"""
        if not bool(self._cfg("JEV_ENABLED", False)):
            return None
        key = str(self._cfg("JEV_API_KEY", "") or "").strip()
        if not key:
            logger.warning("[FeishuQA] JEV_ENABLED=true 但未配置 JEV_API_KEY,按未启用处理")
            return None
        return JevClient(
            key,
            endpoint=str(self._cfg("JEV_ENDPOINT", "") or "").strip() or DEFAULT_JEV_ENDPOINT,
            model=str(self._cfg("JEV_MODEL", "") or "").strip() or DEFAULT_JEV_MODEL,
            timeout=float(self._cfg("JEV_TIMEOUT_SECONDS", 5.0)),
        )

    @staticmethod
    def _jev_candidates(entries: list, *, limit: int) -> list[JevCandidate]:
        """语料条目 → Jev 候选。文本截断:长 state 会稀释准确率(jaggedness #5)。"""
        out: list[JevCandidate] = []
        for e in entries[:limit]:
            body = " ".join((e.body or "").split())
            out.append(
                JevCandidate(
                    entry_id=e.id,
                    title=normalize_title(e.raw_title),
                    text=body[:_JEV_TEXT_CHARS],
                )
            )
        return out

    def _jev_ask(self, question: str, candidates: list[JevCandidate]):
        """一次调用并行求值全部问题。任何失败返回 ok=False 的结果。"""
        if self._jev is None or not candidates:
            return None
        try:
            result = self._jev.evaluate(
                build_state(question, candidates), build_questions(candidates)
            )
        except Exception as exc:  # 兜底:Jev 不得把整条应答链路带崩
            logger.warning("[FeishuQA] Jev 调用异常,回落本地判定: %s", type(exc).__name__)
            return None
        # 成功也记一行:决策日志在 data/plugin_data 里,不方便从面板回读,
        # 而"到底有没有真的调出去"是最需要一眼可见的事。
        if result.ok:
            ans = result.choice(RANK_QID)
            top = max(ans.probabilities.values()) if ans and ans.probabilities else 0.0
            logger.info(
                "[FeishuQA] Jev 判定 cands=%d answerable=%.2f top_p=%.2f model=%s tok=%d",
                len(candidates),
                result.noul(ANY_QID) or 0.0,
                top,
                result.model,
                result.input_tokens,
            )
        return result

    def _log_jev(self, decision: JevDecision, *, stage: str, question: str, action: str) -> None:
        """Jev 判定落决策日志,并打一行 INFO。

        决策日志在 data/plugin_data/ 下,WebUI 不好回读;而"这次到底按 Jev
        还是按本地规则做的决定"是最需要一眼可见的事,故同时落日志。
        """
        logger.info(
            "[FeishuQA] Jev 决策 stage=%s action=%s reason=%s picked=%s",
            stage, action, decision.reason, ",".join(decision.entry_ids) or "-",
        )
        self._log_decision(
            stage=stage,
            action=action,
            query=question,
            results=[],
            extra={
                "jev_action": decision.action,
                "jev_reason": decision.reason,
                "jev_nouls": {k: round(v, 4) for k, v in decision.nouls.items()},
                "jev_probs": decision.probabilities,
                "jev_picked": list(decision.entry_ids),
            },
        )

    async def _jev_takeover(self, event, question: str, plan) -> bool:
        """C 位点:由 Jev 判定这条 MEDIUM 查询是自己答还是交回主 Agent。

        返回 True 表示"已处理完"(无论答了还是明确交回),调用方不要再走
        本地模糊档分支。返回 False 表示 Jev 未启用或结果不可用,交回调用方
        按原本的本地逻辑处理。

        这里**只发模糊措辞**的章节指引,不贴正文、不附图。理由:Jev 的作用是
        筛掉不够格的候选从而省掉一次 Agent 往返,不是让我们改用更激进的答法。
        """
        if self._jev is None:
            return False
        entries = [r.entry for r in plan.candidates[:_JEV_MAX_CANDIDATES]]
        cands = self._jev_candidates(entries, limit=_JEV_MAX_CANDIDATES)
        result = self._jev_ask(question, cands)
        if result is None:
            return False

        decision = decide_takeover(
            cands,
            result,
            answerable_threshold=float(self._cfg("JEV_ANSWERABLE", 0.50)),
            probability_threshold=float(self._cfg("JEV_TAKEOVER_PROBABILITY", 0.35)),
        )
        if decision.action == ACTION_FALLBACK:
            return False

        if decision.action == ACTION_ANSWER_SELF and decision.entry_ids:
            picked = [e for e in entries if e.id in set(decision.entry_ids)]
            self._log_jev(
                decision, stage="route", question=question, action="jev_tentative_sent"
            )
            self._decision_cache.put(
                question,
                action="tentative",
                entry_ids=[e.id for e in picked],
                revision=self._revision,
            )
            await self._send_direct(event, self._build_tentative_list(event, picked))
            event.stop_event()
        else:
            self._log_jev(
                decision, stage="route", question=question, action="jev_hand_off"
            )
        return True

    def _jev_selfcheck(self) -> None:
        """启动时自检 Jev,并跑一遍**完整决策链**做金丝雀,结果写日志。

        动机有两层:
        1. 改完 `JEV_API_KEY` 之后,管理员最需要的是**立刻**知道能不能用,而不是
           等第一条真实提问才发现鉴权失败或容器出不了网——那时的表现是"静默
           回落���本地判定",看起来像功能没生效,实则可能只是 key 过期。
        2. 只探活不够:连通 ≠ 判定链路通。这里用一条**自带已知答案**的探针
           问题跑 ``router.route → Jev → decide_takeover → 落决策日志``,
           因此每次重启都会在 ``decisions.jsonl`` 里留下一条 ``jev_selftest``
           记录,既能看 Jev 通不通,也能看它**是否真的改变了结论**。

        探针问题取自语料里第一条带问号的条目标题,因此**不依赖固定文案**,
        语料换了仍然有效。失败只告警,插件照常以本地判定服务。
        """
        if self._jev is None:
            return
        probe = self._jev_probe_query()
        try:
            result = self._jev.evaluate(
                state={"question": probe or "Cakewalk 无法激活", "candidates": []},
                questions={
                    "ping": noul_question(
                        "Is the user asking about a Cakewalk software problem?",
                        true="The question is about Cakewalk or its plugins.",
                        false_="The question is not about Cakewalk at all.",
                    )
                },
            )
        except Exception as exc:  # 自检不得让插件起不来
            logger.warning("[FeishuQA] Jev 自检异常: %s", type(exc).__name__)
            return
        if not result.ok:
            logger.warning(
                "[FeishuQA] Jev 自检失败(%s)——判定将静默回落到本地规则,机器人仍可用",
                result.error,
            )
            return
        logger.info(
            "[FeishuQA] Jev 自检通过 model=%s ping=%.2f tok=%d",
            result.model,
            result.noul("ping") or 0.0,
            result.input_tokens,
        )
        self._jev_selftest_sweep()

    def _jev_probe_queries(self, limit: int = _JEV_SELFTEST_PROBES) -> list[str]:
        """从语料里取若干条带问号的标题作为探针问题(不依赖写死文案)。

        取**多条**而非一条:单条只是一次抽样,看不出 Jev 与本地结论的**分歧率**。
        这里按语料顺序取,不挑题——挑一条已知会分歧的当探针就是自欺。
        """
        out: list[str] = []
        for entry in self._entries:
            _, stripped = extract_symptom_tags(entry.raw_title)
            q = stripped.split("？")[0].split("?")[0].strip()
            if len(q) >= 6 and q not in out:
                out.append(q)
            if len(out) >= limit:
                break
        return out

    def _jev_probe_query(self) -> str:
        """单条探针(供 /jev_probe 之外的旧调用点使用)。"""
        queries = self._jev_probe_queries(1)
        return queries[0] if queries else ""

    def _jev_selftest_sweep(self) -> None:
        """扫若干条语料自带问句,统计 Jev 与本地结论的分歧率。

        为什么扫多条:标准里要的是"观察到一次因 Jev 结论而改变的路由结果"。
        拿单条已知会分歧的题当探针是自欺;按语料顺序取若干条、如实报出
        分歧几次,才是真观测——分歧为 0 也是有价值的结论。

        每条都走完整链路并落一条 ``jev_selftest`` 记录。
        """
        queries = self._jev_probe_queries()
        if not queries:
            return
        diverged: list[str] = []
        for q in queries:
            picked, local = self._jev_selftest_chain(q)
            if picked and local and picked != local:
                diverged.append(q)
        logger.info(
            "[FeishuQA] Jev 金丝雀: 探测 %d 条,Jev 与本地结论分歧 %d 条%s",
            len(queries), len(diverged),
            f" → {diverged[:2]}" if diverged else "",
        )

    def _jev_selftest_chain(self, question: str) -> tuple[str, str]:
        """金丝雀:跑完整决策链,落一条 jev_selftest 决策记录。

        记录里同时写本地结论与 Jev 结论,便于一眼看出 Jev 有没有改变动作。
        全程不发消息、不阻断、只写自己的决策日志。
        """
        if self._retriever is None or not question:
            return "", ""
        try:
            # 同样**不走 router**:router 带群白名单门禁,而自检用的 group_id
            # 不在白名单里,会被判 denied 拿不到候选(与 /jev_probe 同一个坑)。
            results = self._retriever.search(question, top_k=_JEV_MAX_CANDIDATES)
            entries = [r.entry for r in results]
            cands = self._jev_candidates(entries, limit=_JEV_MAX_CANDIDATES)
            result = self._jev_ask(question, cands)
        except Exception as exc:
            logger.warning("[FeishuQA] Jev 决策链自检异常: %s", type(exc).__name__)
            return "", ""
        if result is None or not result.ok or not entries:
            logger.info(
                "[FeishuQA] Jev 决策链自检跳过: cands=%d jev=%s",
                len(entries), result.error if result else "no_client",
            )
            return "", ""

        decision = decide_takeover(
            cands,
            result,
            answerable_threshold=float(self._cfg("JEV_ANSWERABLE", 0.50)),
            probability_threshold=float(self._cfg("JEV_TAKEOVER_PROBABILITY", 0.35)),
        )
        local_pick = entries[0].id if entries else ""
        changed = decision.action == ACTION_ANSWER_SELF and bool(
            decision.entry_ids
        ) and local_pick not in decision.entry_ids
        written = self._log_decision(
            stage="route",
            action="jev_selftest",
            query=question,
            results=results,
            extra={
                "jev_action": decision.action,
                "jev_reason": decision.reason,
                "jev_nouls": {k: round(v, 4) for k, v in decision.nouls.items()},
                "jev_probs": decision.probabilities,
                "jev_picked": list(decision.entry_ids),
                "local_top1": local_pick,
                "local_score": round(results[0].score, 3),
                "changed_vs_local": changed,
            },
        )
        logger.info(
            "[FeishuQA] Jev 决策链自检: local_top1=%s(score=%.2f) jev=%s(%s)%s "
            "picked=%s 落盘=%s",
            local_pick,
            results[0].score,
            decision.action,
            decision.reason,
            " [与本地不同]" if changed else "",
            ",".join(decision.entry_ids) or "-",
            "ok" if written else "FAIL",
        )
        return (decision.entry_ids[0] if decision.entry_ids else ""), local_pick

    def _jev_filter_links(
        self, question: str, entries: list
    ) -> JevDecision | None:
        """D 位点:由 Jev 判定这批候选章节哪些值得附给用户。

        返回 None 表示 Jev 未启用/不可用,调用方改用本地 ``linkable_entries``。
        判定基准恒为**用户原问题**——2026-09-15 事故的根因就是拿模型那次
        检索词当基准,而检索相关度是为检索词服务的,与用户问什么无关。

        阈值刻意高于 C 位点:附链是对用户的承诺,发错章节的代价是信任,
        而漏发一条指引的代价只是少个参考(主 Agent 的正文回答还在)。
        """
        if self._jev is None or not question or not entries:
            return None
        cands = self._jev_candidates(entries, limit=_JEV_MAX_CANDIDATES)
        result = self._jev_ask(question, cands)
        if result is None:
            return None
        decision = decide_links(
            cands,
            result,
            answerable_threshold=float(self._cfg("JEV_ANSWERABLE", 0.50)),
            probability_threshold=float(self._cfg("JEV_LINK_PROBABILITY", 0.45)),
            max_links=_MAX_AUTO_LINKS,
        )
        if decision.action == ACTION_FALLBACK:
            return None
        return decision

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
            # 短码索引(魔法链接,Modu ADR-011):code = 稳定 entry_id 去前缀后
            # 前 5 位十六进制;同时保留完整 id 以兼容旧调用。
            self._entry_refs = {}
            for e in entries:
                self._entry_refs[e.id] = e
                self._entry_refs.setdefault(e.id[3:8], e)
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
            self._revision = manifest.get("revision_id")
            # 语料换了 revision,旧判定对新语料不再成立,记忆整体作废。
            self._decision_cache.clear()
            logger.info(
                "[FeishuQA] 语料已装载 revision=%s entries=%d",
                self._revision,
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
        self._migrate_snapshot_to_slices()
        if not self.adapter.available:
            logger.warning(
                "[FeishuQA] lark_cli 平台未加载,飞书拉取/授权不可用,本地语料继续服务"
            )
        if int(self._cfg("SYNC_INTERVAL_HOURS", 12)) > 0:
            self._sync_task = asyncio.create_task(self._sync_loop())
        if bool(self._cfg("FUUUMUSIC_ENABLED", True)):
            self._fuuumusic_task = asyncio.create_task(self._fuuumusic_sync_loop())
        if bool(self._cfg("JEV_SELFTEST_ON_LOAD", True)):
            # 同步执行:管理员在日志里立刻能看到 key/网络是否通,而不是等首问
            self._jev_selfcheck()

    def _migrate_snapshot_to_slices(self) -> None:
        """把单来源时代的 ``corpus.json`` 就地拆成一个分片(一次性,幂等)。

        分片化之前只存在一份 crontab 式的整篇快照;若不做这步,首个跑起来的
        来源会算出"只有自己"的合并结果,把另一个来源的条目静默抹掉——尤其在
        ``SYNC_INTERVAL_HOURS=0``(关闭飞书自动同步)时飞书语料会凭空消失。
        纯本地文件操作,失败只告警。
        """
        try:
            if self.store.load_slice(SOURCE_FEISHU) or self.store.load_slice(SOURCE_FUUUMUSIC):
                return
            old = self.store.load()
            if not old or not old.get("entries"):
                return
            # 旧版网页同步会把 manifest["source"] 写成 "fuuumusic.com"
            name = (
                SOURCE_FUUUMUSIC
                if str(old.get("source") or "") in ("fuuumusic.com", SOURCE_FUUUMUSIC)
                else SOURCE_FEISHU
            )
            for entry in old["entries"]:
                entry.setdefault("source", name)
            old["source"] = name
            self.store.save_slice(name, old)
            logger.info(
                "[FeishuQA] 旧快照已迁移为分片 source=%s entries=%d",
                name,
                len(old["entries"]),
            )
        except Exception as exc:
            logger.warning("[FeishuQA] 快照迁移失败(不影响问答): %s", exc)

    async def terminate(self) -> None:
        for task in (self._sync_task, getattr(self, "_fuuumusic_task", None)):
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

    async def _fuuumusic_sync_loop(self) -> None:
        interval = int(self._cfg("FUUUMUSIC_SYNC_INTERVAL_HOURS", 24)) * 3600
        await asyncio.sleep(60)  # 启动后稍等,避免与飞书同步争抢带宽
        while True:
            try:
                result = await self._sync_fuuumusic()
                logger.info("[FeishuQA] fuuumusic 定时同步: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[FeishuQA] fuuumusic 同步失败: %s", exc)
            await asyncio.sleep(interval)

    async def sync_once(self) -> dict:
        """抓取→解析→下载缺失图→原子提交→热替换检索器。"""
        doc = await self.adapter.fetch_doc(
            fmt=DOC_FORMAT_XML, detail="with-ids"
        )
        parsed = parse_xml(doc.content, source_revision=doc.revision_id)
        manifest = build_manifest(
            parsed,
            revision_id=doc.revision_id,
            document_id=doc.document_id,
            source=SOURCE_FEISHU,
        )
        old = self.store.load()
        # 差异一律按**合并视图**比较(旧合并 vs 新合并,见下方 commit 之后):
        # 飞书同步只替换自己那片分片,若拿"自己的分片"与"整份合并快照"相减,
        # 会把另一个来源的全部条目报成 removed(线上实测 removed=76,极易误读
        # 成"语料被删了")。

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

        # 飞书只写自己的分片;合并快照由全部分片重算(网页那份不会被抹掉)。
        self.store.save_slice(SOURCE_FEISHU, manifest)
        merged = self._commit_merged()
        diff = diff_manifests(old, merged)
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
            "corpus_entries": merged["entry_count"],
            "sources": {k: v["kept"] for k, v in merged.get("sources", {}).items()},
            "image_failures": failures,
            "relinked": len(relinked),
        }

    async def _sync_fuuumusic(self) -> dict:
        """抓取小闻的问答聚合页 → 逐条结构化 → 写入自己的分片 → 重算合并快照。

        只写 ``corpus_sources/fuuumusic.json``,绝不直接改写 ``corpus.json``:
        合并视图由全部分片重算(见 ``_commit_merged``)。2026-10-07 事故正是
        两个来源各自整篇覆盖快照,导致 47 条飞书语料与 76 条网页语料每 12 小时
        轮流消失。
        """
        if not bool(self._cfg("FUUUMUSIC_ENABLED", True)):
            return {"status": "disabled"}

        try:
            html = await fetch_url(QA_PAGE_URL)
            if not html:
                return {"status": "error", "reason": "qa page fetch failed"}

            sections_cfg = self._cfg("FUUUMUSIC_SECTIONS", []) or []
            wanted = [str(s) for s in sections_cfg if str(s).strip()]
            available = discover_sections(html)
            sections = resolve_sections(wanted, available)
            if wanted and not sections:
                # 配置过的章节名在页面上一个都不存在(旧版目录名残留 / 站点改版):
                # 不能据此导出 0 条,那等于把整个网页语料清零。
                logger.warning(
                    "[FeishuQA] FUUUMUSIC_SECTIONS 配置的章节均不存在于页面,"
                    "本次按不过滤处理。配置=%s 页面实际=%s",
                    wanted,
                    available,
                )
            revision_id = int(datetime.now(UTC).timestamp())
            entries = parse_qa_document(
                html, revision_id=revision_id, sections=sections or None
            )
            if not entries:
                # 站点改版等异常时宁可保留旧分片,也不要用空语料覆盖掉用户
                # 已经能用的知识库。
                return {
                    "status": "error",
                    "reason": f"no entries parsed (sections={sections})",
                }

            manifest = build_manifest(
                ParseResult(entries=entries),
                revision_id=revision_id,
                document_id="fuuumusic_qa",
                source=SOURCE_FUUUMUSIC,
            )
            images_ok, image_failures = await download_manifest_images(
                manifest, self.data_root
            )
            self.store.save_slice(SOURCE_FUUUMUSIC, manifest)
            merged = self._commit_merged()

            return {
                "status": "synced",
                "entries": manifest["entry_count"],
                "images": images_ok,
                "image_failures": image_failures,
                "corpus_entries": merged["entry_count"],
                "sources": {k: v["kept"] for k, v in merged.get("sources", {}).items()},
            }
        except Exception as exc:
            logger.warning("[FeishuQA] fuuumusic 同步失败: %s", exc)
            return {"status": "error", "reason": str(exc)}

    def _commit_merged(self) -> dict:
        """由全部分片重算合并快照并热替换检索器。

        分片顺序即优先级:飞书在前——网页里的 FAQ 部分是飞书文档的镜像
        (同章节同标题 → 同 ID),飞书那份是人工维护的原文且图片已落地。
        """
        merged = merge_manifests(
            [
                (SOURCE_FEISHU, self.store.load_slice(SOURCE_FEISHU)),
                (SOURCE_FUUUMUSIC, self.store.load_slice(SOURCE_FUUUMUSIC)),
            ]
        )
        self.store.commit(merged)
        self._load_corpus()
        return merged

    # ── 指令 ──

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admins = list(self._cfg("ADMIN_USERS", []))
        return str(event.get_sender_id()) in {str(a) for a in admins} or event.is_admin()

    @filter.command("问", alias={"qa", "Q&A"})
    async def ask(self, event: AstrMessageEvent):
        """/问 <问题>:确定性直答入口(高置信不调用 LLM)。"""
        question = self._strip_command(event.message_str, "问", "qa", "Q&A")
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
            entry = self._entry_refs.get(plan.direct.entry_id)
            if entry is not None:
                await self._send_direct(
                    event, self._build_link_list(event, [entry])
                )
            else:
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
        if not text or text.startswith("/"):
            # 任何 `/` 开头的都是指令(含别名),一律交给 command handler。
            # 原先只放过 /问 与 /qa,导致 /jev_probe、/qa_sync、/learn 等被
            # @ 之后仍被当成提问送进判定链路——线上实测 /jev_probe 文本里的
            # "midi 键盘怎么连" 真的被判成提问并触发了 Jev。
            return
        router = self._router
        if router is None:
            return
        group_id = event.get_group_id()
        umo = event.unified_msg_origin

        # 同一问题在 TTL 内重复问 → 复用首次判定,杜绝"同一句问两次答案不同"。
        cached = self._decision_cache.get(text, revision=self._revision)
        if cached is not None:
            entry_ids = list(cached["entry_ids"])
            if cached["action"] == "direct" and entry_ids:
                entry = self._entry_refs.get(entry_ids[0])
                if entry is not None:
                    self._log_decision(
                        stage="route",
                        action="direct",
                        query=text,
                        results=[],
                        cached=True,
                    )
                    await self._send_direct(
                        event, self._build_link_list(event, [entry])
                    )
                    event.stop_event()
                    return

        plan = router.route(text, group_id=group_id, umo=umo, top_k=_MAX_TENTATIVE_LINKS + 1)

        if plan.kind == "direct" and plan.direct:
            entry = self._entry_refs.get(plan.direct.entry_id)
            self._decision_cache.put(
                text,
                action="direct",
                entry_ids=[plan.direct.entry_id],
                revision=self._revision,
            )
            self._log_decision(
                stage="route", action="direct", query=text, results=plan.candidates
            )
            if entry is not None:
                await self._send_direct(event, self._build_link_list(event, [entry]))
            else:
                await self._send_direct(event, plan.direct)
            event.stop_event()
            return

        if plan.kind == "tentative":
            # MEDIUM 区:证据不足以断言"这就是答案",但可能有用。
            if await self._jev_takeover(event, text, plan):
                return
            # Jev 未启用/不可用 → 沿用纯本地的模糊档开关,行为与 v0.9.0 一致。
            if self._tentative_enabled():
                picked = [r.entry for r in plan.candidates[:_MAX_TENTATIVE_LINKS]]
                self._log_decision(
                    stage="route",
                    action="tentative_sent",
                    query=text,
                    results=plan.candidates,
                )
                self._decision_cache.put(
                    text,
                    action="tentative",
                    entry_ids=[e.id for e in picked],
                    revision=self._revision,
                )
                await self._send_direct(
                    event,
                    self._build_tentative_list(event, picked),
                )
                event.stop_event()
                return
            # 关闭时也记录:一周后据此判断"若开启会发出什么",零风险回放。
            self._log_decision(
                stage="route",
                action="tentative_disabled",
                query=text,
                results=plan.candidates,
            )
            return

        # miss:证据不足,不回复、不阻断 → 主 Agent 正常接管
        self._log_decision(
            stage="route", action="miss", query=text, results=plan.candidates
        )


    @filter.llm_tool(name="qa_send_answer")
    async def qa_send_answer(self, event: AstrMessageEvent, entry_ids: str):
        """把与用户问题匹配的一个或多个 QA 条目整理成飞书文档章节直达链接列表发送给用户。

        当【全家桶FAQ】条目命中问题时优先使用;链接列表发出后只需简短衔接,
        不要复述条目内容。

        Args:
            entry_ids (str): 知识库条目末尾 [ref:xxxxx] 里的引用短码,
                多个用英文逗号分隔
        """
        wanted = [s for s in re.split(r"[,，、;\s]+", entry_ids or "") if s.strip()]
        entries, invalid, seen = [], [], set()
        for raw in wanted:
            # 魔法短码(Modu ADR-011):知识块携带的引用码 → 语料条目;
            # 完整条目 ID 亦接受。映射外取值一律拒绝,模型无法猜测绕过。
            entry = self._entry_refs.get(raw)
            if entry is None:
                invalid.append(raw)
                continue
            if entry.id in seen:
                continue
            seen.add(entry.id)
            entries.append(entry)
        skipped = list(invalid)

        # 会话级去重:自动附链(auto_send_faq_links / Jev 闸门)已经发过的条目,
        # 模型再点一次不再重发。线上实测 2026-09-27:用户问"cakewalk 打不开了",
        # Jev 闸门发出【打开就闪退】,主 Agent 随后又调 qa_send_answer 重发同一条,
        # 用户收到两条内容重复的链接消息。此处补齐去重,跳过项照实回报给模型。
        already = set(event.get_extra("_faq_links_sent") or ())
        duplicated: list[str] = []
        if already:
            fresh = [e for e in entries if e.id not in already]
            duplicated = [e.raw_title[:16] for e in entries if e.id in already]
            entries = fresh
        if not entries:
            if duplicated:
                detail = ",".join(duplicated + invalid)
                return f"未重复投递:这轮问里已发过({detail})"
            bad = ",".join(invalid) if invalid else "未提供有效条目"
            return f"发送失败:没有可投递的条目({bad})"

        direct = self._format_links(event, entries)
        # 单次 event.send 投出整块;经此而非 set_result:Agent 循环保持存活。
        await self._send_direct(event, direct)
        note = f";已跳过:{','.join(skipped)}" if skipped else ""
        if duplicated:
            note += f";已发过不重发:{','.join(duplicated)}"
        titles = "》《".join(normalize_title(e.raw_title) for e in entries)
        return (
            f"已投递{len(entries)}条章节直达链接:《{titles}》{note};"
            "请勿复述条目正文,链接里含图文步骤。"
        )

    def _attribution_for(self, entries: list) -> str:
        """按条目来源给署名——链接指向哪里就署哪里的名。

        混投时用中性说法:一轮里同时发出飞书与网页条目,任何单边署名都只
        覆盖一半,反而误导。
        """
        sources = {str(getattr(e, "source", "") or SOURCE_FEISHU) for e in entries}
        if sources == {SOURCE_FUUUMUSIC}:
            return SOURCE_ATTRIBUTION_FUUUMUSIC
        if len(sources) > 1:
            return SOURCE_ATTRIBUTION_MIXED
        return SOURCE_ATTRIBUTION

    def _format_links(self, event, entries: list) -> DirectAnswer:
        """所有用户可见链接消息的唯一出口。

        四条投递路径(高置信直答 / 模糊档 / LLM 工具投递 / 自动附链)曾各写各
        的头部与尾部,线上一次回答里能同时看到三种格式,用户无从判断"这是不
        是同一种东西"。措辞与版式全部收口到 ``format_link_list``。
        """
        platform_name = ""
        with contextlib.suppress(Exception):
            platform_name = str(event.get_platform_name() or "")
        return format_link_list(
            entries,
            url_of=self._wiki_block_url,
            markdown=platform_name == "qq_official",
            attribution=self._attribution_for(entries),
        )

    def _build_link_list(self, event, entries: list) -> DirectAnswer:
        """章节链接列表(直答路径;与其它路径共用同一格式)。"""
        return self._format_links(event, entries)

    def _build_tentative_list(self, event, entries: list) -> DirectAnswer:
        """模糊档的章节链接列表——与直答/工具/自动附链格式完全一致。

        保留方法名只为让 C 位点的调用点自解释;它不再有任何独立措辞。
        """
        return self._format_links(event, entries)

    def _wiki_block_url(self, entry) -> str:
        """条目直达链接。

        网页来源的条目 ``source_locator`` 本身就是**可点的完整 URL**
        (``.../cakewalk-sonar-faq/all#q38``),直接返回;否则按飞书文档锚点拼。
        多来源之前只有"飞书锚点"一种拼法,网页条目的定位符会被拼成
        ``<wiki>#https://www.fuuumusic.com/...#q38`` 这种废链。
        """
        locator = str(getattr(entry, "source_locator", "") or "").strip()
        if locator.startswith(("http://", "https://")):
            return locator
        base = str(self._cfg("WIKI_URL", "") or "").strip().rstrip("/")
        if not base:
            return "(未配置 WIKI_URL)"
        if locator:
            return f"{base}#{locator}"
        return base

    @filter.on_llm_tool_respond()
    async def auto_send_faq_links(self, event, tool, tool_args, tool_result) -> None:
        """astr_kb_search 命中精选问答后,自动投递对应章节直达链接。

        确定性投递:解析检索结果里的 [ref:短码],不依赖模型自觉调用工具;
        同一轮会话内已发过的条目自动去重。单次最多附 3 条防刷屏。

        **附链是对用户的承诺**("你的问题在这里有答案"),因此不能照抄检索顺序:
        2026-09-15 实测,用户问"Cakewalk 的混音台怎么打开",模型检索后把
        检索结果里前三条 FAQ(ref:c1a91/c118a/8a197——分别是"连接MIDI设备"
        "Clip重叠""搜索不到自带音源")当链接发出,与提问毫无关系。根因是
        KB 检索的相关度是**为该次检索词**服务的,靠前的 FAQ 只说明它和
        "Cakewalk"同域,不代表答得上用户的问题。现改为用**用户原问题**做
        一次本地确定性判定,沿用与 Tier 0 直答同一套证据标准(本地 MEDIUM+
        且头部覆盖主题词),不达标就不附链——宁可少一条链接,也不把无关
        章节塞给用户。零 LLM、零网络,仍是纯本地计算。
        """
        if getattr(tool, "name", "") != "astr_kb_search" or tool_result is None:
            return
        try:
            text = "\n".join(
                c.text for c in tool_result.content if getattr(c, "text", None)
            )
        except Exception:
            return
        codes = list(dict.fromkeys(re.findall(r"\[ref:([a-z0-9]{5})\]", text)))
        candidates = []
        for code in codes:
            entry = self._entry_refs.get(code)
            if entry is not None:
                candidates.append(entry)
        # 判定基准是用户原问题(剥掉 @唤醒),不是模型的检索词。
        question = self._strip_wake(getattr(event, "message_str", "") or "")
        if not candidates:
            self._log_decision(
                stage="linkable",
                action="links_suppressed",
                query=question,
                results=[],
                extra={"reason": "no_ref_matched", "n_refs": len(codes)},
            )
            return
        retriever = self._retriever
        n_before = len(candidates)
        jev_decision = self._jev_filter_links(question, candidates)
        if jev_decision is not None:
            kept = set(jev_decision.entry_ids)
            candidates = [e for e in candidates if e.id in kept]
            if not candidates:
                self._log_jev(
                    jev_decision,
                    stage="linkable",
                    question=question,
                    action="jev_links_suppressed",
                )
                return
            self._log_jev(
                jev_decision,
                stage="linkable",
                question=question,
                action="jev_links_sent",
            )
        elif retriever is not None and question:
            candidates = retriever.linkable_entries(
                question, candidates, max_n=_MAX_AUTO_LINKS
            )
        else:
            candidates = candidates[:_MAX_AUTO_LINKS]
        if not candidates:
            # 闸门全灭:这正是 2026-09-15 事故要拦的形态(检索命中但答非所问)。
            # 抑制原因落盘,一周后可直接统计闸门的真实拦截率与误杀率。
            self._log_decision(
                stage="linkable",
                action="links_suppressed",
                query=question,
                results=[],
                extra={"reason": "gate_rejected_all", "n_refs": n_before},
            )
            return
        sent = set(event.get_extra("_faq_links_sent") or ())
        new_entries = [e for e in candidates if e.id not in sent]
        if not new_entries:
            # 候选全部已在会话内投过:不重复打扰,但判定本身仍记一条,
            # 否则"附链被抑制"与"附链重复"在日志里无法区分。
            self._log_decision(
                stage="linkable",
                action="links_suppressed",
                query=question,
                results=[],
                extra={"reason": "all_already_sent", "n_candidates": len(candidates)},
            )
            return
        sent.update(e.id for e in new_entries)
        self._log_decision(
            stage="linkable",
            action="links_sent",
            query=question,
            results=[],
            extra={
                "n_sent": len(new_entries),
                "n_candidates": len(candidates),
                "titles": [e.raw_title for e in new_entries],
            },
        )
        await self._send_direct(event, self._format_links(event, new_entries))
        event.set_extra("_faq_links_sent", sent)

    @filter.on_llm_request()
    async def add_faq_citation_guidance(self, event: AstrMessageEvent, req) -> None:
        """注入 FAQ 引用规范(恒定文本,前缀缓存安全)。

        条目发现由知识块内的 [ref:短码] 承载,模型从检索结果中原样搬运
        短码调用 qa_send_answer;此处不再做词法预判与动态注入。
        """
        req.system_prompt = (req.system_prompt or "") + _FAQ_CITATION_GUIDANCE

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

    @filter.command("jev_probe", alias={"jev诊断"})
    async def jev_probe(self, event: AstrMessageEvent):
        """[管理] 用 Jev 干跑一次判定,打印候选概率与最终决策。不发群、不阻断。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可用")
            return
        if self._jev is None:
            yield event.plain_result(
                "Jev 未启用。检查配置 JEV_ENABLED 与 JEV_API_KEY。"
            )
            return
        question = self._strip_command(event.message_str, "jev_probe", "jev诊断")
        if not question.strip():
            yield event.plain_result("用法:/jev_probe <你的问题>")
            return

        router = self._router
        if router is None:
            yield event.plain_result("语料尚未就绪,请先 /qa_sync")
            return
        # 诊断命令**不受群白名单约束**:私聊(C2C)下 get_group_id() 为 None,
        # 走 router 会被判 denied,管理员反而在自己的私聊里看不到诊断结果。
        # 这里直接用检索器取候选,只为拿概率,不做任何投递。
        results = self._retriever.search(question, top_k=_JEV_MAX_CANDIDATES)
        entries = [r.entry for r in results]
        cands = self._jev_candidates(entries, limit=_JEV_MAX_CANDIDATES)
        result = self._jev_ask(question, cands)
        local_kind = f"local_top1_score={results[0].score:.2f}" if results else "no_hit"

        ans_t = float(self._cfg("JEV_ANSWERABLE", 0.50))
        take_t = float(self._cfg("JEV_TAKEOVER_PROBABILITY", 0.35))
        link_t = float(self._cfg("JEV_LINK_PROBABILITY", 0.45))
        lines = [
            f"问题: {question}",
            f"本地检索: {local_kind}",
            f"阈值: answerable>={ans_t}  takeover>={take_t}  link>={link_t}",
        ]
        if result is None or not result.ok:
            reason = result.error if result is not None else "no_candidates"
            lines.append(f"Jev: **不可用** ({reason}) → 会回落到本地判定")
            yield event.plain_result("\n".join(lines))
            return

        answerable = result.noul(ANY_QID)
        gate = "✅过门槛" if answerable >= ans_t else "❌未过门槛"
        lines.append(f"answerable = {answerable:.3f} {gate}")
        choice = result.choice(RANK_QID)
        for i, entry in enumerate(entries):
            key = option_key(i)
            p = choice.probabilities.get(key) if choice else None
            mark = " ←选中" if choice and choice.choice == key else ""
            p_txt = f"{p:.3f}" if p is not None else "缺失"
            lines.append(f"  [{i}] P={p_txt}{mark}  {normalize_title(entry.raw_title)[:34]}")
        if choice is not None and choice.confidence is not None:
            lines.append(f"choice confidence = {choice.confidence:.3f}")
        decision = decide_takeover(
            cands,
            result,
            answerable_threshold=ans_t,
            probability_threshold=take_t,
        )
        picked = f" → {list(decision.entry_ids)}" if decision.entry_ids else ""
        lines.append(f"C 位点决策: {decision.action} ({decision.reason}){picked}")
        lines.append(f"token 用量: {result.input_tokens} in / model {result.model}")
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

    @filter.command("fuuumusic_sync")
    async def fuuumusic_sync(self, event: AstrMessageEvent):
        """手动触发 fuuumusic.com 同步(管理员)。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可用")
            return
        yield event.plain_result("开始同步 fuuumusic.com…")
        try:
            result = await self._sync_fuuumusic()
            yield event.plain_result(f"fuuumusic 同步完成: {result}")
        except Exception as exc:
            yield event.plain_result(f"fuuumusic 同步失败: {exc}")

    @filter.command("qa_reload")
    async def qa_reload(self, event: AstrMessageEvent):
        """重新加载本地语料(管理员)。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可用")
            return
        ok = self._load_corpus()
        yield event.plain_result("语料已重新加载" if ok else "语料加载失败")


    @filter.command("learn", alias={"学习"})
    async def learn(self, event: AstrMessageEvent):
        """/learn(或 /学习):分析最近群聊,提取候选 QA(管理员;确认后才生效)。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可用")
            return

        text = self._strip_command(event.message_str, "learn", "学习")
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

        # 1) 取素材:聊天记录转录 > 引用消息 > 平台历史(见 _collect_learn_material)
        history_texts, source_note = await self._collect_learn_material(event, text)
        if history_texts is None:
            yield event.plain_result(
                "没能取到可分析的素材。可以这样用:\n"
                "① 把群里的问答「合并转发」到群里,再引用它发 /学习;\n"
                "② 直接引用某条回答发 /学习;\n"
                "③(仅 aiocqhttp)直接 /学习 分析最近群聊。"
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
            yield event.plain_result(
                f"这份素材({source_note})里没有值得加入 FAQ 的新问答。"
            )
            return

        duplicate = find_duplicate(candidate, self._retriever) if self._retriever else None
        if duplicate is not None:
            yield event.plain_result(
                f"与既有条目高度相似,不新增:\n【{duplicate.raw_title}】\n"
                f"如需更新该条目内容,请直接编辑飞书文档。"
            )
            return

        self._pending_learn[user_id] = candidate
        yield event.plain_result(
            f"[素材来源: {source_note}]\n{format_candidate_display(candidate)}"
        )

    async def _collect_learn_material(
        self, event: AstrMessageEvent, text: str
    ) -> tuple[list[str] | None, str]:
        """按可靠性优先级收集 /learn 的分析素材。

        实测(2026-09-16)三条来源的可用性与质量:

        1. **聊天记录转录**(最优):官方 QQ 机器人在**服务端**就把合并转发展开成
           纯文本(message_type=102,[群聊的聊天记录] + === 消息 N === + [发送者]),
           随 content 一起下发,所以插件无需任何转发组件解析,`message_str` 即完整
           转录。素材由转发者人工筛选,信噪比远高于"最近 N 条闲聊",且自带发送者。
        2. **引用消息**:取被引用内容及本条的正文(被引用内容在 Reply 组件的
           message_str/chain 上)。适合"引用某人的回答 → /学习"。
        3. **平台历史**(aiocqhttp 专属):`get_group_msg_history`,其他平台不可用。

        返回 (素材列表, 来源说明);都取不到时素材为 None。
        """
        # 1) 本条消息本身就是一份聊天记录转录
        if is_chat_record_transcript(text):
            return [extract_transcript(text)], "聊天记录转录"

        # 2) 引用消息:被引用的内容 + 本条附带说明
        quoted = self._extract_quoted_text(event)
        if quoted:
            material = [quoted]
            if text.strip():
                material.append(text.strip())
            return material, "引用消息"

        # 3) 平台历史回退
        history_texts = await self._fetch_recent_history(event)
        if history_texts:
            return history_texts, "最近群聊历史"
        return None, ""

    def _extract_quoted_text(self, event: AstrMessageEvent) -> str:
        """取出本条消息引用的内容(纯文本);无引用或取不到返回空串。"""
        for comp in event.get_messages() or []:
            if getattr(comp, "type", "") != "Reply":
                continue
            direct = str(getattr(comp, "message_str", "") or "").strip()
            if direct:
                return direct
            chain = getattr(comp, "chain", None) or []
            parts = [
                str(getattr(c, "text", "") or "")
                for c in chain
                if getattr(c, "text", None)
            ]
            joined = " ".join(p for p in parts if p).strip()
            if joined:
                return joined
        return ""

    async def _fetch_recent_history(self, event: AstrMessageEvent) -> list[str] | None:
        """读最近 N 条群消息文本;仅 aiocqhttp 可用,其他平台返回 None。

        qq_official 的 event.bot 是 BotAPI(无 call_action),此前会走到
        `client.api.call_action` 并抛 AttributeError——虽被兜住,但每轮都产生
        一条无意义的失败日志。这里先按平台判定,不做注定失败的尝试。
        """
        limit = int(self._cfg("LEARN_CONTEXT_MESSAGES", 50))
        platform_name = ""
        with contextlib.suppress(Exception):
            platform_name = str(event.get_platform_name() or "")
        if platform_name and platform_name != "aiocqhttp":
            return None
        try:
            bot = getattr(event, "bot", None)
            client = bot if bot is not None else self._get_aiocqhttp_client(event)
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
    def _strip_command(message_str: str, *command_names: str) -> str:
        """剥掉指令名,返回参数部分。

        WakingCheckStage 在唤醒检查时**已经把 wake_prefix("/")剥掉**
        (astrbot/core/pipeline/waking_check/stage.py:130),所以 handler 收到的
        是"指令名+参数"形态(`learn ok`、`问 xxx`),而不是 `/learn ok`。
        2026-09-16 线上实测因此踩坑:`/learn ok` 剥不出 "ok" → 确认分支永不命中
        → 掉进素材分析分支并回复"没能取到可分析的素材"。这里显式剥指令名。
        """
        text = (message_str or "").strip()
        # 核心通常会剥掉 wake_prefix,但私聊/其他入口下可能仍带 "/",一并容错。
        probe = text[1:].lstrip() if text.startswith("/") else text
        for name in sorted(command_names, key=len, reverse=True):
            if not name:
                continue
            if probe == name or name == text:
                return ""
            for candidate in (probe, text):
                if candidate.startswith(name + " "):
                    return candidate[len(name) + 1 :].strip()
        if probe != text:
            text = probe
        for prefix in ("/问", "问:", "问 ", "问"):
            if text.startswith(prefix):
                return text[len(prefix) :].strip()
        return text

    @staticmethod
    def _strip_wake(message_str: str) -> str:
        return (message_str or "").strip()
