"""fuuumusic.com(小闻的奇妙屋)Cakewalk Sonar 中文语料接入。

数据来源是站点自带的**问答聚合页** ``/cakewalk-sonar-faq/all``:该页把全部
问答渲染成 ``<details class="qa-item" id="qN" data-qtitle="【症状标签】问题？">``,
外层 ``<h2>``/``<h3>`` 分组,答案在 ``<div class="qa-a">`` 内(含 ``<figure><img>``)。
实测该页共 100 条 = 47 条 FAQ + 53 篇官方文档翻译,与站点 ``llms.txt`` 声明一致,
故它才是机器可读的规范入口;各条目的独立页面是该页的子集。

**2026-10-07 修正(线上事故)**:此前实现把整页当成**一条**条目导入,该条标题
里不含任何具体症状词,于是检索的"主题词支撑闸门"(``retrieval/scorer.py`` 的
``DISCRIMINATIVE_MIN_IDF``/``UNSUPPORTED_PENALTY``)判定它只是共享了领域高频词,
分数被压到 0.3 倍——用户按标题原话提问(仅改了表述)也匹配不到,只能交回主 Agent。
现在按站点自身的 ``details`` 结构拆成逐条条目,并复用飞书解析器的字段形态
(``section_path``/``category``/症状标签/``raw_title``/图片/``source_locator``),
使检索、置信闸门、Jev 判定、链接渲染全部无需改动即可工作。

**网络必须不阻塞事件循环**:插件零第三方依赖,没有 aiohttp,同步 ``urllib``
直接跑在事件循环里会把整台机器人卡住(实测每次同步停顿约 30 秒,期间普通
消息回复一起变慢)。所有抓取一律经 ``asyncio.to_thread`` 走线程。

**2026-10-24 修正(官方文档只有摘要)**:聚合页里那 53 条"官方文档"只是
**一段导语 + 「阅读全文」链接**,正文在各自的 ``/cakewalk-sonar-help/<slug>``
页面(``article.cw-article`` → ``div.cw-body``)。此前只抓聚合页,于是官方
文档只剩摘要,而且标题取的是 ``data-qtitle`` 里的英文原名,中文提问在主题词
闸门上压根匹配不到。现在额外抓每个官方页的**全文与中文标题**,按
``dateModified`` 记录更新,并用官方索引页 ``/cakewalk-sonar-help`` 兜底发现
聚合页之外的新译文;抓取失败的页面**保留聚合页摘要**作为降级,绝不因此丢条目。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .corpus.builder import build_manifest, manifest_content_hash, merge_manifests
from .corpus.model import (
    QaEntry,
    QaImage,
    derive_entry_id,
    extract_symptom_tags,
    normalize_section,
)
from .corpus.parser import ParseResult
from .storage.snapshot import SnapshotStore

logger = logging.getLogger("feishu_qa.fuuumusic")

FUUU_BASE = "https://www.fuuumusic.com"
QA_PAGE_URL = f"{FUUU_BASE}/cakewalk-sonar-faq/all"
# 官方文档译文索引页:聚合页只给摘要,全文在各自的 /cakewalk-sonar-help/<slug>。
HELP_INDEX_URL = f"{FUUU_BASE}/cakewalk-sonar-help"
HELP_SECTION = "Cakewalk 官方文档（中文翻译）"
SOURCE_NAME = "fuuumusic"
SOURCE_FEISHU = "feishu"

# 站点把问答截图统一放在这两个前缀下;logo/装饰图不在其中。
# ``qa`` = 问答条目截图,``cakewalk`` = 官方文档译文的配图。
QA_IMAGE_PREFIX = "/assets/img/qa/"
HELP_IMAGE_PREFIX = "/assets/img/cakewalk/"
_IMAGE_PREFIXES = (QA_IMAGE_PREFIX, HELP_IMAGE_PREFIX)

# 官方文档逐页抓取的并发度。53 页 ≈ 每页 10~30KB,6 路并发对站点足够客气,
# 整轮同步也在几十秒量级(同步跑在后台任务里,不阻塞事件循环)。
HELP_FETCH_CONCURRENCY = 6

_USER_AGENT = "Mozilla/5.0 (compatible; astrbot-plugin-feishu-qa)"
DEFAULT_TIMEOUT = 20.0

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
# 整棵子树都不承载问答内容(导航/页脚/脚本)。button 是站点的交互控件
# (如"展开全部"),其文本也不属于条目正文。
_SKIP_TAGS = {"script", "style", "nav", "footer", "header", "button"}

_HEADING_TAGS = ("h2", "h3")


# ── 网络(一律走线程,绝不阻塞事件循环) ──


def _fetch_bytes_sync(url: str, timeout: float) -> bytes:
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 站点固定为 https
        return resp.read()


async def fetch_bytes(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes | None:
    """抓取原始字节;失败返回 None(调用方决定降级方式)。"""
    try:
        return await asyncio.to_thread(_fetch_bytes_sync, url, timeout)
    except Exception as exc:
        logger.debug("[fuuumusic] 抓取失败 %s: %s", url, exc)
        return None


async def fetch_url(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """抓取文本;失败返回 None。"""
    raw = await fetch_bytes(url, timeout=timeout)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


# ── 解析 ──


@dataclass
class QaItem:
    """聚合页里的一条问答(尚未成型为 QaEntry)。"""

    anchor: str
    raw_title: str
    body: str
    images: list[str]
    section_path: list[str]
    # 官方文档译文条目在聚合页里只有一段摘要 + 「阅读全文」;这里记下目标页,
    # 供同步层用全文替换摘要。FAQ 条目为空串。
    help_url: str = ""
    # 站点自己的检索关键词(``data-qkw``)。官方文档的标题是正式译名,用户
    # 提问用的是口语("装不上""爆音""界面糊了"),这些词只在这里出现。
    keywords: list[str] = field(default_factory=list)


class QaDocumentParser(HTMLParser):
    """解析问答聚合页的一条条 ``<details class="qa-item">``。

    只用实现方自己的语义标记(class/id/data-qtitle)定位,**不猜正文结构**:
    条目边界、标题原文、章节归属全部来自站点标注,因此条目数与锚点天然稳定。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._skip_at: int | None = None
        self._h2 = ""
        self._h3 = ""
        self._heading: str | None = None
        self._heading_buf: list[str] = []
        self._item_at: int | None = None
        self._item: dict | None = None
        self._in_summary = False
        self._summary_span = ""
        self._summary_tag_buf: list[str] = []
        self._summary_rest_buf: list[str] = []
        self._block_open = False
        self._block_buf: list[str] = []
        self.items: list[QaItem] = []

    # ── 标签事件 ──

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_starttag(self, tag: str, attrs) -> None:
        attr = {k: (v or "") for k, v in attrs}
        if self._skip_at is not None:
            if tag not in _VOID_TAGS:
                self._depth += 1
            return
        if tag in _SKIP_TAGS:
            self._skip_at = self._depth
            self._depth += 1
            return

        if tag in _HEADING_TAGS and self._item is None:
            self._heading = tag
            self._heading_buf = []
        elif tag == "details" and "qa-item" in attr.get("class", ""):
            self._item_at = self._depth
            self._item = {
                "anchor": attr.get("id", ""),
                "qtitle": attr.get("data-qtitle", ""),
                "h2": self._h2,
                "h3": self._h3,
                "body": [],
                "images": [],
                "help_url": "",
                "qkw": attr.get("data-qkw", ""),
            }
            self._summary_tag_buf = []
            self._summary_rest_buf = []
        elif self._item is not None:
            if tag == "summary":
                self._in_summary = True
                self._summary_tag_buf = []
                self._summary_rest_buf = []
            elif tag == "img":
                src = attr.get("src", "")
                if src.startswith(_IMAGE_PREFIXES):
                    self._item["images"].append(urljoin(FUUU_BASE, src))
            elif tag == "a" and "qa-xref" in attr.get("class", ""):
                href = attr.get("href", "").strip()
                if href:
                    self._item["help_url"] = urljoin(FUUU_BASE, href)
            elif tag in ("p", "li", "figcaption"):
                self._block_open = True
                self._block_buf = []
            elif tag == "span" and self._in_summary:
                cls = attr.get("class", "")
                if "qa-num" in cls:
                    self._summary_span = "num"
                elif "qa-tag" in cls:
                    self._summary_span = "tag"

        if tag not in _VOID_TAGS:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag not in _VOID_TAGS:
            self._depth -= 1
            if self._skip_at is not None:
                if self._depth <= self._skip_at:
                    self._skip_at = None
                return
        if self._skip_at is not None:
            return

        if self._heading == tag:
            text = _squash("".join(self._heading_buf))
            if tag == "h2":
                self._h2, self._h3 = text, ""
            else:
                self._h3 = text
            self._heading = None
        elif tag == "span" and self._in_summary:
            self._summary_span = ""
        elif tag == "summary":
            self._in_summary = False
            self._summary_span = ""
        elif tag in ("p", "li", "figcaption") and self._block_open:
            self._flush_block()
        elif tag == "details" and self._item is not None and self._item_at == self._depth:
            self._finish_item()

    def handle_data(self, data: str) -> None:
        if self._skip_at is not None:
            return
        if self._heading is not None:
            self._heading_buf.append(data)
        elif self._in_summary:
            if self._summary_span == "num":
                return  # 站点自己的编号,不是标题的一部分
            if self._summary_span == "tag":
                self._summary_tag_buf.append(data)
            else:
                self._summary_rest_buf.append(data)
        elif self._item is not None and self._block_open:
            self._block_buf.append(data)

    # ── 落定 ──

    def _flush_block(self) -> None:
        text = _squash("".join(self._block_buf))
        self._block_open = False
        self._block_buf = []
        if text and self._item is not None:
            self._item["body"].append(text)

    def _finish_item(self) -> None:
        item = self._item
        self._item = None
        self._item_at = None
        if not item:
            return
        raw_title = item["qtitle"] or self._summary_title()
        if not raw_title:
            return
        section_path = [p for p in (item["h2"], item["h3"]) if p]
        self.items.append(
            QaItem(
                anchor=item["anchor"],
                raw_title=raw_title,
                body="\n\n".join(item["body"]),
                images=list(dict.fromkeys(item["images"])),  # 去重保序
                section_path=section_path,
                help_url=item["help_url"],
                keywords=item["qkw"].split(),
            )
        )

    def _summary_title(self) -> str:
        """data-qtitle 缺失时用 summary 文本重建:标签加回【】,编号丢弃。"""
        tag = _squash("".join(self._summary_tag_buf))
        rest = _squash("".join(self._summary_rest_buf))
        if tag and not rest.startswith(tag):
            return f"【{tag}】{rest}"
        return rest


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _to_image(url: str) -> QaImage:
    """网页图片 → QaImage(本地路径由 build_manifest 按文件名回填)。"""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    name = Path(urlparse(url).path).name or f"{digest}.webp"
    return QaImage(image_id=digest, file_token=url, name=name, local_path="")


