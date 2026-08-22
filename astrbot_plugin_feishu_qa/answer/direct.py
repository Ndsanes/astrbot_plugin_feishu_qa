"""直答文本组装(spec §22/§46)。纯逻辑,不依赖 AstrBot。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..corpus.model import QaEntry
from ..storage.snapshot import SnapshotStore

SOURCE_ATTRIBUTION = "来源:肖闻 Xiaowenn 的 Q&A 文档"


@dataclass
class DirectAnswer:
    """高置信直答的完整载荷:文本 + 图片绝对路径列表。"""

    text: str
    image_paths: list[str]
    entry_id: str


def _resolve_image_path(store: SnapshotStore, img) -> Path:
    """解析图片绝对路径;local_path 为空时按构建期约定回退。"""
    rel = img.local_path or f"images/{img.image_id}.png"
    return store.image_path(rel)


def format_direct_answer(
    entry: QaEntry,
    *,
    store: SnapshotStore | None = None,
    max_images: int = 3,
) -> DirectAnswer:
    """组装直答:标题 + 原文 + 图片 + 来源(spec §80 的最终体验)。"""
    tags_prefix = "".join(f"【{t}】" for t in entry.symptom_tags)
    lines = ["找到一个相关问题:", "", f"{tags_prefix}{entry.title}", "", entry.body]

    image_paths: list[str] = []
    if store is not None:
        for img in entry.images[:max_images]:
            path = _resolve_image_path(store, img)
            if path.is_file():
                image_paths.append(str(path))

    lines.extend(["", SOURCE_ATTRIBUTION])
    return DirectAnswer(
        text="\n".join(lines).strip(),
        image_paths=image_paths,
        entry_id=entry.id,
    )


def format_miss_reply() -> str:
    """LOW 置信的兜底话术(不编造结论,spec §21/§26)。"""
    return (
        "没有在 Q&A 文档里找到足够相关的问题。\n"
        "可以换个说法试试,或用 /问 <关键词> 描述具体报错信息。"
    )
