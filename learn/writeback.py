"""/learn 写回飞书文档(二期,行动 prompt v1.1 Phase 3)。

职责边界:
- 纯逻辑:写回 markdown 构造、目标文档查重、条目 ID 对齐;
- 实际写入由 adapter.append_doc_content 执行(一次完整 block 提交);
- 原子性语义:本地完整构造 → 本地校验 → 单次 append;
  失败时不继续后续内容、候选保留、不得预先标记已学习。

写回格式与 corpus parser 的结构约定严格对齐:
`### 【症状标签】标题` + 正文段落 —— 同步后 h3 被解析为 QA 条目,
ID 由 derive_entry_id 内容派生,因此新条目表现为"新增"而非全量变更。
"""

from __future__ import annotations

import hashlib
import re

from ..corpus.model import extract_symptom_tags, normalize_title

# 目标文档抓取(markdown)中的 h3 标题行
_H3_RE = re.compile(r"^\s*#{3}\s+(.+?)\s*$", re.MULTILINE)


def build_entry_markdown(record: dict) -> str:
    """把 pending 记录构造成完整的追加块(markdown,h3 标题 + 正文)。

    返回值以单个换行结尾;调用方将其作为一次 --command append 的全部内容。
    """
    tags = record.get("symptom_tags") or []
    tag_prefix = "".join(f"【{t}】" for t in tags if str(t).strip())
    title = str(record.get("question") or "").strip()
    answer = str(record.get("answer") or "").strip()
    return f"### {tag_prefix}{title}\n\n{answer}\n"


def _canonical_title(raw: str) -> str:
    """比对口径:先剥【症状标签】再 normalize_title 去序号前缀。"""
    _, stripped = extract_symptom_tags(raw)
    return normalize_title(stripped)


def extract_h3_titles(markdown: str) -> set[str]:
    """从目标文档的 markdown 抓取结果中提取归一化后的 h3 标题集合。

    文档条目标题带【标签】前缀而候选记录的 question 不含,
    统一经 _canonical_title 归一后再比对。
    """
    return {_canonical_title(m) for m in _H3_RE.findall(markdown or "")}


def is_duplicate_title(record: dict, existing_titles: set[str]) -> bool:
    """候选问题与目标文档既有条目标题重复(归一化后相等)。"""
    return (
        _canonical_title(str(record.get("question") or "")) in existing_titles
    )


def derive_record_key(record: dict) -> str:
    """待写入记录的稳定键(归一化问题文本哈希),用于跨会话重复保护。"""
    basis = hashlib.sha256(
        _canonical_title(str(record.get("question") or "")).encode("utf-8")
    ).hexdigest()[:16]
    return f"lrn_{basis}"


def duplicate_guard_result(
    record: dict,
    existing_titles: set[str],
    *,
    already_synced_keys: set[str] | None = None,
) -> str:
    """写回前查重,返回决策:``ok`` / ``already_exists``。

    双保险:
    1. 记录键已出现在历史同步成功的记录里 → 已存在;
    2. 标题与目标文档既有 h3 条目归一化相等 → 已存在。
    """
    if already_synced_keys and derive_record_key(record) in already_synced_keys:
        return "already_exists"
    if is_duplicate_title(record, existing_titles):
        return "already_exists"
    return "ok"
