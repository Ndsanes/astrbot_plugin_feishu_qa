"""fuuumusic 问答聚合页解析测试。

fixture 严格照抄站点真实标记(class="qa-item" / data-qtitle / qa-a / qa-img):
解析器的正确性建立在"站点自己的语义标注"上,标错了就等于把线上契约测错。
"""

from __future__ import annotations

from astrbot_plugin_feishu_qa.corpus.builder import build_manifest
from astrbot_plugin_feishu_qa.corpus.parser import ParseResult
from astrbot_plugin_feishu_qa.fuuumusic import (
    QA_PAGE_URL,
    SOURCE_NAME,
    discover_sections,
    parse_qa_document,
)

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><title>全部问题 · 有福同享全家桶 常见问题汇总 | 小闻的奇妙屋</title></head>
<body>
<nav>导航 首页 常见问题 帮助</nav>
<main>
<h1>有福同享全家桶 · 常见问题汇总</h1>
<h2>一、Cakewalk Sonar相关问答汇总</h2>
<h3 class="qa-h3">（一）缺少内容相关</h3>
<details class="qa-item" id="q1"
  data-qtitle="【没有自带模板】Cakewalk没有自带的模板怎么办？">
<summary class="qa-q"><span class="qa-num">1</span>
<span class="qa-qt"><span class="qa-tag">没有自带模板</span>
Cakewalk没有自带的模板怎么办？</span></summary>
<div class="qa-a">
<figure class="qa-fig">
<img class="qa-img" src="/assets/img/qa/image2.webp" width="1400" height="788" alt="">
</figure>
<p>A：一般来说我们会自己去创建模板。</p>
<p>（1）首先我们打开Cakewalk，按快捷键P。</p>
</div>
</details>
<details class="qa-item" id="q2"
  data-qtitle="【搜索不到自带音源】Cakewalk自带的Studio Instruments下载后搜索不到怎么办？">
<summary class="qa-q"><span class="qa-num">2</span>
<span class="qa-qt"><span class="qa-tag">搜索不到自带音源</span>
Cakewalk自带的Studio Instruments下载后搜索不到怎么办？</span></summary>
<div class="qa-a">
<p>A：先看 VST 扫描路径。</p>
<ol><li>打开设置</li><li>添加 C:\\Program Files\\Cakewalk\\VstPlugins</li></ol>
<figure class="qa-fig">
<img class="qa-img" src="/assets/img/qa/image3.webp" alt="">
</figure>
</div>
</details>
<h2>二、其他音源相关问答汇总</h2>
<h3 class="qa-h3">（一）Native Instruments （NI）相关</h3>
<details class="qa-item" id="q38"
  data-qtitle="【NI安装失败】Native Instrument音源、插件一直安装失败怎么办？">
