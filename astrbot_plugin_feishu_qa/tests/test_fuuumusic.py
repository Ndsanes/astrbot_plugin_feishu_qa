"""fuuumusic 抓取与解析测试。"""

from __future__ import annotations

from astrbot_plugin_feishu_qa.fuuumusic import (
    FuuumusicHTMLParser,
    FuuumusicPage,
    discover_sections,
    page_to_entry,
    parse_sitemap,
)


class TestParseSitemap:
    def test_extracts_all_non_homepage_urls(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.fuuumusic.com/</loc></url>
  <url><loc>https://www.fuuumusic.com/cakewalk-sonar-faq</loc></url>
  <url><loc>https://www.fuuumusic.com/cakewalk-sonar-faq/login-activate</loc></url>
  <url><loc>https://www.fuuumusic.com/cakewalk-sonar-help</loc></url>
  <url><loc>https://www.fuuumusic.com/cakewalk-sonar-help/how-to-export</loc></url>
  <url><loc>https://www.fuuumusic.com/merch</loc></url>
  <url><loc>https://www.fuuumusic.com/freebies</loc></url>
</urlset>"""
        urls = parse_sitemap(xml)
        assert len(urls) == 6
        assert "https://www.fuuumusic.com/cakewalk-sonar-faq" in urls
        assert "https://www.fuuumusic.com/cakewalk-sonar-faq/login-activate" in urls
        assert "https://www.fuuumusic.com/cakewalk-sonar-help" in urls
        assert "https://www.fuuumusic.com/cakewalk-sonar-help/how-to-export" in urls
        assert "https://www.fuuumusic.com/merch" in urls
        assert "https://www.fuuumusic.com/freebies" in urls
        # 排除首页
        assert "https://www.fuuumusic.com/" not in urls

    def test_empty_sitemap(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
</urlset>"""
        assert parse_sitemap(xml) == []

    def test_discover_sections(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.fuuumusic.com/</loc></url>
  <url><loc>https://www.fuuumusic.com/cakewalk-sonar</loc></url>
  <url><loc>https://www.fuuumusic.com/cakewalk-sonar-faq</loc></url>
  <url><loc>https://www.fuuumusic.com/cakewalk-sonar-faq/login-activate</loc></url>
  <url><loc>https://www.fuuumusic.com/cakewalk-sonar-help</loc></url>
  <url><loc>https://www.fuuumusic.com/merch</loc></url>
  <url><loc>https://www.fuuumusic.com/freebies</loc></url>
</urlset>"""
        sections = discover_sections(xml)
        assert sections == [
            "cakewalk-sonar",
            "cakewalk-sonar-faq",
            "cakewalk-sonar-help",
            "freebies",
            "merch",
        ]


class TestFuuumusicHTMLParser:
    def test_parses_title_and_body(self) -> None:
        html = """<!DOCTYPE html>
<html>
<head><title>Cakewalk 如何登录激活？ | Cakewalk Sonar FAQ | 小闻的奇妙屋</title></head>
<body>
<nav>导航栏</nav>
<h1>Cakewalk 如何登录激活？</h1>
<p>（1）打开Cakewalk Sonar，随便创建一个工程</p>
<p>（2）在弹出页面中，如果你没有Bandlab账号，就点击下方的Sign up</p>
<footer>页脚</footer>
</body>
</html>"""
        parser = FuuumusicHTMLParser()
        parser.feed(html)
        title, body, images = parser.get_result()
        assert "Cakewalk 如何登录激活" in title
        assert "（1）打开Cakewalk Sonar" in body
        assert "（2）在弹出页面中" in body
        assert "导航栏" not in body
        assert "页脚" not in body

    def test_extracts_images(self) -> None:
        html = """<!DOCTYPE html>
<html>
<head><title>测试 | 小闻的奇妙屋</title></head>
<body>
<p>正文</p>
<img src="/assets/img/qa/image46.webp" alt="截图">
<img src="/assets/img/qa/image47.webp" alt="截图2">
<img src="/assets/img/logo.png" alt="logo">
</body>
</html>"""
        parser = FuuumusicHTMLParser()
        parser.feed(html)
        _, _, images = parser.get_result()
        assert len(images) == 2
        assert "https://www.fuuumusic.com/assets/img/qa/image46.webp" in images
        assert "https://www.fuuumusic.com/assets/img/qa/image47.webp" in images

    def test_parses_list_items(self) -> None:
        html = """<!DOCTYPE html>
<html>
<head><title>测试 | 小闻的奇妙屋</title></head>
<body>
<ul>
<li>第一步：打开软件</li>
<li>第二步：点击登录</li>
</ul>
</body>
</html>"""
        parser = FuuumusicHTMLParser()
        parser.feed(html)
        _, body, _ = parser.get_result()
        assert "第一步：打开软件" in body
        assert "第二步：点击登录" in body


class TestPageToEntry:
    def test_converts_faq_page(self) -> None:
        page = FuuumusicPage(
            url="https://www.fuuumusic.com/cakewalk-sonar-faq/login-activate",
            title="Cakewalk 如何登录激活？",
            body="（1）打开Cakewalk Sonar\n\n（2）点击登录",
            images=["https://www.fuuumusic.com/assets/img/qa/image46.webp"],
        )
        entry = page_to_entry(page, revision_id=12345)
        assert entry.id.startswith("qa_")
        assert entry.section_path == ["fuuumusic FAQ"]
        assert entry.category == "fuuumusic FAQ"
        assert entry.title == "Cakewalk 如何登录激活？"
        assert entry.raw_title == "Cakewalk 如何登录激活？"
        assert "打开Cakewalk Sonar" in entry.body
        assert len(entry.images) == 1
        assert entry.source_locator == page.url
        assert entry.source_revision == 12345

    def test_converts_help_page(self) -> None:
        page = FuuumusicPage(
            url="https://www.fuuumusic.com/cakewalk-sonar-help/how-to-export",
            title="如何导出项目",
            body="导出步骤说明",
            images=[],
        )
        entry = page_to_entry(page, revision_id=12345)
        assert entry.section_path == ["fuuumusic 帮助文档"]
        assert entry.category == "fuuumusic 帮助文档"

    def test_stable_id(self) -> None:
        page = FuuumusicPage(
            url="https://www.fuuumusic.com/cakewalk-sonar-faq/test",
            title="测试标题",
            body="测试正文",
            images=[],
        )
        entry1 = page_to_entry(page, revision_id=1)
        entry2 = page_to_entry(page, revision_id=2)
        assert entry1.id == entry2.id
