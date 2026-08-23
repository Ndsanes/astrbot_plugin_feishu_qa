"""结构感知分块:章节树 → 带 breadcrumb 前缀的检索块。

规则(规格 §11/§14/§15):
- L1/L2 边界强制开新块;L3 及以下在 token 预算内合并
- 编号列表块视为原子,不在中间切断
- 超长小节按段落边界切分
- 每个最终块以 【Document > Chapter > Section > Subsection】 开头,
  该前缀进入 embedding 与 sparse 检索输入
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .structure import Section


def token_len(text: str) -> int:
    """近似 token 计数(英文 ~4 字符/token)。标注为 approximate(规格 §47)。"""
    return max(1, (len(text) + 3) // 4)


@dataclass(slots=True)
class Chunk:
    chunk_id: str  # sha256 前 12 位,由最终内容决定(deterministic)
    breadcrumb: str
    body: str
    page_start: int
    page_end: int
    tokens: int

    @property
    def content(self) -> str:
        """进入 embedding/sparse 的完整文本 = prefix + 正文。"""
        return f"【{self.breadcrumb}】\n\n{self.body}"


class _Buffer:
    """当前累积中的块。"""

    __slots__ = ("min_tokens", "max_tokens", "breadcrumb", "parts", "pages")

    def __init__(self, min_tokens: int, max_tokens: int) -> None:
        self.min_tokens = min_tokens
        self.max_tokens = max_tokens
        self.breadcrumb: str | None = None
        self.parts: list[str] = []
        self.pages: list[int] = []

    @property
    def tokens(self) -> int:
        return sum(token_len(p) for p in self.parts)

    def reset(self, breadcrumb: str) -> None:
        self.breadcrumb = breadcrumb
        self.parts = []
        self.pages = []

    def can_accept(self, breadcrumb: str, text: str) -> bool:
        if self.breadcrumb is None:
            return True
        if breadcrumb != self.breadcrumb and self.parts:
            return False  # 路径变化 → 必须开新块
        return self.tokens + token_len(text) <= self.max_tokens or (
            not self.parts  # 空块必须接受第一个原子(哪怕超限,保持原子性)
            or self.tokens < self.min_tokens // 2
        )

    def add(self, breadcrumb: str, text: str, page: int) -> None:
        if self.breadcrumb is None:
            self.reset(breadcrumb)
        self.parts.append(text.strip())
        self.pages.append(page)

    def take(self) -> tuple[str, list[str], list[int]] | None:
        if self.breadcrumb is None or not self.parts:
            return None
        out = (self.breadcrumb, self.parts, self.pages)
        self.breadcrumb = None
        self.parts = []
        self.pages = []
        return out


def _section_atoms(section: Section) -> list[tuple[bool, str]]:
    """小节 → 原子序列 [(是否硬原子, 文本)]。

    硬原子 = 编号列表/表格等不可再切的单元。
    """
    atoms: list[tuple[bool, str]] = []
    if section.title and section.level >= 3:
        atoms.append((False, section.title))
    for block in section.blocks:
        if block.kind == "list":
            atoms.append((True, block.text))
        else:
            for para in block.text.split("\n\n"):
                para = para.strip()
                if para:
                    atoms.append((False, para))
    for child in section.children:
        atoms.extend(_section_atoms(child))
    return atoms


_STEP_RE = None


def _split_list(text: str, max_tokens: int) -> list[str]:
    """把超长编号列表按步骤边界分组;每组 ≤ max_tokens。"""
    import re as _re

    item_starts = list(_re.finditer(r"^\s*\d{1,2}[.)]\s", text, _re.M))
    if len(item_starts) < 2:
        return [text]
    bounds = [m.start() for m in item_starts] + [len(text)]
    items = [text[bounds[i]:bounds[i + 1]].strip() for i in range(len(bounds) - 1)]
    groups: list[list[str]] = [[]]
    gt = 0
    for item in items:
        it = token_len(item)
        if groups[-1] and gt + it > max_tokens:
            groups.append([])
            gt = 0
        groups[-1].append(item)
        gt += it
    out = ["\n\n".join(g) for g in groups if g]
    return out if len(out) > 1 else [text]


def chunk_sections(
    root: Section,
    doc_title: str,
    *,
    min_tokens: int = 250,
    max_tokens: int = 500,
) -> list[Chunk]:
    """章节树 → Chunk 列表。确定性:同输入同输出。"""
    chunks: list[Chunk] = []
    buf = _Buffer(min_tokens, max_tokens)

    def flush() -> None:
        taken = buf.take()
        if taken is None:
            return
        bc, parts, pages = taken
        body = "\n\n".join(parts).strip()
        if not body:
            return
        # 内容可能在不同章节重复出现(手册常见),
        # 混入序号保证 id 唯一;序号本身确定性,不破坏 reproducibility
        seq = len(chunks)
        content = f"【{bc}】\n\n{body}"
        digest = hashlib.sha256(f"{seq}|{content}".encode()).hexdigest()[:12]
        chunks.append(
            Chunk(
                chunk_id=f"c{seq:04d}_{digest}",
                breadcrumb=bc,
                body=body,
                page_start=min(pages),
                page_end=max(pages),
                tokens=token_len(content),
            )
        )

    def push(breadcrumb: str, text: str, page: int, hard: bool) -> None:
        t = token_len(text)
        over = buf.tokens + t > max_tokens
        # 硬原子(编号列表)超限时也要开新块,但不切开它本身;
        # 非硬原子在缓冲已有内容时才允许触发分块。
        path_changed = breadcrumb != buf.breadcrumb
        should_split = path_changed or (over and (hard or buf.tokens >= min_tokens // 2))
        if buf.parts and should_split:
            flush()
        buf.add(breadcrumb, text, page)

    for chapter in root.children:
        flush()
        chapter_bc = f"{doc_title} > {chapter.title}"
        for section in chapter.walk():
            if section.level == 1:
                continue
            bc = f"{doc_title} > {section.breadcrumb(doc_title)}"
            _ = chapter_bc
            if section.level <= 2:
                flush()

            atoms: list[tuple[bool, str]] = []
            if section.title and section.level >= 3:
                atoms.append((False, section.title))
            for block in section.blocks:
                if block.kind == "list":
                    # 超过 max_tokens 的列表按编号步骤边界拆成多组,
                    # 每组仍是完整步骤序列(§15: 只允许在明确步骤边界切分)
                    groups = _split_list(block.text, max_tokens)
                    atoms.extend((True, g) for g in groups)
                else:
                    for para in block.text.split("\n\n"):
                        para = para.strip()
                        if para:
                            atoms.append((False, para))

            for hard, text in atoms:
                push(bc, text, section.page, hard)

    flush()
    return chunks