# ── 官方文档译文(全文页) ──

# 正文容器:``div.cw-body``。目录(``nav.cw-toc``)、译者声明、原文链接与免责
# 声明都在容器之外,天然不会混进正文。
_HELP_BODY_CLASS = "cw-body"
_HELP_IMAGE_PREFIXES = (HELP_IMAGE_PREFIX, QA_IMAGE_PREFIX)
# 这些标签的**整棵子树**都不是正文:目录、脚本、交互按钮(如"展开查看全部")。
_HELP_SKIP_TAGS = {"script", "style", "nav", "footer", "header", "button", "summary"}
# 逐块落定的正文元素:每个 <p>/<li>/<hN> 成为独立段落,列表项可逐条检索。
_HELP_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "p", "li", "dt", "dd", "figcaption"}
_JSONLD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)


class HelpDocumentParser(HTMLParser):
    """解析官方文档译文页的 ``div.cw-body`` 正文。

    正文里有表格、折叠块(``details``)、标签页(``cw-tabs``)、callout 与配图。
    表格按行拼成 "单元格 | 单元格",标签页在每块前标出页签名(如【会员版】),
    保证"对比类"内容丢进检索后仍然可读——正文是给检索和 KB 用的纯文本。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._skip_at: int | None = None
        self._body_at: int | None = None
        self._title_tag: str | None = None
        self._title_buf: list[str] = []
        self._block_at: int | None = None
        self._block_buf: list[str] = []
        self._cell_at: int | None = None
        self._cell_buf: list[str] = []
        self._row_at: int | None = None
        self._row: list[str] = []
        self._tab_menu_at: int | None = None
        self._tab_label_buf: list[str] | None = None
        self._tab_labels: list[str] = []
        self._tab_index = 0
        self.title = ""
        self.blocks: list[str] = []
        self.images: list[str] = []

    # ── 标签事件 ──

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_starttag(self, tag: str, attrs) -> None:
        attr = {k: (v or "") for k, v in attrs}
        if self._skip_at is not None:
            if tag not in _VOID_TAGS:
                self._depth += 1
            return
        if tag in _HELP_SKIP_TAGS:
            if tag == "button" and self._tab_menu_at is not None:
                # 标签页的按钮文字是"免费版/会员版"这类页签名,需要留下。
                self._tab_label_buf = []
            else:
                self._skip_at = self._depth
            if tag not in _VOID_TAGS:
                self._depth += 1
            return

        if tag == "h1" and self._body_at is None:
            self._title_tag = tag
            self._title_buf = []
        elif tag == "div":
            cls = attr.get("class", "")
            if _HELP_BODY_CLASS in cls and self._body_at is None:
                self._body_at = self._depth
            elif self._body_at is not None and "cw-tabs-menu" in cls:
                self._tab_menu_at = self._depth
                self._tab_labels, self._tab_index = [], 0
            elif self._body_at is not None and "cw-tabpanel" in cls:
                label = (
                    self._tab_labels[self._tab_index]
                    if self._tab_index < len(self._tab_labels)
                    else ""
                )
                self._tab_index += 1
                if label:
                    self._flush_block()
                    self.blocks.append(f"【{label}】")
        elif self._body_at is not None:
            if tag in ("td", "th"):
                self._cell_at = self._depth
                self._cell_buf = []
            elif tag == "tr":
                self._row_at = self._depth
                self._row = []
            elif tag in _HELP_BLOCK_TAGS:
                if self._block_at is None:
                    self._block_at = self._depth
                    self._block_buf = []
                else:
                    self._block_buf.append(" ")  # 嵌套段落之间留空隙
            elif tag == "img":
                src = attr.get("src", "")
                if src.startswith(_HELP_IMAGE_PREFIXES):
                    self.images.append(urljoin(FUUU_BASE, src))
            elif tag == "br":
                self._break_line()

        if tag not in _VOID_TAGS:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag not in _VOID_TAGS:
            self._depth -= 1
            if self._skip_at is not None:
                if self._depth <= self._skip_at:
                    self._skip_at = None
                return
        if self._skip_at is not None:
            return

        if tag == "button" and self._tab_label_buf is not None:
            label = _squash("".join(self._tab_label_buf))
            self._tab_label_buf = None
            if label:
                self._tab_labels.append(label)
        elif tag == self._title_tag:
            self.title = _squash("".join(self._title_buf))
            self._title_tag = None
        elif self._block_at is not None and self._depth <= self._block_at:
            self._flush_block()
        elif self._cell_at is not None and self._depth <= self._cell_at:
            self._finish_cell()
        elif self._row_at is not None and self._depth <= self._row_at:
            self._finish_row()
        elif tag == "div":
            if self._tab_menu_at is not None and self._depth <= self._tab_menu_at:
                self._tab_menu_at = None
            if self._body_at is not None and self._depth <= self._body_at:
                self._body_at = None

    def handle_data(self, data: str) -> None:
        if self._skip_at is not None:
            return
        if self._tab_label_buf is not None:
            self._tab_label_buf.append(data)
            return
        if self._title_tag is not None:
            self._title_buf.append(data)
            return
        if self._body_at is None:
            return
        if self._block_at is not None:
            self._block_buf.append(data)
        elif self._cell_at is not None:
            self._cell_buf.append(data)

    # ── 落定 ──

    def _break_line(self) -> None:
        if self._block_at is not None:
            self._block_buf.append(" ")
        elif self._cell_at is not None:
            self._cell_buf.append(" ")

    def _flush_block(self) -> None:
        text = _squash("".join(self._block_buf))
        self._block_at = None
        self._block_buf = []
        if not text:
            return
        if self._cell_at is not None:
            self._cell_buf.append(text)  # 单元格里的段落并入该单元格
        else:
            self.blocks.append(text)

    def _finish_cell(self) -> None:
        text = _squash("".join(self._cell_buf))
        self._cell_at = None
        self._cell_buf = []
        if text:
            self._row.append(text)

    def _finish_row(self) -> None:
        self._row_at = None
        row, self._row = self._row, []
        if row:
            self.blocks.append(" | ".join(row))


def parse_help_metadata(html: str) -> dict[str, str]:
    """从页面 JSON-LD 取官方原文链接 / 分类 / 更新时间。

    站点自己把 ``dateModified``、``articleSection`` 与 ``translationOfWork``
    写进了 TechArticle,这是"官方说明书更新了"唯一可机读的信号:标题、正文、
    分类都改不动时,它仍然会动。
    """
    meta: dict[str, str] = {}
    for match in _JSONLD_RE.finditer(html):
        try:
            payload = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        nodes = payload.get("@graph") if isinstance(payload, dict) else None
        for node in nodes or ([payload] if isinstance(payload, dict) else []):
            if not isinstance(node, dict):
                continue
            if node.get("@type") not in ("TechArticle", "Article", "WebPage"):
                continue
            meta.setdefault("title", str(node.get("headline") or ""))
            meta.setdefault("section", str(node.get("articleSection") or ""))
            meta.setdefault("updated", str(node.get("dateModified") or ""))
            work = node.get("translationOfWork")
            if isinstance(work, dict) and work.get("url"):
                meta.setdefault("original", str(work["url"]))
    return {k: v for k, v in meta.items() if v}


def parse_help_document(
    html: str,
    *,
    page_url: str,
    revision_id: int = -1,
    section_top: str = HELP_SECTION,
    meta: dict[str, str] | None = None,
) -> QaEntry | None:
    """官方文档译文页 → QaEntry(全文)。结构异常时返回 None(调用方保留摘要)。"""
    parser = HelpDocumentParser()
    parser.feed(html)
    parser.close()
    meta = meta if meta is not None else parse_help_metadata(html)

    raw_title = _squash(parser.title) or _squash(meta.get("title", ""))
    if not raw_title or not parser.blocks:
        logger.warning(
            "[fuuumusic] 官方文档页结构异常,跳过(保留聚合页摘要): %s", page_url
        )
        return None

    category = _squash(meta.get("section", ""))
    section_path = [section_top] + ([category] if category else [])
    tags, stripped = extract_symptom_tags(raw_title)
    return QaEntry(
        id=derive_entry_id(section_path, stripped, page_url),
        section_path=section_path,
        category=category or section_top,
        symptom_tags=tags,
        title=stripped,
        raw_title=raw_title,
        body="\n\n".join(parser.blocks),
        images=[_to_image(u) for u in dict.fromkeys(parser.images)],
        source_locator=page_url,
        source_revision=revision_id,
        source=SOURCE_NAME,
    )


def _norm_url(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def discover_help_pages(html: str) -> list[str]:
    """列出页面里指向官方文档译文的链接(``/cakewalk-sonar-help/<slug>``)。"""
    seen: list[str] = []
    for match in re.finditer(r'href="(/cakewalk-sonar-help/[a-z0-9][a-z0-9-]*)"', html):
        url = _norm_url(urljoin(FUUU_BASE, match.group(1)))
        if url not in seen:
            seen.append(url)
    return seen


async def fetch_help_entries(
    urls: list[str],
    *,
    revision_id: int = -1,
    concurrency: int = HELP_FETCH_CONCURRENCY,
) -> dict[str, QaEntry]:
    """并发抓取官方文档译文全文,返回 {规范化 URL: 条目}。

    单页失败只丢这一页(聚合页摘要仍在),不影响整轮同步——与图片下载
    同样的"降级优先于失败"约定。
    """
    sem = asyncio.Semaphore(max(1, concurrency))
    out: dict[str, QaEntry] = {}
    updated: list[str] = []
    wanted = list(dict.fromkeys(_norm_url(u) for u in urls))

    async def one(url: str) -> None:
        async with sem:
            html = await fetch_url(url)
        if not html:
            logger.warning("[fuuumusic] 官方文档页抓取失败,保留摘要: %s", url)
            return
        meta = parse_help_metadata(html)
        entry = parse_help_document(
            html, page_url=url, revision_id=revision_id, meta=meta
        )
        if entry is None:
            return
        out[url] = entry
        if meta.get("updated"):
            updated.append(meta["updated"])

    await asyncio.gather(*(one(u) for u in wanted))
    logger.info(
        "[fuuumusic] 官方文档全文: 成功 %d/%d 最新原文更新=%s",
        len(out),
        len(wanted),
        max(updated) if updated else "未知",
    )
    return out


async def collect_help_entries(
    page_html: str,
    *,
    revision_id: int = -1,
) -> dict[str, QaEntry]:
    """聚合页 + 官方索引页 → 全部官方文档全文。

    聚合页的「阅读全文」链接已经覆盖当前 53 篇;索引页兜底"站点新增了译文
    但聚合页还没重生成"的情况(官方说明书更新后的典型形态)。索引页抓不到
    时只用聚合页链接,不让整轮同步失败。
    """
    urls = discover_help_pages(page_html)
    index_html = await fetch_url(HELP_INDEX_URL)
    if index_html:
        for url in discover_help_pages(index_html):
            if url not in urls:
                urls.append(url)
    else:
        logger.warning("[fuuumusic] 官方文档索引页抓取失败,只用聚合页里的链接")
    if not urls:
        logger.warning("[fuuumusic] 聚合页里没有任何官方文档链接(页面结构可能变了)")
        return {}
    return await fetch_help_entries(urls, revision_id=revision_id)


def parse_qa_document(
    html: str,
    *,
    page_url: str = QA_PAGE_URL,
    revision_id: int = -1,
    sections: list[str] | None = None,
    help_entries: dict[str, QaEntry] | None = None,
) -> list[QaEntry]:
    """问答聚合页 → QaEntry 列表。

    ``sections`` 为章节名白名单(对应页面 ``<h2>`` 文本);空/None = 不过滤。
    条目 ID 与飞书解析器同源(章节首段 + 去标签标题),故网页里那份与飞书
    重复的 FAQ 会派生出**相同 ID**,交由 ``merge_manifests`` 去重。

    ``help_entries`` 是官方文档全文 ``{URL: 条目}``:命中时用全文条目**替换**
    聚合页里的摘要条目(位置不变);没抓到的页面保留摘要作为降级;索引里存在
    但聚合页还没收录的新译文追加在末尾,这样官方说明书更新后站点新增的译文
    也能自动进来。
    """
    parser = QaDocumentParser()
    parser.feed(html)
    parser.close()

    # 章节名会随站点改版带/不带"一、"这类序号,故按去序号后的主题词比较。
    wanted = {normalize_section(_squash(s)) for s in sections} if sections else None

    entries: list[QaEntry] = []
    used_help: set[str] = set()
    for item in parser.items:
        top = item.section_path[0] if item.section_path else ""
        if wanted is not None and normalize_section(_squash(top)) not in wanted:
            continue
        if item.help_url and help_entries:
            key = _norm_url(item.help_url)
            full = help_entries.get(key)
            if full is not None:
                used_help.add(key)
                # 站点的检索关键词只挂在聚合页的 data-qkw 上,全文页没有;
                # 标题是正式译名,口语提问靠这些词才匹配得到。
                full.symptom_tags = list(dict.fromkeys(item.keywords))
                entries.append(full)
                continue
        tags, stripped = extract_symptom_tags(item.raw_title)
        locator = f"{page_url}#{item.anchor}" if item.anchor else page_url
        entries.append(
            QaEntry(
                id=derive_entry_id(item.section_path, stripped, locator),
                section_path=item.section_path,
                category=item.section_path[-1] if item.section_path else "",
                symptom_tags=tags,
                title=stripped,
                raw_title=item.raw_title,
                body=item.body,
                images=[_to_image(u) for u in item.images],
                source_locator=locator,
                source_revision=revision_id,
                source=SOURCE_NAME,
            )
        )

    if help_entries and (wanted is None or normalize_section(HELP_SECTION) in wanted):
        seen_ids = {e.id for e in entries}
        for key, entry in sorted(help_entries.items()):
            if key in used_help or entry.id in seen_ids:
                continue
            seen_ids.add(entry.id)
            entries.append(entry)

    logger.info(
        "[fuuumusic] 解析完成: entries=%d images=%d 官方全文=%d sections=%s",
        len(entries),
        sum(len(e.images) for e in entries),
        len(used_help),
        "全部" if wanted is None else len(wanted),
    )
    return entries


def sections_include_official(sections: list[str] | None) -> bool:
    """章节白名单是否包含官方文档译文(空 = 不过滤,即包含)。

    官方文档要逐页抓 53 个全文,管理员明确只要中文问答时不做这轮请求。
    """
    if not sections:
        return True
    target = normalize_section(HELP_SECTION)
    return any(normalize_section(str(s)) == target for s in sections)


def discover_sections(html: str) -> list[str]:
    """列出聚合页的全部章节名(``<h2>`` 文本,按页面顺序去重)。"""
    parser = QaDocumentParser()
    parser.feed(html)
    parser.close()
    seen: list[str] = []
    for item in parser.items:
        if item.section_path and item.section_path[0] not in seen:
            seen.append(item.section_path[0])
    return seen


def resolve_sections(configured, available: list[str]) -> list[str]:
    """把配置的章节白名单解析为页面上**真实存在**的章节名。

    配置项的键是章节名而不是稳定 ID,站点改版或旧版配置残留都会让它过期。
    实测(2026-10-07):线上保存的还是旧版目录名(``cakewalk-sonar-faq``),
    与新页面的章节名对不上,白名单命中 0 条——若据此导入,整个网页语料会被
    静默清零。故**过期的白名单一律退化为"不过滤"**:宁可多导入,不可默默
    少导入。返回空列表即"不过滤"。
    """
    wanted = {str(s).strip() for s in (configured or []) if str(s).strip()}
    if not wanted:
        return []
    return [s for s in available if s in wanted]


# ── 图片下载 ──


async def download_manifest_images(
    manifest: dict,
    data_root: Path,
    *,
    concurrency: int = 8,
) -> tuple[int, int]:
    """下载 manifest 里缺失的图片,返回 (成功, 失败)。

    必须收 manifest 而非 QaEntry:本地路径是 ``build_manifest`` 按
    ``image_id + 扩展名`` 回填的,拿条目拿不到同一套约定。
    已存在则短路;单张失败只计数,绝不影响语料可用性。
    """
    sem = asyncio.Semaphore(concurrency)
    images_dir = Path(data_root) / "images"
    state = {"ok": 0, "failed": 0}

    async def one(img: dict) -> None:
        target = Path(data_root) / str(img.get("local_path") or "")
        if not img.get("local_path"):
            state["failed"] += 1
            return
        if target.is_file() and target.stat().st_size > 0:
            state["ok"] += 1
            return
        async with sem:
            raw = await fetch_bytes(str(img.get("file_token") or ""))
        if not raw:
            state["failed"] += 1
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.tmp")
        tmp.write_bytes(raw)
        tmp.replace(target)
        state["ok"] += 1

    await asyncio.gather(
        *(one(img) for e in manifest.get("entries", []) for img in e.get("images", []))
    )
    if state["failed"]:
        logger.warning(
            "[fuuumusic] 图片下载: 成功 %d 失败 %d(失败不影响检索)",
            state["ok"],
            state["failed"],
        )
    else:
        logger.info("[fuuumusic] 图片下载完成: %d 张(images_dir=%s)", state["ok"], images_dir)
    return state["ok"], state["failed"]


# ── CLI(本地构建/验收用;线上走插件内同步) ──


async def _run_cli(data_root: Path, *, check_idempotent: bool) -> int:
    html = await fetch_url(QA_PAGE_URL)
    if not html:
        print("[ERROR] 无法获取问答聚合页")
        return 1

    revision_id = int(datetime.now(UTC).timestamp())
    help_entries = await collect_help_entries(html, revision_id=revision_id)
    entries = parse_qa_document(
        html, revision_id=revision_id, help_entries=help_entries
    )
    if not entries:
        print("[ERROR] 未解析出任何条目")
        return 1

    manifest = build_manifest(
        ParseResult(entries=entries),
        revision_id=revision_id,
        document_id="fuuumusic_qa",
        source=SOURCE_NAME,
    )
    ok, failed = await download_manifest_images(manifest, data_root)
    print(
        f"[build] entries={manifest['entry_count']} "
        f"官方全文={len(help_entries)} images={ok}(失败 {failed})"
    )

    store = SnapshotStore(data_root)
    store.save_slice(SOURCE_NAME, manifest)
    merged = merge_manifests(
        [
            (SOURCE_FEISHU, store.load_slice(SOURCE_FEISHU)),
            (SOURCE_NAME, store.load_slice(SOURCE_NAME)),
        ]
    )
    store.commit(merged)
    print(
        f"[merged] entries={merged['entry_count']} sources="
        f"{ {k: v['kept'] for k, v in merged['sources'].items()} }"
    )

    if check_idempotent:
        again = build_manifest(
            ParseResult(
                entries=parse_qa_document(
                    html, revision_id=revision_id, help_entries=help_entries
                )
            ),
            revision_id=revision_id,
            document_id="fuuumusic_qa",
            source=SOURCE_NAME,
        )
        h1, h2 = manifest_content_hash(manifest), manifest_content_hash(again)
        if h1 != h2:
            print("[FAIL] 幂等校验失败:两次构建 hash 不一致")
            return 1
        print(f"[ok] 幂等校验通过 hash={h1[:12]}…")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="从 fuuumusic.com 构建 Cakewalk Sonar 中文语料")
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument("--check-idempotent", action="store_true")
    args = ap.parse_args()
    data_root = args.data_root or Path(__file__).resolve().parent / "data" / "feishu_qa"
    return asyncio.run(_run_cli(data_root, check_idempotent=args.check_idempotent))


if __name__ == "__main__":
    raise SystemExit(main())
