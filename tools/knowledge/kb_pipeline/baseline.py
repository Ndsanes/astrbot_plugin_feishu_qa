"""baseline 分块:模拟当前实际方案(pdftotext 式扁平文本 + 定长切片)。

作为 benchmark 的对照组,尽可能复现"整本压平 + RecursiveCharacter(500,100)"
的现状行为。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .chunker import token_len
from .extract import page_texts


@dataclass(slots=True)
class BaselineChunk:
    chunk_id: str
    body: str
    tokens: int


def baseline_chunks(
    pdf_path: str,
    *,
    chunk_size_chars: int = 500,
    overlap_chars: int = 100,
) -> list[BaselineChunk]:
    """扁平文本 → 按字符数滑窗切块(AstrBot RecursiveCharacter 的近似)。"""
    raw = "\n".join(page_texts(pdf_path))
    # 剥掉独立页码行(baseline 也应至少做到这一步,否则页码噪声主导索引)
    lines = [
        line
        for line in raw.split("\n")
        if line.strip() and not _is_page_number(line)
    ]
    text = "\n".join(lines)
    step = max(1, chunk_size_chars - overlap_chars)
    out: list[BaselineChunk] = []
    start = 0
    seq = 0
    while start < len(text):
        end = min(len(text), start + chunk_size_chars)
        piece = text[start:end].strip()
        if piece:
            digest = hashlib.sha256(f"{seq}|{piece}".encode()).hexdigest()[:12]
            out.append(
                BaselineChunk(
                    chunk_id=f"b{seq:04d}_{digest}",
                    body=piece,
                    tokens=token_len(piece),
                )
            )
            seq += 1
        if end >= len(text):
            break
        start += step
    return out


def _is_page_number(line: str) -> bool:
    s = line.strip()
    return s.isdigit() and len(s) <= 4
