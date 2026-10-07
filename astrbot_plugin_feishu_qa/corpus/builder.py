"""CorpusBuilder — 从解析结果构建可持久化 manifest(spec §15)。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from .parser import ParseResult


def build_manifest(
    parsed: ParseResult,
    *,
    revision_id: int,
    document_id: str,
    built_at: datetime | None = None,
    source: str = "",
) -> dict:
    """把解析结果组装为快照 manifest。

    图片本地路径由同步层下载后回填;此处先记录 file_token 与占位路径。
    ``source`` 为该分片的来源标识,会被盖到每个条目上——分片是单来源的,
    由分片层统一下发比让每个解析器各自记得填更不容易漏。
    """
    entries = []
    for e in parsed.entries:
        entry_dict = e.to_dict()
        if source:
            entry_dict["source"] = source
        for img in entry_dict["images"]:
            if not img["local_path"]:
                img["local_path"] = f"images/{img['image_id']}{_ext_for(img['name'])}"
        entries.append(entry_dict)

    corpus_payload = json.dumps(entries, ensure_ascii=False, sort_keys=True)
    content_hash = hashlib.sha256(corpus_payload.encode("utf-8")).hexdigest()

    return {
        "schema_version": 1,
        "revision_id": revision_id,
        "document_id": document_id,
        "built_at": (built_at or datetime.now(UTC)).isoformat(),
        "source": source,
        "entry_count": len(parsed.entries),
        "image_count": sum(len(e["images"]) for e in entries),
        "content_hash": content_hash,
        "preamble": parsed.preamble.strip(),
        "diagnostics": list(parsed.diagnostics),
        "entries": entries,
    }


def _ext_for(name: str) -> str:
    lowered = (name or "").lower()
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        if lowered.endswith(ext):
            return ext
    return ".png"


def manifest_content_hash(manifest: dict) -> str:
    """独立于 built_at 的内容指纹(幂等验证用,spec §66)。"""
    payload = json.dumps(manifest.get("entries", []), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def merge_manifests(
    slices: list[tuple[str, dict | None]],
    *,
    revision_id: int | None = None,
    built_at: datetime | None = None,
) -> dict:
    """把多个来源分片合并为一份快照(spec §15 的多来源扩展)。

    **分片是唯一真相**:每个来源只写自己的分片,合并结果每次由全部分片重算。
    2026-10-07 线上事故的根因正是没有这层——飞书同步与网页同步各自把整份
    ``corpus.json`` 整篇覆盖,于是每 12 小时两个来源轮流把对方抹掉(实测
    47 条与 76 条交替消失),问答流程随之在"只剩一半语料"和"网页整页塌成
    一条"之间摇摆。

    ``slices`` 顺序即优先级:ID 冲突时先到者胜。网页的 FAQ 部分本就是飞书
    文档的镜像(同标题、同章节, ``derive_entry_id`` 只取章节首段+标题,故两边
    同 ID),飞书那份是人工维护的精选原文且图片已落地,故排在前面。
    """
    entries: list[dict] = []
    seen_ids: set[str] = set()
    sources: dict[str, dict] = {}
    revisions: list[int] = []

    for name, manifest in slices:
        if not manifest:
            continue
        kept = 0
        for entry in manifest.get("entries", []):
            entry_id = entry.get("id")
            if not entry_id or entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)
            entries.append(entry)
            kept += 1
        if isinstance(manifest.get("revision_id"), int):
            revisions.append(manifest["revision_id"])
        sources[name] = {
            "entry_count": len(manifest.get("entries", [])),
            "kept": kept,
            "deduped": len(manifest.get("entries", [])) - kept,
            "revision_id": manifest.get("revision_id"),
            "built_at": manifest.get("built_at"),
        }

    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True)
    merged_revision = revision_id
    if merged_revision is None:
        merged_revision = max(revisions) if revisions else -1
    return {
        "schema_version": 1,
        "revision_id": merged_revision,
        "document_id": "merged",
        "built_at": (built_at or datetime.now(UTC)).isoformat(),
        "source": "merged",
        "sources": sources,
        "entry_count": len(entries),
        "image_count": sum(len(e.get("images", [])) for e in entries),
        "content_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "preamble": "",
        "diagnostics": [],
        "entries": entries,
    }


def _entry_signature(entry: dict) -> str:
    """条目完整内容签名(不信任存量 content_hash 字段)。

    含 ``source``:同一 ID 的条目可能因来源优先级换手(网页条目被飞书条目
    抢占),那是实质变化,必须报成 updated 而不是静默不变。
    """
    payload = json.dumps(
        {
            k: entry.get(k)
            for k in ("section_path", "title", "raw_title", "body", "images", "source")
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diff_manifests(old: dict | None, new: dict) -> dict:
    """比较新旧 manifest,输出条目级变更摘要(通知用)。"""
    if not old:
        return {
            "added": len(new["entries"]),
            "updated": 0,
            "removed": 0,
            "changed": True,
        }
    old_by_id = {e["id"]: e for e in old.get("entries", [])}
    new_by_id = {e["id"]: e for e in new["entries"]}
    added = [i for i in new_by_id if i not in old_by_id]
    removed = [i for i in old_by_id if i not in new_by_id]
    updated = [
        i
        for i in new_by_id
        if i in old_by_id
        and _entry_signature(old_by_id[i]) != _entry_signature(new_by_id[i])
    ]
    return {
        "added": len(added),
        "updated": len(updated),
        "removed": len(removed),
        "changed": bool(added or removed or updated),
        "added_titles": [new_by_id[i]["raw_title"][:40] for i in added[:10]],
        "updated_titles": [new_by_id[i]["raw_title"][:40] for i in updated[:10]],
    }
