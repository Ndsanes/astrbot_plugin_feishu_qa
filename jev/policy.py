"""Jev 判定的落地策略(纯逻辑,可单测,不联网)。

把"概率值"翻译成"发不发、返哪几条"。本模块**不认识** QaEntry / AstrBot,
只吃候选的 (id, 标题, 文本) 三元组,便于离线用假概率穷举。

## 两个位点的误判代价方向相反,阈值必须分开

- **C(接管与否)**:误判"答得上"→ 我们用自己的模糊措辞回一句,顶多不够好;
  误判"答不上"→ 本该答的问题静默,把主 Agent 那一轮也省没了,用户干等。
  → **代价偏向"答得上"**,阈值取低。

- **D(返回哪几条)**:误判"值得附"→ 把不相关章节发给用户,这是 2026-09-15
  已经发生过的事故形态(承诺了"你的问题在这里有答案"却给错章节),代价是
  信任;误判"不值得附"→ 少给一条指引,主 Agent 的正文回答还在。
  → **代价偏向"别附"**,阈值取高。

所以拿同一个阈值同时管两个位点,必然在一边出错。这是它们必须独立配置的原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 主 Agent 接管点(C)与附链投递点(D)的判定动作
ACTION_ANSWER_SELF = "answer_self"  # 我们自己回(模糊措辞),阻断 Agent
ACTION_HAND_OFF = "hand_off"  # 交主 Agent(候选不自行投递)
ACTION_FALLBACK = "fallback_local"  # Jev 不可用/未启用,完全走本地判定


@dataclass
class JevCandidate:
    """喂给 Jev 判断的一个候选。只保留判断需要的三样东西。"""

    entry_id: str
    title: str
    text: str


# state 里给模型的字段名,与官方 cookbook 的做法一致(把待判断的成对材料
# 放进 state,问题保持字面简短——jaggedness 第 4 条:少跳一层是一层)。
QUESTION_FIELD = "question"
CANDIDATES_FIELD = "candidates"

SYNTHESIS_QID = "needs_synthesis"
SUFFICIENT_PREFIX = "sufficient_"


def sufficient_qid(index: int) -> str:
    return f"{SUFFICIENT_PREFIX}{index}"


def build_state(question: str, candidates: list[JevCandidate]) -> dict:
    """构造 Jev state。

    截断候选正文:jaggedness 第 5 条明确"state 里无关内容越多准确率越低",
    而候选正文可能很长。截断是**准确率**要求,不是省钱——省的是输出(本就免费)。
    """
    return {
        QUESTION_FIELD: question,
        CANDIDATES_FIELD: [
            {"id": c.entry_id, "title": c.title, "text": c.text} for c in candidates
        ],
    }


def build_questions(candidates: list[JevCandidate]) -> dict:
    """一次调用并行求值:需不需要综合 + 每条候选举不够格。

    刻意**不**问"要给几条"——jaggedness 第 2 条:jev-1.13 数数不可靠,长列表
    尤其糟。条数是代码根据 noul 阈值算出来的结果,不是喂给模型的输入。
    """
    from .client import noul_question

    return {
        SYNTHESIS_QID: noul_question(
            "Does answering this question require combining information from "
            "more than one of the candidate documents?",
            true=(
                "The question compares, contrasts, or otherwise needs facts from "
                "two or more candidates together to answer."
            ),
            false_=(
                "A single candidate could answer it on its own; no comparison or "
                "cross-document synthesis is required."
            ),
        ),
        **{
            sufficient_qid(i): noul_question(
                f"Could candidate {i} answer the question?",
                true=(
                    "This candidate addresses the specific thing being asked and "
                    "states the answer or the steps for it."
                ),
                false_=(
                    "This candidate is on a related topic, or covers a different "
                    "aspect, and does not address what was asked."
                ),
            )
            for i in range(len(candidates))
        },
    }


@dataclass
class JevDecision:
    action: str
    entry_ids: tuple[str, ...] = ()
    nouls: dict[str, float] = field(default_factory=dict)
    reason: str = ""


def _usable(result, n_needed: int) -> bool:
    """结果是否可用于决策。

    除 ok 之外还要求 synthesis 与全部候选问题都真的问到了——**缺失必须
    当作不可用**,不能用缺省值补:补 0.0 等于伪造"模型说不够格"。
    """
    if result is None or not result.ok:
        return False
    if result.noul(SYNTHESIS_QID) is None:
        return False
    return all(result.noul(sufficient_qid(i)) is not None for i in range(n_needed))


def decide_takeover(
    candidates: list[JevCandidate],
    result,
    *,
    sufficiency_threshold: float,
    synthesis_threshold: float,
) -> JevDecision:
    """C 位点:我们自己答,还是交主 Agent。

    顺序有讲究:先看"有没有够格的候选",再看"要不要综合"。反过来的话,
    一个需要综合的问题会被单条候选的高分骗过去,而综合恰恰是单条候选
    最该让位的情况。
    """
    if not candidates:
        return JevDecision(ACTION_FALLBACK, reason="no_candidates")
    if not _usable(result, len(candidates)):
        return JevDecision(ACTION_FALLBACK, reason="jev_unusable")

    nouls = {qid: result.noul(qid) for qid in result.nouls}
    picked = tuple(
        candidates[i].entry_id
        for i in range(len(candidates))
        if result.noul(sufficient_qid(i)) >= sufficiency_threshold
    )
    needs_synthesis = result.noul(SYNTHESIS_QID) >= synthesis_threshold

    if picked and not needs_synthesis:
        return JevDecision(
            ACTION_ANSWER_SELF, entry_ids=picked, nouls=nouls, reason="sufficient"
        )
    if needs_synthesis and not picked:
        return JevDecision(
            ACTION_HAND_OFF, nouls=nouls, reason="needs_synthesis"
        )
    # 既有够格候选又需要综合:证据不足以自己断言,交回主 Agent 更稳。
    return JevDecision(ACTION_HAND_OFF, nouls=nouls, reason="mixed_evidence")


def decide_links(
    candidates: list[JevCandidate],
    result,
    *,
    sufficiency_threshold: float,
    max_links: int,
) -> JevDecision:
    """D 位点:这轮要不要附链接、附哪几条。

    只返回**够格**的候选,并按 noul 降序——不是按本地分数降序。2026-09-15
    的事故根因正是"按检索顺序发":检索顺序是为模型那次检索词服务的,与
    用户原问题无关。
    """
    if not candidates:
        return JevDecision(ACTION_FALLBACK, reason="no_candidates")
    if not _usable(result, len(candidates)):
        return JevDecision(ACTION_FALLBACK, reason="jev_unusable")

    scored = [
        (result.noul(sufficient_qid(i)), candidates[i].entry_id)
        for i in range(len(candidates))
    ]
    kept = [pair for pair in scored if pair[0] >= sufficiency_threshold]
    kept.sort(key=lambda pair: (-pair[0], pair[1]))
    return JevDecision(
        ACTION_ANSWER_SELF,
        entry_ids=tuple(eid for _, eid in kept[:max_links]),
        nouls=dict(result.nouls),
        reason="sufficient" if kept else "none_sufficient",
    )
