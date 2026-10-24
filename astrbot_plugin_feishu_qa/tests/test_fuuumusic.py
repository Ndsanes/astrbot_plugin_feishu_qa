"""fuuumusic 问答聚合页解析测试。

fixture 严格照抄站点真实标记(class="qa-item" / data-qtitle / qa-a / qa-img):
解析器的正确性建立在"站点自己的语义标注"上,标错了就等于把线上契约测错。
"""

from __future__ import annotations

from astrbot_plugin_feishu_qa.corpus.builder import build_manifest
from astrbot_plugin_feishu_qa.corpus.model import QaEntry, derive_entry_id
from astrbot_plugin_feishu_qa.corpus.parser import ParseResult
from astrbot_plugin_feishu_qa.fuuumusic import (
    QA_PAGE_URL,
    SOURCE_NAME,
    discover_sections,
    parse_qa_document,
    resolve_sections,
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


class TestResolveSections:
    """章节白名单是**章节名**(会过期),过期的白名单必须退化为不过滤。"""

    AVAILABLE = [
        "一、Cakewalk Sonar相关问答汇总",
        "二、其他音源相关问答汇总",
        "三、Cakewalk 官方文档（中文翻译）",
    ]

    def test_legacy_config_degenerates_to_no_filter(self) -> None:
        # 线上实测:保存的还是旧版目录名,若据此过滤会把整个网页语料清零
        assert resolve_sections(["cakewalk-sonar-faq", "cakewalk-sonar-help"], self.AVAILABLE) == []

    def test_matching_keys_are_kept(self) -> None:
        assert resolve_sections(["二、其他音源相关问答汇总"], self.AVAILABLE) == [
            "二、其他音源相关问答汇总"
        ]

    def test_partial_match_keeps_what_matches(self) -> None:
        got = resolve_sections(["三、Cakewalk 官方文档（中文翻译）", "过期章节"], self.AVAILABLE)
        assert got == ["三、Cakewalk 官方文档（中文翻译）"]

    def test_empty_or_none_means_no_filter(self) -> None:
        assert resolve_sections([], self.AVAILABLE) == []
        assert resolve_sections(None, self.AVAILABLE) == []
        assert resolve_sections(["", "  "], self.AVAILABLE) == []

    def test_whitespace_tolerated(self) -> None:
        assert resolve_sections(["  二、其他音源相关问答汇总  "], self.AVAILABLE) == [
            "二、其他音源相关问答汇总"
        ]


# ── 官方文档译文(全文页) ──
#
# 站点在这 53 篇官方文档上的真实形态:聚合页只给一段导语 + 「阅读全文」,
# 全文在 /cakewalk-sonar-help/<slug> 的 div.cw-body 里,分类/更新日期/官方
# 原文链接写在 JSON-LD(TechArticle)里。下面的 fixture 严格照抄这些标记。

HELP_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<title>如何在 Cakewalk Sonar 中插入 FX｜Cakewalk Sonar 中文帮助</title>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "TechArticle",
      "headline": "如何在 Cakewalk Sonar 中插入 FX",
      "articleSection": "快速上手",
      "dateModified": "2026-08-13",
      "translationOfWork": {
        "@type": "CreativeWork",
        "url": "https://help.cakewalk.com/hc/en-us/articles/41322234512537-How-to-Insert-FX-in-Cakewalk-Sonar"
      }
    }
  ]
}
</script>
</head>
<body>
<nav class="site-nav">站点导航不该进正文</nav>
<main id="main"><div class="cw-main"><div class="container cw-layout">
<article class="cw-article">
<h1>如何在 Cakewalk Sonar 中插入 FX</h1>
<p class="cw-meta">译自官方帮助文档 ·
<a href="https://help.cakewalk.com/hc/en-us/articles/41322234512537"
target="_blank">查看英文原文</a></p>
<p class="cw-attrib">文中「我们」均指 BandLab / Cakewalk。本页为中文翻译，非官方页面。</p>
<nav class="cw-toc" aria-label="本页目录"><p class="cw-toc-t">本页目录</p>
<ol><li class="lv3"><a href="#h_01">打开音轨视图（Track View）</a></li></ol></nav>
<div class="cw-body">
<div class="article-intro">
<p>在 Cakewalk Sonar 中，为音轨插入效果器（FX）是塑造声音的重要环节。</p></div>
<section class="article-section"><h2 id="h_01">打开音轨视图（Track View）</h2>
<p>添加效果之前，请确认你位于<strong>音轨视图</strong>中。</p>
<ul>
<li>转到 <strong>视图（View）</strong> &gt; <strong>音轨视图</strong>，
或按 <code>Alt+1</code> 打开。</li>
</ul>
<figure style="width: 100%;">
<img alt="音轨视图" src="/assets/img/cakewalk/cw-38677242734233.webp"/>
</figure>
</section>
<section class="article-section"><h2 id="h_02">选择效果器</h2>
<table class="custom-table">
<thead><tr><th>效果</th><th>快捷键</th></tr></thead>
<tbody><tr><td>混响</td><td><strong>R</strong></td></tr></tbody>
</table>
<div class="cw-tabs">
<div class="cw-tabs-menu" role="tablist">
<button aria-selected="true" class="cw-tab is-active" role="tab" type="button">免费版</button>
<button aria-selected="false" class="cw-tab" role="tab" type="button">会员版</button>
</div>
<div class="cw-tabpanel is-active"><p>免费版：核心功能可用。</p></div>
<div class="cw-tabpanel"><p>会员版：解锁全部插件。</p></div>
</div>
<details class="cw-acc cw-acc--more"><summary id="acc-1">展开查看全部</summary>
<div class="bento-grid"><div class="bento-item"><h3>场景切换</h3>
<p>在单个音色（patch）内切换场景。</p></div></div></details>
</section>
<script>var decoy = "脚本不该进正文";</script>
</div>
<p class="cw-disclaimer">本文为 Cakewalk Help Center 原文的非官方中文翻译。</p>
<nav class="cw-footnav"><a href="/cakewalk-sonar-faq">← 返回常见问题</a></nav>
</article></div></div></main>
<footer>页脚不该进正文</footer>
</body>
</html>"""

HELP_URL = "https://www.fuuumusic.com/cakewalk-sonar-help/how-to-insert-fx-in-cakewalk-sonar"

AGG_WITH_HELP_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><body><main>
<h1>有福同享全家桶 · 常见问题汇总</h1>
<h2>一、Cakewalk Sonar相关问答汇总</h2>
<h3 class="qa-h3">（一）缺少内容相关</h3>
<details class="qa-item" id="q1" data-qtitle="【没有自带模板】Cakewalk没有自带的模板怎么办？">
<summary class="qa-q"><span class="qa-num">1</span>
<span class="qa-qt"><span class="qa-tag">没有自带模板</span>
Cakewalk没有自带的模板怎么办？</span></summary>
<div class="qa-a"><p>A：一般来说我们会自己去创建模板。</p></div>
</details>
<h2>三、Cakewalk 官方文档（中文翻译）</h2>
<h3 class="qa-h3">安装</h3>
<details class="qa-item" id="d1"
  data-qtitle="Cakewalk Product Center"
  data-qkw="下载器 安装器 产品中心 装不上">
<summary class="qa-q"><span class="qa-num">48</span>
<span class="qa-qt"><span class="qa-tag">官方</span>Cakewalk Product Center</span></summary>
<div class="qa-a">
<p>Cakewalk Product Center 是一款一站式程序。</p>
<p><a class="qa-xref" href="/cakewalk-sonar-help/cakewalk-product-center">阅读全文</a></p>
</div>
</details>
</main></body></html>"""

