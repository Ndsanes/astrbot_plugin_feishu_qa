"""直答文本组装(spec §22/§46)。纯逻辑,不依赖 AstrBot。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..corpus.model import QaEntry, normalize_title
from ..storage.snapshot import SnapshotStore

SOURCE_ATTRIBUTION = "来源:肖闻 Xiaowenn 的 Q&A 文档"

# 用户可见链接消息的**唯一**头部/尾部。
#
# 只声明"相关",不声明"是答案"——两者不是一回事,而把"相关"说成"答案"正是
# 2026-09-15 事故的形态(承诺了"你的问题在这里有答案"却给错章节)。
# 但也**不加"仅供参考/不一定是答案"这类免责**:是否放行由 Jev 置信闸门决定
# (answerable>=0.50 且最对候选 P>=0.35),文案再兜一遍是把同一件事说两遍。
# 闸门管安全,文案管表达。
LINK_LIST_HEADER = "以下章节与你的问题相关:"


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
    include_attribution: bool = True,
) -> DirectAnswer:
    """组装直答:标题 + 原文 + 图片 + 来源(spec §80 的最终体验)。

    include_attribution=False 用于多条目合并投递:署名由调用方统一追加一次。
    """
    tags_prefix = "".join(f"【{t}】" for t in entry.symptom_tags)
    lines = [f"{tags_prefix}{entry.title}", "", entry.body]

    image_paths: list[str] = []
    if store is not None:
        for img in entry.images[:max_images]:
            path = _resolve_image_path(store, img)
            if path.is_file():
                image_paths.append(str(path))

    if include_attribution:
        lines = ["找到一个相关问题:", "", *lines, "", SOURCE_ATTRIBUTION]
    return DirectAnswer(
        text="\n".join(lines).strip(),
        image_paths=image_paths,
        entry_id=entry.id,
    )


def format_entry_link_lines(
    entries: list[QaEntry],
    *,
    url_of,
    markdown: bool,
) -> list[str]:
    """条目列表 → 有序链接行。**全部链接渲染的唯一来源**。

    四个投递路径(高置信直答 / 自动附链 / LLM 工具投递 / 模糊档)此前各自
    复制了一份渲染逻辑,而且**连头部和尾部都不一样**——线上一次回答里同时
    出现三种格式。现在头部/条目/尾部全部收口到 ``format_link_list``。

    ``raw_title`` 自带 "1、"/"2.1、" 序号前缀(UP 主在文档里手工编号,会
    随增删条目漂移)。直接拼进有序列表会渲染成 "1. [1、【xxx】](url)" 的
    重复编号,非 markdown 分支还会嵌套成 "1、【1、【xxx】】"。故一律用
    ``normalize_title`` 去序号,症状标签保留——它对用户识别条目有帮助。

    纯文本分支**不再**给标题套一层 【】:标题本身多以症状标签 【xxx】 开头,
    再包一层会渲染成 【【xxx】…】 的嵌套括号。症状标签已经提供了视觉分隔。

    ``url_of`` 由调用方注入(飞书锚点 URL 依赖插件配置,不放进纯逻辑层)。
    """
    lines: list[str] = []
    for i, entry in enumerate(entries, 1):
        url = url_of(entry)
        title = normalize_title(entry.raw_title)
        if markdown:
            safe_title = title.replace("[", "［").replace("]", "］")
            lines.append(f"{i}. [{safe_title}]({url})")
        else:
            # 标题本身多以症状标签 【xxx】 开头,再套一层会渲染成 【【xxx】…】。
            lines.append(f"{i}、{title}👉 {url}")
    return lines


def format_link_list(
    entries: list[QaEntry],
    *,
    url_of,
    markdown: bool,
    attribution: str = SOURCE_ATTRIBUTION,
) -> DirectAnswer:
    """章节链接列表 —— **用户可见链接消息的唯一格式**。

    四条投递路径(高置信直答 / 模糊档 / LLM 工具投递 / 自动附链)现在共用
    同一套头部、条目与尾部。此前它们各有各的措辞,一次回答里能同时看到
    三种格式,用户无从判断"这是不是同一种东西"。

    措辞只声明"相关",不声明"是答案"——把"相关"说成"答案"正是 2026-09-15
    事故的形态(承诺了"你的问题在这里有答案"却给错章节)。也**不加免责
    话术**:是否放行已由 Jev 置信闸门决定(见 jev/policy.py),文案再兜一遍
    是把同一件事说两遍。**闸门管安全,文案管表达。**
    """
    lines = [LINK_LIST_HEADER, ""]
    lines += format_entry_link_lines(entries, url_of=url_of, markdown=markdown)
    lines.extend(["", f"> {attribution}"])
    return DirectAnswer(
        text="\n".join(lines).strip(),
        image_paths=[],
        entry_id=entries[0].id,
    )