<summary class="qa-q"><span class="qa-num">9</span>
<span class="qa-qt"><span class="qa-tag">NI安装失败</span>
Native Instrument音源、插件一直安装失败怎么办？</span></summary>
<div class="qa-a">
<p>自从inMusic收购Native Instrument之后，NI相关的插件领取、下载、安装有了很大的变动。</p>
<figure class="qa-fig">
<img class="qa-img" src="/assets/img/qa/image60.webp" alt="">
</figure>
</div>
</details>
<details class="qa-item" id="q99">
<summary class="qa-q"><span class="qa-num">10</span>
<span class="qa-qt"><span class="qa-tag">无标题属性</span>
没有 data-qtitle 时靠 summary 重建？</span></summary>
<div class="qa-a"><p>A：是的。</p></div>
</details>
</main>
<script>var decoy = "【假标签】脚本内容不该进语料";</script>
<footer>页脚不该进语料</footer>
</body>
</html>"""


class TestParseQaDocument:
    def test_item_count(self) -> None:
        entries = parse_qa_document(PAGE_HTML, revision_id=7)
        assert len(entries) == 4

    def test_title_from_data_qtitle_with_tags(self) -> None:
        entry = parse_qa_document(PAGE_HTML)[0]
        # 站点标注的标题带【症状标签】,与飞书文档同形,故症状标签可直接复用
        assert entry.raw_title == "【没有自带模板】Cakewalk没有自带的模板怎么办？"
        assert entry.symptom_tags == ["没有自带模板"]
        assert entry.title == "Cakewalk没有自带的模板怎么办？"

    def test_section_path_and_category(self) -> None:
        entry = parse_qa_document(PAGE_HTML)[0]
        assert entry.section_path == ["一、Cakewalk Sonar相关问答汇总", "（一）缺少内容相关"]
        assert entry.category == "（一）缺少内容相关"

    def test_deep_link_locator(self) -> None:
        entries = parse_qa_document(PAGE_HTML)
        assert entries[0].source_locator == f"{QA_PAGE_URL}#q1"
        # 用户实测那条:NI 安装失败
        ni = next(e for e in entries if "NI安装失败" in e.raw_title)
        assert ni.source_locator == f"{QA_PAGE_URL}#q38"
        assert ni.section_path[0] == "二、其他音源相关问答汇总"

    def test_source_tagged(self) -> None:
        for entry in parse_qa_document(PAGE_HTML):
            assert entry.source == SOURCE_NAME

    def test_body_paragraphs_and_list_items(self) -> None:
        entries = parse_qa_document(PAGE_HTML)
        assert "按快捷键P" in entries[0].body
        assert "打开设置" in entries[1].body
        assert "添加 C:\\Program Files\\Cakewalk\\VstPlugins" in entries[1].body

    def test_skips_nav_script_footer(self) -> None:
        body = "\n".join(e.body for e in parse_qa_document(PAGE_HTML))
        assert "页脚不该进语料" not in body
        assert "假标签" not in body
        assert "导航" not in body

    def test_summary_fallback_rebuilds_tag(self) -> None:
        entry = next(
            e
            for e in parse_qa_document(PAGE_HTML)
            if e.raw_title.startswith("【无标题属性】")
        )
        assert entry.raw_title == "【无标题属性】没有 data-qtitle 时靠 summary 重建？"
        assert entry.symptom_tags == ["无标题属性"]

    def testImages_collected_in_order(self) -> None:
        entries = parse_qa_document(PAGE_HTML)
        assert [i.file_token for i in entries[0].images] == [
            "https://www.fuuumusic.com/assets/img/qa/image2.webp"
        ]
        assert [i.file_token for i in entries[1].images] == [
            "https://www.fuuumusic.com/assets/img/qa/image3.webp"
        ]

    def test_section_filter(self) -> None:
        entries = parse_qa_document(PAGE_HTML, sections=["二、其他音源相关问答汇总"])
        assert len(entries) == 2
        assert all(e.section_path[0] == "二、其他音源相关问答汇总" for e in entries)

    def test_empty_sections_imports_everything(self) -> None:
        assert len(parse_qa_document(PAGE_HTML, sections=[])) == 4

    def test_ids_stable_and_unique(self) -> None:
        first = parse_qa_document(PAGE_HTML, revision_id=1)
        second = parse_qa_document(PAGE_HTML, revision_id=2)
        assert [e.id for e in first] == [e.id for e in second]
        assert len({e.id for e in first}) == len(first)
        assert all(e.id.startswith("qa_") for e in first)

    def test_garbage_html_yields_nothing(self) -> None:
        assert parse_qa_document("<html><body><p>空页</p></body></html>") == []


class TestDiscoverSections:
    def test_lists_h2_in_order(self) -> None:
        assert discover_sections(PAGE_HTML) == [
            "一、Cakewalk Sonar相关问答汇总",
            "二、其他音源相关问答汇总",
        ]


class TestManifestIntegration:
    def test_images_get_local_path_from_builder(self) -> None:
        entries = parse_qa_document(PAGE_HTML, revision_id=3)
        manifest = build_manifest(
            ParseResult(entries=entries),
            revision_id=3,
            document_id="fuuumusic_qa",
            source=SOURCE_NAME,
        )
        first = manifest["entries"][0]
        assert first["source"] == SOURCE_NAME
        assert first["images"][0]["local_path"].startswith("images/")
        assert first["images"][0]["local_path"].endswith(".webp")

    def test_entry_survives_manifest_roundtrip(self) -> None:
        # 与 main.py 的 _manifest_to_entries 同一路径:字段全须可还原
        from astrbot_plugin_feishu_qa.corpus.model import QaEntry, QaImage

        entries = parse_qa_document(PAGE_HTML, revision_id=3)
        manifest = build_manifest(
            ParseResult(entries=entries), revision_id=3, document_id="d", source=SOURCE_NAME
        )
        raw = manifest["entries"][0]
        restored = QaEntry(**{**raw, "images": [QaImage(**i) for i in raw["images"]]})
        assert restored.source == SOURCE_NAME
        assert restored.source_locator.endswith("#q1")


class TestFetchDoesNotBlockEventLoop:
    """抓取必须让出事件循环。

    插件零第三方依赖(没有 aiohttp),旧实现直接在协程里跑同步 urllib:线上
    实测每次同步期间整台机器人停顿约 30 秒,普通消息回复跟着一起变慢。
    """

    async def test_fetch_bytes_yields_to_loop(self, monkeypatch) -> None:
        import asyncio
        import time

        from astrbot_plugin_feishu_qa import fuuumusic

        def slow_fetch(url: str, timeout: float) -> bytes:
            time.sleep(0.3)
            return b"payload"

        monkeypatch.setattr(fuuumusic, "_fetch_bytes_sync", slow_fetch)

        gaps: list[float] = []
        stop = False

        async def ticker() -> None:
            last = time.monotonic()
            while not stop:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        task = asyncio.create_task(ticker())
        try:
            assert await fuuumusic.fetch_bytes("https://example.invalid/x") == b"payload"
        finally:
            stop = True
            await task

        assert gaps, "心跳任务没有跑起来"
        assert max(gaps) < 0.2, f"事件循环被阻塞了 {max(gaps):.2f}s"
