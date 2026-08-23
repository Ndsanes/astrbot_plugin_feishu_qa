"""PDF 文本提取层(pymupdf)。

输出统一的"页 → 行"结构,保留每行的最大字体号,
供后续标题层级识别与页眉/页脚统计使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Line:
    """页面内的一行文本。"""

    page: int  # 0-based 页码
    text: str
    size: float  # 行内最大字号
    y: float  # 页面纵向位置(用于 header/footer 判定)


@dataclass(slots=True)
class PageLines:
    page: int
    lines: list[Line] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


def extract_pages(pdf_path: str | Path) -> list[PageLines]:
    """提取全部页面的行级文本(带字号与位置)。"""
    import pymupdf

    doc = pymupdf.open(pdf_path)
    try:
        pages: list[PageLines] = []
        for pno, page in enumerate(doc):
            pl = PageLines(page=pno)
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    spans = [s for s in line.get("spans", []) if s["text"].strip()]
                    if not spans:
                        continue
                    text = "".join(s["text"] for s in spans).strip()
                    if not text:
                        continue
                    pl.lines.append(
                        Line(
                            page=pno,
                            text=text,
                            size=max(s["size"] for s in spans),
                            y=line["bbox"][1],
                        )
                    )
            pages.append(pl)
        return pages
    finally:
        doc.close()


def page_texts(pdf_path: str | Path) -> list[str]:
    """纯文本逐页提取(baseline 用,模拟 pdftotext 式扁平输出)。"""
    import pymupdf

    doc = pymupdf.open(pdf_path)
    try:
        return [page.get_text("text") for page in doc]
    finally:
        doc.close()
