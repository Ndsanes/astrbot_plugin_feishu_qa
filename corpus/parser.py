"""Corpus 解析器(spec §13-§14)。

输入:docs +fetch --doc-format xml --detail with-ids 的 XML 内容。
策略:xml.etree 解析(实测 fixture 可完整解析),顺序遍历顶层块,
h1→section,h2→category,h3→QA 条目;条目内所有 img(含 grid 嵌套)
按文档序绑定。异常结构降级为 warning + 保留原文,不抛异常中断。
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .model import QaEntry, QaImage, derive_entry_id, extract_symptom_tags

logger = logging.getLogger("feishu_qa.corpus")

_HEADING_TAGS = {"h1", "h2", "h3"}
# 正文清洗:飞书内部媒体/流链接整段剔除,保留锚文本
_INTERNAL_URL_RE = re.compile(
    r"https?://(?:internal-api-drive-stream\.(?:feishu\.cn|larksuite\.com)"
    r"|[a-z0-9-]+\.feishucdn\.com|open\.(?:feishu\.cn|larksuite\.com))"
    r"/[^\s)】」]*",
)


@dataclass
class ParseResult:
    entries: list[QaEntry] = field(default_factory=list)
    preamble: str = ""  # 首个 h3 之前的内容(如"说在最前面")
    diagnostics: list[str] = field(default_factory=list)

    @property
    def image_count(self) -> int:
        return sum(len(e.images) for e in self.entries)


def _clean_text(text: str) -> str:
    """清洗正文文本,保留代码/路径/快捷键/错误信息/标签(spec §14)。"""
    text = _INTERNAL_URL_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _block_lines(elem: ET.Element) -> list[str]:
    """把一个正文块转成行列表:p→一段;ol/ul→每 li 一行;grid→递归列内内容。"""
    lines: list[str] = []
    tag = elem.tag
    if tag in ("ol", "ul"):
        for li in elem.iter("li"):
            text = _clean_text("".join(li.itertext()))
            if text:
                lines.append(f"• {text}")
        return lines
    if tag == "p":
        text = _clean_text("".join(elem.itertext()))
        if text:
            lines.append(text)
        return lines
    if tag == "img":
        return lines  # 图片单独走绑定,不进正文
    # grid / 其他未知块:收集其中全部 p/li 文本与嵌套 img 由调用方 iter 处理
    for child in elem.iter():
        if child.tag == "p":
            text = _clean_text("".join(child.itertext()))
            if text:
                lines.append(text)
    return lines


def _entry_images(elem: ET.Element, revision: int) -> list[QaImage]:
    """收集块子树内全部 img(file_token 在 src 属性),保持文档序并去重。"""
    images: list[QaImage] = []
    seen_tokens: set[str] = set()
    for img in elem.iter("img"):
        token = (img.get("src") or "").strip()
        if not token or token in seen_tokens:
            continue
        seen_tokens.add(token)
        image_id = f"img_{token[:20]}"
        images.append(
            QaImage(image_id=image_id, file_token=token, name=img.get("name") or "")
        )
    return images


def parse_xml(xml_content: str, *, source_revision: int = -1) -> ParseResult:
    """解析飞书 DocxXML 为 QA 条目列表。

    异常标题结构(缺 h1/h2、空正文等)不中断:
    记入 diagnostics 并尽量保留原始内容(spec §13)。
    """
    result = ParseResult()
    try:
        root = ET.fromstring(f"<doc>{xml_content}</doc>")
    except ET.ParseError as exc:
        raise ValueError(f"XML 解析失败: {exc}") from exc

    section_path: list[str] = []  # [section(h1), category(h2)]
    current: QaEntry | None = None
    body_parts: list[str] = []
    pending_blocks: list[ET.Element] = []

    def flush() -> None:
        nonlocal current, body_parts, pending_blocks
        if current is None:
            # 首个 h3 之前的内容 → preamble
            for block in pending_blocks:
                result.preamble += "\n".join(_block_lines(block)) + "\n"
            pending_blocks = []
            return
        body = _clean_text("\n".join(body_parts))
        tags, stripped_title = extract_symptom_tags(current.raw_title)
        entry_id = derive_entry_id(
            current.section_path, stripped_title, current.source_locator
        )
        images = []
        for block in pending_blocks:
            images.extend(_entry_images(block, source_revision))
        current.id = entry_id
        current.symptom_tags = tags
        current.title = stripped_title
        current.body = body
        current.images = images
        current.source_revision = source_revision
        current.content_hash = current.compute_content_hash()
        if not body and not images:
            result.diagnostics.append(
                f"条目 {entry_id} 无正文且无图片(raw_title={current.raw_title!r})"
            )
        result.entries.append(current)
        current = None
        body_parts = []
        pending_blocks = []

    for elem in root:
        if elem.tag == "title":
            continue  # 文档标题不入 corpus 条目
        if elem.tag in _HEADING_TAGS:
            if current is not None:
                flush()
            heading_id = elem.get("id") or ""
            heading_text = _clean_text("".join(elem.itertext()))
            if elem.tag == "h1":
                section_path = [heading_text, ""]
            elif elem.tag == "h2":
                if section_path:
                    section_path[1] = heading_text
                else:
                    section_path = ["", heading_text]
                    result.diagnostics.append(
                        f"h2 出现在 h1 之前:{heading_text!r}(已保留)"
                    )
            else:  # h3 → 新条目
                current = QaEntry(
                    id="",  # flush 时派生
                    section_path=[p for p in section_path if p],
                    category=section_path[1] if len(section_path) > 1 else "",
                    symptom_tags=[],
                    title="",
                    raw_title=heading_text,
                    body="",
                    source_locator=heading_id,
                )
            continue
        # 正文块
        pending_blocks.append(elem)
        if current is not None:
            body_parts.extend(_block_lines(elem))

    if current is not None:
        flush()

    logger.info(
        "[corpus] 解析完成: entries=%d images=%d diagnostics=%d",
        len(result.entries),
        result.image_count,
        len(result.diagnostics),
    )
    return result
