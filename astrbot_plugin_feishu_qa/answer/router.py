"""AnswerRouter — 白名单门 + 置信路由(spec §21-§22/§28-§29)。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..corpus.model import QaEntry
from ..retrieval.scorer import Confidence, Retriever, SearchResult
from ..storage.snapshot import SnapshotStore
from .direct import DirectAnswer, format_direct_answer


@dataclass
class AnswerPlan:
    """一次提问的路由结果。main.py 据此组装消息链。

    kind:
      - "denied":    白名单外,零响应(不应产生任何消息)
      - "direct":    高置信原文直答(0 LLM)
      - "tentative": 命中 MEDIUM 区:证据不足以下断言,但可能相关。**是否真的
                      投递由调用方按 TENTATIVE_ANSWER_ENABLED 决定**,本层
                      只如实报告证据等级,不做投递策略判断(保持纯逻辑)。
      - "miss":      无足够相关内容,返回兜底话术(0 LLM)

    candidates 恒定携带 top-N 打分明细,供决策日志与模糊档组装使用;
    它不改变任何投递行为。
    """

    kind: str
    direct: DirectAnswer | None = None
    score: float = 0.0
    candidates: list[SearchResult] = field(default_factory=list)


class AnswerRouter:
    """纯逻辑路由器;消息链组装由 main.py 完成。"""

    def __init__(
        self,
        retriever: Retriever,
        *,
        store: SnapshotStore | None = None,
        enabled_groups: list[str] | None = None,
        max_images: int = 3,
    ) -> None:
        self.retriever = retriever
        self.store = store
        # spec §29:默认空白名单 = 不启用任何群;["*"] 表示全部。
        # 条目两种形态(与 bili_verify 白名单约定一致):
        #   - 裸群 ID(aiocqhttp 数字号 / qq_official group_openid):按 get_group_id 匹配
        #   - 完整 UMO(实例ID:GroupMessage:群openid):与事件 unified_msg_origin 精确匹配,
        #     用于多 bot 实例下精确圈定"哪个平台实例的哪个群"
        self.enabled_groups = list(enabled_groups or [])
        self.max_images = max_images

    def group_enabled(self, group_id: str | None, umo: str | None = None) -> bool:
        if "*" in self.enabled_groups:
            return True
        for entry in self.enabled_groups:
            if ":" in entry:
                if umo and entry == umo:
                    return True
            elif group_id and entry == group_id:
                return True
        return False

    def route(
        self, query: str, *, group_id: str | None, umo: str | None = None, top_k: int = 3
    ) -> AnswerPlan:
        if not self.group_enabled(group_id, umo=umo):
            return AnswerPlan(kind="denied")

        results: list[SearchResult] = self.retriever.search(query, top_k=top_k)
        if not results:
            return AnswerPlan(kind="miss")

        top = results[0]
        if top.confidence == Confidence.HIGH and isinstance(top.entry, QaEntry):
            direct = format_direct_answer(
                top.entry, store=self.store, max_images=self.max_images
            )
            return AnswerPlan(
                kind="direct", direct=direct, score=top.score, candidates=results
            )
        if top.confidence == Confidence.MEDIUM:
            # 落在 MEDIUM 区:如实上报为 tentative。是否投递由 main.py 决定,
            # 路由器不持有投递策略(默认仍交主 Agent,行为与旧版一致)。
            return AnswerPlan(kind="tentative", score=top.score, candidates=results)
        return AnswerPlan(kind="miss", score=top.score, candidates=results)
