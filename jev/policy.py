"""Jev 判定的落地策略(纯逻辑,可单测,不联网)。

把 Jev 的答案翻译成"发不发、返哪几条"。本模块**不认识** QaEntry / AstrBot,
只吃候选的 (id, 标题, 文本) 三元组,便于离线用假答案穷举。

## 为什么是 Choice 而不是逐候选 Noul

2026-09-26 实测纠正过一次设计错误。最初对每个候选各问一个 Noul
("这条能回答问题吗"),结果正确条目排第一只有 **3/8**,而且所有候选的分数
挤在 0.85–0.95 —— 绝对概率之间**不竞争**,一条 0.9 并不会让另一条掉下来。

官方 jaggedness 第 8 条正是这么写的:"A Choice over options and one Noul per
option answer different questions: the Choice is **relative**, settling which
option, while each Noul is **absolute**."换成 Choice 后同批样本 **6/8**,
22 题规模上 20/22。

所以本模块按官方的两粒度模式组织,`skill_suggestion` cookbook 的原话是
"Choice to pick a skill and the Nouls to decide whether to suggest one at all":
  - **Choice** → 相对:这几个候选里哪个最对(排序/选择)
  - **Noul**  → 绝对:这堆材料答不答得上用户这个问题(该不该用)

## 两个位点的误判代价方向相反,阈值必须分开

- **C(接管与否)**:误判"答得上"→ 用模糊措辞回一句,顶多不够好;
  误判"答不上"→ 本该答的问题静默,把主 Agent 那一轮也省没了,用户干等。
  → **代价偏向"答得上"**,阈值取低。

- **D(返回哪几条)**:误判"值得附"→ 把不相关章节发给用户,这是 2026-09-15
  已经发生过的事故形态(承诺了"你的问题在这里有答案"却给错章节),代价是信任;
  误判"不值得附"→ 少给一条指引,主 Agent 的正文回答还在。
  → **代价偏向"别附"**,阈值取高。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 主 Agent 接管点(C)与附链投递点(D)的判定动作
ACTION_ANSWER_SELF = "answer_self"  # 我们自己回(模糊措辞),阻断 Agent
ACTION_HAND_OFF = "hand_off"  # 交主 Agent(候选不自行投递)
ACTION_FALLBACK = "fallback_local"  # Jev 不可用/未启用,完全走本地判定

RANK_QID = "best_match"  # Choice:候选里哪个最对
ANY_QID = "answerable"  # Noul:这堆材料答不答得上


@dataclass
class JevCandidate:
    """喂给 Jev 判断的一个候选。只保留判断需要的三样东西。"""

    entry_id: str
    title: str
    text: str


def option_key(index: int) -> str:
    """Choice 的选项键。只用序号,不把 entry_id 送进选项名(无信息量)。"""
    return f"option_{index}"


# state 里给模型的字段名,与官方 cookbook 的做法一致(把待判断的成对材料
# 放进 state,问题保持字面简短——jaggedness 第 4 条:少跳一层是一层)。
QUESTION_FIELD = "question"
CANDIDATES_FIELD = "candidates"


def build_state(question: str, candidates: list[JevCandidate]) -> dict:
    """构造 Jev state。

    截断候选正文:jaggedness 第 5 条明确"state 里无关内容越多准确率越低",
    而候选正文可能很长。截断是**准确率**要求,不是省钱——输出本就免费。
    """
    return {
        QUESTION_FIELD: question,
        CANDIDATES_FIELD: [
            {"index": i, "title": c.title, "text": c.text}
            for i, c in enumerate(candidates)
        ],
    }


def build_questions(candidates: list[JevCandidate]) -> dict:
    """一次调用并行求值:一个 Choice(选) + 一个 Noul(该不该用)。

    刻意**不**问"要给几条资料"——jaggedness 第 2 条:jev-1.13 数数不可靠,长
    列表尤其糟。条数是代码根据概率算出来的结果,不是喂给模型的输入。
    """
    from .client import choice_question, noul_question

    criteria = {
        option_key(i): f"候选 {i}:{c.title}" for i, c in enumerate(candidates)
    }
    return {
        RANK_QID: choice_question(
            "Which single candidate best answers the user's question? "
            "Pick exactly one.",
            criteria,
        ),
        ANY_QID: noul_question(
            "Is the user's question answerable from at least one of the candidates?",
            true="At least one candidate directly answers what the user asked.",
            false_="No candidate addresses the specific question; they are only "
            "on the same broad topic.",
        ),
    }


@dataclass
class JevDecision:
    action: str
    entry_ids: tuple[str, ...] = ()
    # 记下两种答案的原始值,便于事后复盘阈值定得对不对
    nouls: dict[str, float] = field(default_factory=dict)
    probabilities: dict[str, float] = field(default_factory=dict)
    reason: str = ""


def _usable(result, n_needed: int) -> bool:
    """结果是否可用于决策。

    要求 Choice 与 Noul **都**答到,否则整体不可用。缺失一律当不可用,不用
    缺省值补:补 0.0 等于伪造"模型说不够格"。
    """
    if result is None or not result.ok:
        return False
    if result.noul(ANY_QID) is None:
        return False
    ans = result.choice(RANK_QID)
    if ans is None:
        return False
    return all(option_key(i) in ans.probabilities for i in range(n_needed))


def _ranked(
    candidates: list[JevCandidate], result
) -> list[tuple[float, str]]:
    """按 Choice 概率降序排候选。同分按 entry_id 兜底,保证确定性。"""
    ans = result.choice(RANK_QID)
    pairs = [(ans.probabilities[option_key(i)], c.entry_id) for i, c in enumerate(candidates)]
    pairs.sort(key=lambda p: (-p[0], p[1]))
    return pairs


def decide_takeover(
    candidates: list[JevCandidate],
    result,
    *,
    answerable_threshold: float,
    probability_threshold: float,
) -> JevDecision:
    """C 位点:我们自己答,还是交主 Agent。

    两个条件都要满足才自己答:材料**答得上**(Noul 过线),且**最对的那条**
    概率够高(Choice 过线)。前者是绝对门槛,后者是相对把握。
    """
    if not candidates:
        return JevDecision(ACTION_FALLBACK, reason="no_candidates")
    if not _usable(result, len(candidates)):
        return JevDecision(ACTION_FALLBACK, reason="jev_unusable")

    ans = result.choice(RANK_QID)
    ranked = _ranked(candidates, result)
    answerable = result.noul(ANY_QID)
    meta = dict(
        nouls=dict(result.nouls),
        probabilities={k: round(v, 4) for k, v in ans.probabilities.items()},
    )

    if answerable < answerable_threshold:
        return JevDecision(ACTION_HAND_OFF, reason="not_answerable", **meta)
    if ranked[0][0] < probability_threshold:
        # 材料沾边但没把握——此时任何断言都是猜测,交回主 Agent
        return JevDecision(ACTION_HAND_OFF, reason="low_confidence", **meta)
    return JevDecision(
        ACTION_ANSWER_SELF, entry_ids=(ranked[0][1],), reason="confident", **meta
    )


def decide_links(
    candidates: list[JevCandidate],
    result,
    *,
    answerable_threshold: float,
    probability_threshold: float,
    max_links: int,
) -> JevDecision:
    """D 位点:这轮要不要附链接、附哪几条。

    按 **Choice 概率**降序取够格的候选,而不是按本地分数降序。2026-09-15
    的事故根因正是"按检索顺序发":检索顺序是为模型那次检索词服务的,与用户
    原问题无关。
    """
    if not candidates:
        return JevDecision(ACTION_FALLBACK, reason="no_candidates")
    if not _usable(result, len(candidates)):
        return JevDecision(ACTION_FALLBACK, reason="jev_unusable")

    ans = result.choice(RANK_QID)
    meta = dict(
        nouls=dict(result.nouls),
        probabilities={k: round(v, 4) for k, v in ans.probabilities.items()},
    )
    if result.noul(ANY_QID) < answerable_threshold:
        return JevDecision(ACTION_FALLBACK, reason="not_answerable", **meta)

    kept = [p for p in _ranked(candidates, result) if p[0] >= probability_threshold]
    return JevDecision(
        ACTION_ANSWER_SELF,
        entry_ids=tuple(eid for _, eid in kept[:max_links]),
        reason="confident" if kept else "none_confident",
        **meta,
    )
