"""噪声清理:重复页眉/页脚检测与剔除(统计规则,非硬编码字符串)。"""

from __future__ import annotations

import re
from collections import Counter

from .extract import PageLines

# 页眉/页脚候选判定参数
_MIN_REPEAT_RATIO = 0.15  # 同一文本在 ≥15% 的页面同位置重复出现 → 视为页眉/页脚
_MAX_CANDIDATE_LEN = 80  # 过长的行不可能是页眉
_TOP_FRACTION = 0.12  # 页面顶部区域(高度占比)
_BOTTOM_FRACTION = 0.88  # 页面底部区域
_PAGE_NUM_RE = re.compile(r"^\s*(?:page\s*)?\d{1,4}\s*$", re.IGNORECASE)
_BANNER_RE = re.compile(r"^\s*=+\s*PAGE\s+\d+\s*=+\s*$", re.IGNORECASE)


def _zone_candidates(pages: list[PageLines], *, top: bool) -> Counter:
    """收集顶部或底部区域的候选行文本。"""
    counter: Counter = Counter()
    for page in pages:
        if not page.lines:
            continue
        ys = [line.y for line in page.lines]
        height = max(ys) - min(ys) + 40.0
        lo = min(ys)
        for line in page.lines:
            rel = (line.y - lo) / height
            in_zone = rel <= _TOP_FRACTION if top else rel >= _BOTTOM_FRACTION
            if (
                in_zone
                and 0 < len(line.text) <= _MAX_CANDIDATE_LEN
            ):
                counter[line.text.strip()] += 1
    return counter


def detect_repeated_lines(
    pages: list[PageLines],
    *,
    min_repeat_ratio: float = _MIN_REPEAT_RATIO,
) -> set[str]:
    """返回被判定为页眉/页脚的行文本集合。

    规则:同一短文本在大量页面的顶部/底部反复出现。
    正文中的 "see page 555" 这类引用不会被误删——它们不在固定区域、
    也不以高比例重复。
    """
    total = max(1, len(pages))
    banned: set[str] = set()
    for top in (True, False):
        for text, count in _zone_candidates(pages, top=top).items():
            if count >= max(3, int(total * min_repeat_ratio)):
                banned.add(text)
    return banned


def strip_banners(text_lines: list[str]) -> list[str]:
    """剥离 '===== PAGE N ====' 形态的页码横幅(旧导出产物)。"""
    return [line for line in text_lines if not _BANNER_RE.match(line)]


def clean_pages(
    pages: list[PageLines],
    *,
    min_repeat_ratio: float = _MIN_REPEAT_RATIO,
) -> tuple[list[PageLines], dict]:
    """清除页眉/页脚/独立页码行;返回 (清理后页面, 统计信息)。"""
    banned = detect_repeated_lines(pages, min_repeat_ratio=min_repeat_ratio)

    removed = {"header_footer": 0, "page_number": 0}
    cleaned: list[PageLines] = []
    for page in pages:
        ys = [line.y for line in page.lines]
        lo = min(ys) if ys else 0.0
        height = (max(ys) - lo + 40.0) if ys else 1.0
        kept = []
        for line in page.lines:
            text = line.text.strip()
            if text in banned:
                removed["header_footer"] += 1
                continue
            # 页码只在页面顶部/底部区域删除;
            # 目录页中部的页码引用列是有效内容,必须保留
            rel = (line.y - lo) / height
            in_margin_zone = rel <= _TOP_FRACTION or rel >= _BOTTOM_FRACTION
            if in_margin_zone and _PAGE_NUM_RE.match(text):
                removed["page_number"] += 1
                continue
            kept.append(line)
        cleaned.append(PageLines(page=page.page, lines=kept))

    stats = {
        "banned_texts": sorted(banned),
        "removed_lines": removed,
    }
    return cleaned, stats
