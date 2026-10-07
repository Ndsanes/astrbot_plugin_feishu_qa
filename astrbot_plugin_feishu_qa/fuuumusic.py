#!/usr/bin/env python3
"""从 fuuumusic.com 抓取 Cakewalk Sonar 中文资料并导入语料。

用法:
    python -m astrbot_plugin_feishu_qa.fuuumusic --data-root <dir>           # 抓取并构建快照
    python -m astrbot_plugin_feishu_qa.fuuumusic --check-idempotent          # 抓取两次比对 hash

退出码:0 成功;1 抓取或构建失败。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .corpus.builder import build_manifest, diff_manifests, manifest_content_hash
from .corpus.model import QaEntry, QaImage, derive_entry_id, extract_symptom_tags
from .storage.snapshot import SnapshotStore

FUUU_BASE = "https://www.fuuumusic.com"
SITEMAP_URL = f"{FUUU_BASE}/sitemap.xml"

# 只抓取 FAQ 和 help 页面（排除首页、merch、freebies 等）
FAQ_PATH_RE = re.compile(r"^/cakewalk-sonar-faq(/.*)?$")
HELP_PATH_RE = re.compile(r"^/cakewalk-sonar-help(/.*)?$")


@dataclass
class FuuumusicPage:
    """抓取到的单个页面。"""

    url: str
    title: str
    body: str
    images: list[str] = field(default_factory=list)  # 图片 URL 列表


class FuuumusicHTMLParser(HTMLParser):
    """解析 fuuumusic 页面 HTML，提取标题、正文和图片。"""

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.body_parts: list[str] = []
        self.images: list[str] = []
        self._in_title = False
        self._in_h1 = False
        self._in_p = False
        self._in_li = False
        self._current_text: list[str] = []
        self._skip_depth = 0  # 跳过 script/style 等

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "nav", "footer", "header"):
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
        elif tag == "p":
            self._in_p = True
            self._current_text = []
        elif tag == "li":
            self._in_li = True
            self._current_text = []
        elif tag == "img":
            src = dict(attrs).get("src", "")
            if src and src.startswith("/assets/img/qa/"):
                self.images.append(urljoin(FUUU_BASE, src))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "nav", "footer", "header"):
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        elif tag == "p":
            self._in_p = False
            text = "".join(self._current_text).strip()
            if text:
                self.body_parts.append(text)
            self._current_text = []
        elif tag == "li":
            self._in_li = False
            text = "".join(self._current_text).strip()
            if text:
                self.body_parts.append(f"• {text}")
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        elif self._in_h1:
            pass  # h1 标题单独处理
        elif self._in_p or self._in_li:
            self._current_text.append(data)

    def get_result(self) -> tuple[str, str, list[str]]:
        title = self.title.strip()
        # 去掉标题中的站点后缀
        title = re.sub(r"\s*[|｜]\s*Cakewalk Sonar FAQ\s*[|｜]\s*小闻的奇妙屋\s*$", "", title)
        title = re.sub(r"\s*[|｜]\s*小闻的奇妙屋\s*$", "", title)
        body = "\n\n".join(self.body_parts)
        # 去重图片
        seen: set[str] = set()
        unique_images: list[str] = []
        for img in self.images:
            if img not in seen:
                seen.add(img)
                unique_images.append(img)
        return title, body, unique_images


def discover_sections(xml_content: str) -> list[str]:
    """从 sitemap.xml 自动发现所有顶级目录。

    返回按字母排序的目录名列表，如 ["cakewalk-sonar", "cakewalk-sonar-faq", ...]。
    排除首页（/）。
    """
    root = ET.fromstring(xml_content)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sections: set[str] = set()
    for url_elem in root.findall(".//sm:url", ns):
        loc = url_elem.find("sm:loc", ns)
        if loc is None or not loc.text:
            continue
        path = urlparse(loc.text.strip()).path
        if path == "/" or not path:
            continue
        # 提取顶级目录名
        parts = path.strip("/").split("/")
        if parts and parts[0]:
            sections.add(parts[0])
    return sorted(sections)


def parse_sitemap(xml_content: str) -> list[str]:
    """解析 sitemap.xml，返回所有非首页页面的 URL。"""
    root = ET.fromstring(xml_content)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls: list[str] = []
    for url_elem in root.findall(".//sm:url", ns):
        loc = url_elem.find("sm:loc", ns)
        if loc is None or not loc.text:
            continue
        url = loc.text.strip()
        path = urlparse(url).path
        if path != "/" and path != "":
            urls.append(url)
    return urls


async def fetch_url(url: str, *, timeout: float = 10.0) -> str | None:
    """异步抓取 URL 内容，返回文本或 None。"""
    try:
        import aiohttp
    except ImportError:
        # 降级到同步 urllib
        return _fetch_url_sync(url, timeout=timeout)
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp,
        ):
            if resp.status != 200:
                return None
            return await resp.text()
    except Exception:
        return None


def _fetch_url_sync(url: str, *, timeout: float = 10.0) -> str | None:
    """同步抓取 URL（aiohttp 不可用时降级）。"""
    import urllib.request

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


async def fetch_all_pages(urls: list[str]) -> list[FuuumusicPage]:
    """并发抓取所有页面。"""
    semaphore = asyncio.Semaphore(5)  # 限制并发

    async def fetch_one(url: str) -> FuuumusicPage | None:
        async with semaphore:
            html = await fetch_url(url)
            if html is None:
                return None
            parser = FuuumusicHTMLParser()
            parser.feed(html)
            title, body, images = parser.get_result()
            if not title or not body:
                return None
            return FuuumusicPage(url=url, title=title, body=body, images=images)

    tasks = [fetch_one(url) for url in urls]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]


def page_to_entry(page: FuuumusicPage, *, revision_id: int) -> QaEntry:
    """把抓取到的页面转成 QaEntry。"""
    # 从 URL 提取分类
    path = urlparse(page.url).path
    if "/cakewalk-sonar-faq/" in path:
        section = "fuuumusic FAQ"
    elif "/cakewalk-sonar-help/" in path:
        section = "fuuumusic 帮助文档"
    else:
        section = "fuuumusic"

    # 提取症状标签
    tags, stripped_title = extract_symptom_tags(page.title)

    # 构建 section_path
    section_path = [section]

    # 派生 ID
    entry_id = derive_entry_id(section_path, page.title, source_locator=page.url)

    # 构建图片
    images: list[QaImage] = []
    for img_url in page.images:
        img_hash = hashlib.sha256(img_url.encode()).hexdigest()[:16]
        images.append(
            QaImage(
                image_id=img_hash,
                file_token=img_url,  # 用 URL 作为 token
                name=Path(urlparse(img_url).path).name,
                local_path=f"images/fuuumusic_{img_hash}.webp",
            )
        )

    return QaEntry(
        id=entry_id,
        section_path=section_path,
        category=section,
        symptom_tags=tags,
        title=stripped_title,
        raw_title=page.title,
        body=page.body,
        images=images,
        source_locator=page.url,
        source_revision=revision_id,
    )


async def download_image(url: str, target: Path) -> bool:
    """下载单张图片到 target 路径。"""
    try:
        html = await fetch_url(url)
        if html is None:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(html.encode("utf-8"))
        return True
    except Exception:
        return False


async def main() -> int:
    ap = argparse.ArgumentParser(description="从 fuuumusic.com 抓取 Cakewalk Sonar 中文资料")
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument("--check-idempotent", action="store_true")
    args = ap.parse_args()

    data_root = args.data_root or Path(__file__).resolve().parent / "data" / "feishu_qa"

    # 1. 解析 sitemap
    sitemap_xml = await fetch_url(SITEMAP_URL)
    if sitemap_xml is None:
        print("[ERROR] 无法获取 sitemap.xml")
        return 1

    urls = parse_sitemap(sitemap_xml)
    print(f"[sitemap] 发现 {len(urls)} 个 FAQ/help 页面")

    if not urls:
        print("[ERROR] sitemap 中没有找到 FAQ/help 页面")
        return 1

    # 2. 抓取所有页面
    pages = await fetch_all_pages(urls)
    print(f"[fetch] 成功抓取 {len(pages)}/{len(urls)} 个页面")

    if not pages:
        print("[ERROR] 没有成功抓取任何页面")
        return 1

    # 3. 转换为 QaEntry
    revision_id = int(datetime.now(UTC).timestamp())
    entries = [page_to_entry(p, revision_id=revision_id) for p in pages]

    # 4. 下载图片
    print("[images] 开始下载图片...")
    img_ok = 0
    img_fail = 0
    for entry in entries:
        for img in entry.images:
            target = data_root / img.local_path
            if target.is_file() and target.stat().st_size > 0:
                img_ok += 1
                continue
            success = await download_image(img.file_token, target)
            if success:
                img_ok += 1
            else:
                img_fail += 1
    print(f"[images] 下载完成: 成功 {img_ok}, 失败 {img_fail}")

    # 5. 构建 manifest
    from astrbot_plugin_feishu_qa.corpus.parser import ParseResult

    parsed = ParseResult(entries=entries)
    manifest = build_manifest(
        parsed, revision_id=revision_id, document_id="fuuumusic_com"
    )
    manifest["source"] = "fuuumusic.com"

    # 6. 提交快照
    store = SnapshotStore(data_root)
    old = store.load()
    diff = diff_manifests(old, manifest)

    if old is not None and old.get("content_hash") == manifest["content_hash"]:
        print(f"[unchanged] hash={manifest['content_hash'][:12]}…")
        if not args.check_idempotent:
            return 0
    else:
        store.commit(manifest)
        print(
            f"[built] entries={manifest['entry_count']} "
            f"images={manifest['image_count']} "
            f"hash={manifest['content_hash'][:12]}… "
            f"(added={diff['added']} updated={diff['updated']} removed={diff['removed']})"
        )

    # 7. 幂等校验
    if args.check_idempotent:
        pages2 = await fetch_all_pages(urls)
        entries2 = [page_to_entry(p, revision_id=revision_id) for p in pages2]
        parsed2 = ParseResult(entries=entries2)
        manifest2 = build_manifest(
            parsed2, revision_id=revision_id, document_id="fuuumusic_com"
        )
        h1, h2 = manifest_content_hash(manifest), manifest_content_hash(manifest2)
        if h1 != h2:
            print("[FAIL] 幂等校验失败:两次构建 hash 不一致")
            return 1
        print(f"[ok] 幂等校验通过 hash={h1[:12]}…")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
