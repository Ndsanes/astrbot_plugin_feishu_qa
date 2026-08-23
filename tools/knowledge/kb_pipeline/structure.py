"""结构识别:把行级文本组装成带层级的章节树。

层级判定基于字号与正文主字号的相对关系(而非绝对值),
对不同排版的手册具有泛化性:
  body_size = 出现字符数最多的字号(即正文)
  其余更大字号按聚集分组为 L1/L2/L3 标题。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .extract import PageLines

# 常见章级编号模式(用于把 L1 归组,如 "Chapter 12" / "12 Recording")
_CHAPTER_NUM_RE = re.compile(r"^\s*(?:chapter\s+)?(\d{1,3})[\s:.]", re.IGNORECASE)


@dataclass(slots=True)
class Heading:
    level: int  # 1=chapter, 2=section, 3=subsection
    title: str
    page: int


@dataclass(slots=True)
class Block:
    """正文块:同一小节下的连续正文行。"""

    text: str
    page: int
    kind: str = "paragraph"  # paragraph | list | caption


@dataclass(slots=True)
class Section:
    """树形章节节点。level=0 表示文档根。"""

    level: int
    title: str
    page: int
    blocks: list[Block] = field(default_factory=list)
    children: list[Section] = field(default_factory=list)

    def walk(self) -> list[Section]:
        out = [self]
        for child in self.children:
            out.extend(child.walk())
        return out

    def breadcrumb(self, doc_title: str, include_root: bool = False) -> str:
        parts: list[str] = []
        node: Section | None = self
        while node is not None:
            if node.level > 0 and node.title:
                parts.append(node.title)
            node = node.parent
        parts.reverse()
        if include_root:
            parts.insert(0, doc_title)
        return " > ".join(parts) if parts else doc_title

    parent: Section | None = field(default=None, repr=False)


_BODY_MIN_CHARS = 4


def _body_size(pages: list[PageLines]) -> float:
    counter: Counter[float] = Counter()
    for page in pages:
        for line in page.lines:
            if len(line.text) >= _BODY_MIN_CHARS:
                counter[round(line.size, 1)] += len(line.text)
    if not counter:
        return 9.0
    return counter.most_common(1)[0][0]


def _heading_sizes(pages: list[PageLines], body_size: float) -> list[float]:
    """收集显著大于正文的字号,降序 → 映射到 L1..L3。"""
    counter: Counter[float] = Counter()
    for page in pages:
        for line in page.lines:
            if line.size > body_size + 0.5 and len(line.text) >= 2:
                counter[round(line.size, 1)] += len(line.text)
    distinct = sorted(counter, reverse=True)
    # 合并相近字号(±0.3)
    merged: list[float] = []
    for size in distinct:
        if merged and abs(size - merged[-1]) <= 0.3:
            continue
        merged.append(size)
    return merged[:3]  # 最多三级


def _size_to_level(size: float, heading_sizes: list[float]) -> int | None:
    for idx, hs in enumerate(heading_sizes):
        if abs(size - hs) <= 0.3:
            return idx + 1
    return None


_TOC_ENTRY_NUM_RE = re.compile(r"^\d{1,4}$")


def detect_toc_pages(pages: list[PageLines]) -> set[int]:
    """检测目录页:≥40% 的行为纯数字(目录的目标页码列)。

    目录在书中是连续区块;首尾向外各扩一页,
    覆盖目录封面页(如仅含 "Table of contents" 标题的页)。
    """
    raw = sorted(
        page.page
        for page in pages
        if page.lines
        and sum(
            1 for line in page.lines if _TOC_ENTRY_NUM_RE.match(line.text.strip())
        )
        / len(page.lines)
        >= 0.4
    )
    if not raw:
        return set()
    # 连续段聚类;每段向外扩 1 页覆盖目录封面页。
    # 不做全局 min/max 合并——书末索引页同样是数字密集型,不能与目录粘连。
    expanded: set[int] = set()

    def _add_range(a: int, b: int) -> None:
        for value in range(max(0, a - 1), b + 2):
            expanded.add(value)

    run_start = prev = raw[0]
    for page_no in raw[1:]:
        if page_no - prev <= 2:  # 允许目录内部的少量非检测页
            prev = page_no
            continue
        _add_range(run_start, prev)
        run_start = prev = page_no
    _add_range(run_start, prev)
    return expanded


def extract_toc(pages: list[PageLines], toc_pages: set[int]) -> list[tuple[int, str]]:
    """从目录页提取 (目标页码, 标题) 有序列表。

    目录行成对出现:前一行是纯数字(目标页码),后一行是标题。
    """
    entries: list[tuple[int, str]] = []
    pending_num: int | None = None
    for page in sorted(toc_pages):
        for line in pages[page].lines:
            text = line.text.strip()
            if _TOC_ENTRY_NUM_RE.match(text):
                if pending_num is not None and entries:
                    # 连续两个数字:上一个没有配对到标题,丢弃页码即可
                    pass
                pending_num = int(text)
                continue
            if not text:
                continue
            target = pending_num if pending_num is not None else -1
            entries.append((target, text))
            pending_num = None
    return entries


_SENTENCE_END_RE = re.compile(r"[.!?:]\"?\s*$")


def _to_paragraphs(lines: list[str]) -> list[str]:
    """视觉折行 → 语义段落:句末标点处断段,其余以空格拼接。"""
    paras: list[str] = []
    cur: list[str] = []
    for raw in lines:
        text = raw.strip()
        if not text:
            continue
        cur.append(text)
        if _SENTENCE_END_RE.search(text):
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return paras


def build_sections(
    pages: list[PageLines],
    *,
    doc_title: str = "",
    skip_toc: bool = True,
    toc_pages: set[int] | None = None,
) -> tuple[Section, list[Heading], float]:
    """构建文档章节树。

    Args:
        toc_pages: 调用方在**同一份清洗后数据**上预先检测的目录页集合;
            传 None 则内部自动检测。内外必须使用同一份数据以避免结果漂移。

    Returns:
        (root, headings, body_size)
        root.level == 0,root.title == doc_title。
    """
    if toc_pages is None:
        toc_pages = detect_toc_pages(pages)
    content_pages = [p for p in pages if p.page not in toc_pages] if skip_toc else pages
    body_size = _body_size(content_pages)
    heading_sizes = _heading_sizes(content_pages, body_size)

    root = Section(level=0, title=doc_title, page=0)
    root.parent = None
    headings: list[Heading] = []

    # 栈顶始终是当前最深挂载点
    stack: list[Section] = [root]

    def _attach(level: int, title: str, page_no: int) -> Section:
        # 回退栈直到父节点 level < 新 level
        while len(stack) > 1 and stack[-1].level >= level:
            stack.pop()
        parent = stack[-1]
        node = Section(level=level, title=title, page=page_no)
        node.parent = parent
        parent.children.append(node)
        stack.append(node)
        return node

    for page in content_pages:
        i = 0
        n = len(page.lines)
        while i < n:
            line = page.lines[i]
            level = _size_to_level(line.size, heading_sizes)
            if level is None or len(line.text) < 2:
                # 正文行:归入当前栈顶节点的 blocks。
                # 必须先消费当前行(可能是大字号单字符标签),否则死循环。
                consumed: list[str] = [line.text]
                start_page = line.page
                i += 1
                while i < n:
                    cur_line = page.lines[i]
                    if _size_to_level(cur_line.size, heading_sizes) is not None:
                        break
                    consumed.append(cur_line.text)
                    i += 1
                # 折行合并为语义段:遇到句末标点即断段
                for para_text in _to_paragraphs(consumed):
                    target = stack[-1]
                    is_step = bool(
                        re.match(r"^\s*\d{1,2}[.)]\s", para_text)
                    )
                    prev = target.blocks[-1] if target.blocks else None
                    # 连续编号段聚合为一个列表块,保证步骤序列不被拆散
                    if (
                        is_step
                        and prev is not None
                        and prev.kind == "list"
                        and prev.page >= start_page - 3
                    ):
                        prev.text += "\n\n" + para_text
                    else:
                        target.blocks.append(
                            Block(
                                text=para_text,
                                page=start_page,
                                kind="list" if is_step else "paragraph",
                            )
                        )
                continue

            # 标题行:合并连续同字号标题行(长标题折行)
            title_buf = [line.text]
            start_page = line.page
            i += 1
            while i < n:
                nxt = page.lines[i]
                if (
                    _size_to_level(nxt.size, heading_sizes) == level
                    and not nxt.text.endswith((".", ":", "?", "!"))
                ):
                    title_buf.append(nxt.text)
                    i += 1
                else:
                    break
            title = " ".join(title_buf).strip()
            _attach(level, title, start_page)
            headings.append(Heading(level=level, title=title, page=start_page))

    # 章级编号后处理:给 L1 补充 "Chapter N" 语义(若标题以数字开头)
    chapter_no = 0
    for h in headings:
        if h.level != 1:
            continue
        m = _CHAPTER_NUM_RE.match(h.title)
        if m:
            chapter_no = int(m.group(1))
        elif re.match(r"^[A-Z]", h.title):
            chapter_no += 1
            h.title = f"{chapter_no:02d} {h.title}"

    return root, headings, body_size
