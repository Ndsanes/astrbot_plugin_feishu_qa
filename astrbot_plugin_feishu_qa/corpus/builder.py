"""CorpusBuilder — 从解析结果构建可持久化 manifest(spec §15)。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .parser import ParseResult


def build_manifest(
    parsed: ParseResult,
    *,
    revision_id: int,
    document_id: str,
    built_at: datetime | None = None,
) -> dict:
    """把解析结果组装为快照 manifest。

    图片本地路径由同步层下载后回填;此处先记录 file_token 与占位路径。
    """
    entries = []
    for e in parsed.entries:
        entry_dict = e.to_dict()
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
        "built_at": (built_at or datetime.now(timezone.utc)).isoformat(),
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


def _entry_signature(entry: dict) -> str:
    """条目完整内容签名(不信任存量 content_hash 字段)。"""
    payload = json.dumps(
        {
            k: entry.get(k)
            for k in ("section_path", "title", "raw_title", "body", "images")
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
