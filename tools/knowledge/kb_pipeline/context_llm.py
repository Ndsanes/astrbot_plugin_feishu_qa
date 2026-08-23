"""Layer C —— LLM Contextual Enrichment(可选,离线预处理)。

设计(规格 §21-25):
- 后端可插拔:llm_call_fn 由调用方注入(会话内模型 / OpenAI 兼容 HTTP),
  本模块不绑定任何凭据来源。
- 缓存键 = sha256(document_hash + chunk_hash + prompt_version + model),
  重复运行零额外调用;增量修改只重算受影响块。
- 批量模式:同一章的多个 chunk 合并为一次请求,降低调用次数。

Prompt 约束(§23):只生成 1~2 句定位语境,不改写事实、不给方案、不做总结创作。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .chunker import Chunk, token_len  # noqa: E402

PROMPT_VERSION = "v1"

_SYSTEM = (
    "You are a retrieval-indexing assistant. For each numbered text chunk "
    "from a technical manual, write ONE short context sentence in the same "
    "language as the chunk that situates the chunk within its chapter. "
    "Rules: state what topic the chunk covers and where it belongs; do NOT "
    "summarize the full content; do NOT add facts not present; do NOT give "
    "solutions or advice. Output format: one line per chunk, exactly "
    "`<n>|<context sentence>`, nothing else."
)


def _chunk_key(doc_hash: str, chunk_id: str, model: str) -> str:
    raw = f"{doc_hash}|{chunk_hash_of(chunk_id)}|{PROMPT_VERSION}|{model}"
    return hashlib.sha256(raw.encode()).hexdigest()


def chunk_hash_of(chunk_id: str) -> str:
    return hashlib.sha256(chunk_id.encode()).hexdigest()[:16]


def document_hash(chunks_text: str) -> str:
    return hashlib.sha256(chunks_text.encode()).hexdigest()[:16]


class ContextCache:
    """JSON 文件缓存:key 为 sha256 复合键,value 为生成的 context 句。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, str] = {}
        if path.exists():
            self._data = json.loads(path.read_text(encoding="utf-8"))

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def put(self, key: str, value: str) -> None:
        self._data[key] = value

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=0), encoding="utf-8"
        )

    def __len__(self) -> int:
        return len(self._data)


@dataclass(slots=True)
class EnrichStats:
    total_chunks: int
    cache_hits: int
    llm_calls: int
    chunks_sent: int


def enrich_chunks(
    chunks: list,
    doc_hash: str,
    cache: ContextCache,
    model: str,
    llm_call_fn,
    *,
    batch_size: int = 10,
) -> tuple[dict[str, str], EnrichStats]:
    """为每个 chunk 生成 context 句。

    Args:
        chunks: kb_pipeline.chunker.Chunk 列表(须含 content 属性)
        doc_hash: 文档级哈希(内容变化时整体失效)
        cache: 持久化缓存
        model: 模型标识(进入缓存键)
        llm_call_fn: callable(system, user_prompt) -> str
        batch_size: 单次请求携带的 chunk 数

    Returns:
        ({chunk_id: context_sentence}, stats)
    """
    results: dict[str, str] = {}
    hits = 0
    calls = 0
    sent = 0

    pending: list[tuple[int, object]] = []
    for idx, chunk in enumerate(chunks):
        key = _chunk_key(doc_hash, chunk.chunk_id, model)
        cached = cache.get(key)
        if cached is not None:
            results[chunk.chunk_id] = cached
            hits += 1
        else:
            pending.append((idx, chunk))

    # 按 batch 分组请求
    for group_start in range(0, len(pending), batch_size):
        group = pending[group_start:group_start + batch_size]
        listing = "\n".join(
            f"<chunk n={i}>\n{chunk.content}\n</chunk>"
            for i, (_, chunk) in enumerate(group, 1)
        )
        user_prompt = (
            f"{listing}\n\n"
            f"Write the context line for each chunk {1}..{len(group)} now."
        )
        raw = llm_call_fn(_SYSTEM, user_prompt)
        calls += 1
        sent += len(group)

        parsed = _parse_reply(raw, len(group))
        for i, (_, chunk) in enumerate(group, 1):
            ctx = parsed.get(i)
            if ctx:
                results[chunk.chunk_id] = ctx
                cache.put(_chunk_key(doc_hash, chunk.chunk_id, model), ctx)
            else:
                # 解析失败兜底:退回纯 breadcrumb(等价于非 LLM 模式)
                results[chunk.chunk_id] = ""

    return results, EnrichStats(
        total_chunks=len(chunks),
        cache_hits=hits,
        llm_calls=calls,
        chunks_sent=sent,
    )


def apply_contexts(chunks: list, contexts: dict[str, str]) -> list[Chunk]:
    """把 context 句拼进块内容:context + 原 content。

    返回新 Chunk 列表(id 追加后缀以区分变体,deterministic)。
    """
    out = []
    for chunk in chunks:
        ctx = contexts.get(chunk.chunk_id, "")
        if ctx:
            new_content = f"{ctx}\n\n{chunk.content}"
            out.append(
                Chunk(
                    chunk_id=f"{chunk.chunk_id}_ctx",
                    breadcrumb=chunk.breadcrumb,
                    body=f"{ctx}\n\n{chunk.body}",
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    tokens=token_len(new_content),
                ),
            )
        else:
            out.append(chunk)
    return out


def _parse_reply(raw: str, expected: int) -> dict[int, str]:
    out: dict[int, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        head, _, ctx = line.partition("|")
        try:
            n = int(head.strip().lstrip("<").rstrip(">").strip())
        except ValueError:
            continue
        if 1 <= n <= expected and ctx.strip():
            out[n] = ctx.strip()
    return out
