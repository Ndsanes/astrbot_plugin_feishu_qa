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
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .corpus.builder import build_manifest, manifest_content_hash, merge_manifests
from .corpus.model import QaEntry, QaImage, derive_entry_id, extract_symptom_tags
from .corpus.parser import ParseResult
from .storage.snapshot import SnapshotStore

logger = logging.getLogger("feishu_qa.fuuumusic")

FUUU_BASE = "https://www.fuuumusic.com"
QA_PAGE_URL = f"{FUUU_BASE}/cakewalk-sonar-faq/all"
SOURCE_NAME = "fuuumusic"
SOURCE_FEISHU = "feishu"

# 站点把问答截图统一放在这个前缀下;logo/装饰图不在其中。
QA_IMAGE_PREFIX = "/assets/img/qa/"

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
                if src.startswith(QA_IMAGE_PREFIX):
                    self._item["images"].append(urljoin(FUUU_BASE, src))
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


def parse_qa_document(
    html: str,
    *,
    page_url: str = QA_PAGE_URL,
    revision_id: int = -1,
    sections: list[str] | None = None,
) -> list[QaEntry]:
    """问答聚合页 → QaEntry 列表。

    ``sections`` 为章节名白名单(对应页面 ``<h2>`` 文本);空/None = 不过滤。
    条目 ID 与飞书解析器同源(章节首段 + 去标签标题),故网页里那份与飞书
    重复的 FAQ 会派生出**相同 ID**,交由 ``merge_manifests`` 去重。
    """
    parser = QaDocumentParser()
    parser.feed(html)
    parser.close()

    wanted = {_squash(s) for s in sections} if sections else None

    entries: list[QaEntry] = []
    for item in parser.items:
        top = item.section_path[0] if item.section_path else ""
        if wanted is not None and _squash(top) not in wanted:
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
    logger.info(
        "[fuuumusic] 解析完成: entries=%d images=%d sections=%s",
        len(entries),
        sum(len(e.images) for e in entries),
        "全部" if wanted is None else len(wanted),
    )
    return entries


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
    entries = parse_qa_document(html, revision_id=revision_id)
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
    print(f"[build] entries={manifest['entry_count']} images={ok}(失败 {failed})")

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
            ParseResult(entries=parse_qa_document(html, revision_id=revision_id)),
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