PRODUCT_CENTER_URL = (
    "https://www.fuuumusic.com/cakewalk-sonar-help/cakewalk-product-center"
)


def _help_entry(body: str = "全文第一步。\n\n全文第二步。") -> QaEntry:
    section_path = ["Cakewalk 官方文档（中文翻译）", "安装"]
    return QaEntry(
        id=derive_entry_id(section_path, "Cakewalk Product Center", PRODUCT_CENTER_URL),
        section_path=section_path,
        category="安装",
        symptom_tags=[],
        title="Cakewalk Product Center",
        raw_title="Cakewalk Product Center",
        body=body,
        source_locator=PRODUCT_CENTER_URL,
        source=SOURCE_NAME,
    )


class TestParseHelpDocument:
    def test_title_and_section(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import parse_help_document

        entry = parse_help_document(HELP_PAGE_HTML, page_url=HELP_URL, revision_id=5)
        assert entry is not None
        assert entry.title == "如何在 Cakewalk Sonar 中插入 FX"
        assert entry.section_path == ["Cakewalk 官方文档（中文翻译）", "快速上手"]
        assert entry.category == "快速上手"
        assert entry.source == SOURCE_NAME
        assert entry.source_locator == HELP_URL
        assert entry.source_revision == 5

    def test_body_keeps_steps_and_list_items(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import parse_help_document

        entry = parse_help_document(HELP_PAGE_HTML, page_url=HELP_URL)
        assert entry is not None
        assert "添加效果之前，请确认你位于音轨视图中。" in entry.body
        assert "打开音轨视图（Track View）" in entry.body
        assert "Alt+1" in entry.body
        assert "场景切换" in entry.body

    def test_table_rows_are_joined(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import parse_help_document

        entry = parse_help_document(HELP_PAGE_HTML, page_url=HELP_URL)
        assert entry is not None
        assert "效果 | 快捷键" in entry.body
        assert "混响 | R" in entry.body

    def test_tab_panels_keep_their_labels(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import parse_help_document

        entry = parse_help_document(HELP_PAGE_HTML, page_url=HELP_URL)
        assert entry is not None
        assert "【免费版】" in entry.body
        assert "【会员版】" in entry.body
        assert "免费版：核心功能可用。" in entry.body
        assert "会员版：解锁全部插件。" in entry.body

    def test_skips_chrome_and_boilerplate(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import parse_help_document

        entry = parse_help_document(HELP_PAGE_HTML, page_url=HELP_URL)
        assert entry is not None
        for noise in (
            "本页目录",
            "非官方页面",
            "返回常见问题",
            "展开查看全部",
            "脚本不该进正文",
            "页脚不该进正文",
            "站点导航不该进正文",
        ):
            assert noise not in entry.body, noise

    def test_images_from_cakewalk_prefix(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import parse_help_document

        entry = parse_help_document(HELP_PAGE_HTML, page_url=HELP_URL)
        assert entry is not None
        assert [i.file_token for i in entry.images] == [
            "https://www.fuuumusic.com/assets/img/cakewalk/cw-38677242734233.webp"
        ]

    def test_metadata_extracts_original_and_update_date(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import parse_help_metadata

        meta = parse_help_metadata(HELP_PAGE_HTML)
        assert meta["section"] == "快速上手"
        assert meta["updated"] == "2026-08-13"
        assert meta["original"].endswith("-How-to-Insert-FX-in-Cakewalk-Sonar")

    def test_structure_without_body_returns_none(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import parse_help_document

        assert parse_help_document("<html><body><h1>空页</h1></body></html>", page_url="x") is None

    def test_id_is_stable_across_revisions(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import parse_help_document

        first = parse_help_document(HELP_PAGE_HTML, page_url=HELP_URL, revision_id=1)
        second = parse_help_document(HELP_PAGE_HTML, page_url=HELP_URL, revision_id=2)
        assert first is not None and second is not None
        assert first.id == second.id


class TestDiscoverHelpPages:
    def test_absolutizes_and_dedupes(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import discover_help_pages

        page = (
            '<a class="qa-xref" href="/cakewalk-sonar-help/cakewalk-product-center">阅读全文</a>'
            '<a href="/cakewalk-sonar-help/cakewalk-product-center">再来一次</a>'
            '<a href="/cakewalk-sonar-faq/all">不是官方文档</a>'
        )
        assert discover_help_pages(page) == [
            "https://www.fuuumusic.com/cakewalk-sonar-help/cakewalk-product-center"
        ]

    def test_index_page(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import discover_help_pages

        page = '<a href="/cakewalk-sonar-help/how-to-insert-fx-in-cakewalk-sonar">x</a>'
        assert discover_help_pages(page) == [HELP_URL]


class TestHelpEntriesReplaceExcerpts:
    def test_full_text_replaces_excerpt(self) -> None:
        entries = parse_qa_document(
            AGG_WITH_HELP_HTML,
            help_entries={PRODUCT_CENTER_URL: _help_entry()},
        )
        assert len(entries) == 2
        doc = entries[-1]
        assert doc.body == "全文第一步。\n\n全文第二步。"
        assert doc.source_locator == PRODUCT_CENTER_URL
        # 站点自己的检索关键词(data-qkw)只挂在聚合页上,必须带到全文条目里
        assert doc.symptom_tags == ["下载器", "安装器", "产品中心", "装不上"]

    def test_missing_page_keeps_excerpt(self) -> None:
        entries = parse_qa_document(AGG_WITH_HELP_HTML, help_entries={})
        doc = entries[-1]
        assert "导语摘要" not in doc.body
        assert "Cakewalk Product Center 是一款一站式程序。" in doc.body
        # 摘要条目的回退标题来自 data-qtitle(英文原名),全文抓到时才会换成中文
        assert doc.raw_title == "Cakewalk Product Center"
        assert doc.source_locator.endswith("#d1")
        assert doc.symptom_tags == []

    def test_new_page_appended_when_not_in_aggregate(self) -> None:
        extra = _help_entry(body="插入 FX 的完整步骤。")
        extra.id = "qa_extra0000000000"
        extra.source_locator = HELP_URL
        entries = parse_qa_document(
            AGG_WITH_HELP_HTML,
            help_entries={PRODUCT_CENTER_URL: _help_entry(), HELP_URL: extra},
        )
        assert len(entries) == 3
        assert any(e.id == "qa_extra0000000000" for e in entries)

    def test_section_filter_drops_official_docs(self) -> None:
        entries = parse_qa_document(
            AGG_WITH_HELP_HTML,
            sections=["一、Cakewalk Sonar相关问答汇总"],
            help_entries={PRODUCT_CENTER_URL: _help_entry()},
        )
        assert len(entries) == 1
        assert entries[0].id.startswith("qa_")

    def test_section_filter_matches_without_numbering(self) -> None:
        entries = parse_qa_document(
            AGG_WITH_HELP_HTML,
            sections=["Cakewalk 官方文档（中文翻译）"],
            help_entries={PRODUCT_CENTER_URL: _help_entry()},
        )
        assert [e.source_locator for e in entries] == [PRODUCT_CENTER_URL]

    def test_no_duplicate_when_page_already_in_aggregate(self) -> None:
        entries = parse_qa_document(
            AGG_WITH_HELP_HTML, help_entries={PRODUCT_CENTER_URL: _help_entry()}
        )
        assert len({e.id for e in entries}) == len(entries) == 2


class TestSectionsIncludeOfficial:
    """章节白名单不含官方文档时,不该再为 53 个全文页发请求。"""

    def test_empty_means_include(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import sections_include_official

        assert sections_include_official(None) is True
        assert sections_include_official([]) is True

    def test_matches_with_and_without_numbering(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import sections_include_official

        assert sections_include_official(["三、Cakewalk 官方文档（中文翻译）"]) is True
        assert sections_include_official(["Cakewalk 官方文档（中文翻译）"]) is True

    def test_faq_only_excludes(self) -> None:
        from astrbot_plugin_feishu_qa.fuuumusic import sections_include_official

        assert sections_include_official(["一、Cakewalk Sonar相关问答汇总"]) is False
