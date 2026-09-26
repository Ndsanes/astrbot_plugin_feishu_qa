"""直答文本组装(spec §22/§46)。纯逻辑,不依赖 AstrBot。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..corpus.model import QaEntry, normalize_title
from ..storage.snapshot import SnapshotStore

SOURCE_ATTRIBUTION = "来源:肖闻 Xiaowenn 的 Q&A 文档"

# 模糊档措辞:命中 MEDIUM 区时使用。刻意**不含**"找到""直接命中""解答"等
# 断言词——链接是对用户的承诺,证据只到 MEDIUM 时只能给指引,不能给答案。
TENTATIVE_HEADER = "以下章节可能与你的问题相关(仅供参考,不一定是答案):"


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
    leading_newline: bool = False,
    url_on_newline: bool = False,
) -> list[str]:
    """条目列表 → 有序链接行。**全部链接渲染的唯一来源**。

    四个投递路径(高置信直答 / 自动附链 / LLM 工具投递 / 模糊档)此前各自
    复制了一份这段渲染逻辑,于是"标题自带序号前缀"这个缺陷需要改四处才
    修得干净,现在已经漏修过一轮。收敛到这里后,序号与括号处理只存一份。

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
        nl = "\n" if leading_newline else ""
        if markdown:
            safe_title = title.replace("[", "［").replace("]", "］")
            lines.append(f"{nl}{i}. [{safe_title}]({url})")
        elif url_on_newline:
            lines.append(f"{nl}{i}、{title}")
            lines.append(f"👉 {url}")
        else:
            lines.append(f"{nl}{i}、{title}👉 {url}")
    return lines


def format_tentative_links(
    entries: list[QaEntry],
    *,
    url_of,
    markdown: bool,
    attribution: str = SOURCE_ATTRIBUTION,
) -> DirectAnswer:
    """模糊档:只给章节指引,不给正文、不给图、不作断言。

    与 ``format_direct_answer`` 的区别是刻意的:
      - 不贴 ``entry.body``——正文投递等于在替用户下"这就是答案"的结论;
      - 不附图——图片是手册里逐步骤的截图,脱离正文语境更容易误导;
      - 措辞用 ``TENTATIVE_HEADER``,不含"找到/直接命中/解答"。
    中间档的定位是"给你个可能的方向",因此不追求完整性,只追求不越界。
    """
    lines = [TENTATIVE_HEADER, ""]
    lines += format_entry_link_lines(entries, url_of=url_of, markdown=markdown)
    lines.extend(["", f"> {attribution}"])
    return DirectAnswer(
        text="\n".join(lines).strip(),
        image_paths=[],
        entry_id=entries[0].id,
    )

