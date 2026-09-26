"""/learn 候选 QA 提取(spec §31-§38)。

纯逻辑模块:prompt 构建、严格 JSON 解析、查重。LLM 调用由 main.py 注入。
"""

from __future__ import annotations

import json
import re

from ..corpus.model import QaEntry

MAX_HISTORY_CHARS = 4000

_TRUNCATION_NOTE = "\n……(素材过长,中间部分已省略)……\n"

# QQ 官方机器人"合并转发聊天记录"的服务端转录标记(message_type=102)。
# 平台把整段对话预渲染成纯文本塞进 content,格式形如:
#   [群聊的聊天记录]
#   === 消息 1 ===
#   [消息内容] ...
#   [发送者] ...
# 因此插件无需解析任何转发组件——拿到 events.message_str 就是完整转录。
_CHAT_RECORD_MARKERS = ("[群聊的聊天记录]", "[聊天记录]", "=== 消息 1 ===")

LEARN_PROMPT_TEMPLATE = """你是 FAQ 语料整理助手。以下是群聊最近的消息记录。
请判断其中是否出现了「值得加入 FAQ 的新问答」:
- 必须是关于软件安装/使用/报错的实质性问答
- 玩笑、闲聊、未经验证的观点不算
- 只输出一个 JSON 对象,不要输出其他任何文字

消息记录:
{history}

输出格式:
{{"is_candidate": true/false,
  "question": "...",
  "answer": "...",
  "symptom_tags": ["最多3个"],
  "category": "无法确定时填 unknown",
  "evidence": ["支撑该问答的原始消息摘录"],
  "confidence": 0.0}}"""


def build_learn_prompt(history_texts: list[str]) -> str:
    """把素材组装为受限长度的提取 prompt(spec §34)。

    单条素材可能远超上限(如 QQ 合并转发的聊天记录转录,一条就是整段对话)。
    旧实现按"从最新往回累加、放不下就 break"处理,遇到超长单条会**整条丢弃**,
    结果 prompt 里素材区为空、模型无从判断——静默失败。现在改为首尾保留式截断:
    问句通常转录在开头、结论在结尾,砍掉任一端都会毁掉这条问答,因此
    按预算各留一半,中间用省略标记显式说明(模型不会把截断误当对话结束)。
    """
    kept: list[str] = []
    total = 0
    for line in reversed(history_texts):  # 从最新往回取
        remaining = MAX_HISTORY_CHARS - total
        if remaining <= 0:
            break
        if len(line) > remaining:
            kept.insert(0, _truncate_head_tail(line, remaining))
            total += remaining
            break
        kept.insert(0, line)
        total += len(line)
    return LEARN_PROMPT_TEMPLATE.format(history="\n".join(kept))


def _truncate_head_tail(text: str, budget: int) -> str:
    """按预算保留素材的首尾两段,中间以省略标记衔接。

    问/答分别位于转录两端,只留头会丢答案、只留尾会丢问题,故两端各留一半。
    """
    if budget <= len(_TRUNCATION_NOTE):
        return text[:budget]
    usable = budget - len(_TRUNCATION_NOTE)
    head = usable // 2
    tail = usable - head
    return text[:head] + _TRUNCATION_NOTE + text[len(text) - tail :]


def parse_candidate(raw: str) -> dict | None:
    """从 LLM 输出解析候选;非 JSON 或 is_candidate=false 返回 None。

    禁止自由文本直通(spec §35):必须完整匹配 schema 才算候选。
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    required = {"is_candidate", "question", "answer", "symptom_tags", "confidence"}
    if not isinstance(obj, dict) or not required.issubset(obj):
        return None
    if obj["is_candidate"] is not True:
        return None
    if not str(obj.get("question", "")).strip():
        return None
    if not str(obj.get("answer", "")).strip():
        return None
    obj.setdefault("category", "unknown")
    obj["question"] = str(obj["question"]).strip()
    obj["answer"] = str(obj["answer"]).strip()
    return obj


def format_candidate_display(candidate: dict) -> str:
    tags = "".join(f"【{t}】" for t in candidate.get("symptom_tags", []))
    return (
        f"发现可学习的新 QA:\n\n"
        f"{tags}{candidate['question']}\n\n"
        f"{candidate['answer']}\n\n"
        f"分类:{candidate.get('category', 'unknown')} "
        f"(置信 {candidate.get('confidence')})\n"
        f"回复 /learn ok 确认收录,/learn no 放弃。"
    )


def find_duplicate(
    candidate: dict, retriever, *, high_threshold: float | None = None
) -> QaEntry | None:
    """查重(spec §38):候选问题在现有语料中高置信命中 → 视为重复。

    返回命中的既有条目;无重复返回 None。
    """
    results = retriever.search(candidate["question"], top_k=1)
    if not results:
        return None
    top = results[0]
    threshold = high_threshold if high_threshold is not None else 9.0
    # 查重阈值比直答更宽松:标题高度相似即视为重复
    if top.score >= min(threshold, 7.0):
        return top.entry
    return None


def candidate_to_pending_record(candidate: dict, *, group_id: str, approved_by: str) -> dict:
    """生成待写入记录(隐藏元数据不进用户文档,spec §37)。"""
    return {
        "learned_from_group": group_id,
        "approved_by": approved_by,
        **candidate,
    }


def is_chat_record_transcript(text: str) -> bool:
    """判断一段文本是否为平台预渲染的"聊天记录"转录。

    官方 QQ 机器人把合并转发在服务端展开成纯文本(message_type=102),
    随 content 一起下发,因此这类素材在这里就能直接识别。
    """
    if not text:
        return False
    head = text[:200]
    return any(marker in head for marker in _CHAT_RECORD_MARKERS)


def extract_transcript(text: str) -> str:
    """从消息文本中剥出聊天记录转录本体(去掉 [At:xxx] 等前缀)。"""
    stripped = (text or "").strip()
    for marker in _CHAT_RECORD_MARKERS:
        idx = stripped.find(marker)
        if idx > 0:
            # 保留 marker 之前的 @ 前缀之后的部分,再去除行首的 [At:...] 噪音
            stripped = stripped[idx:]
            break
    return re.sub(r"^\s*\[At:[^\]]*\]\s*", "", stripped).strip()
