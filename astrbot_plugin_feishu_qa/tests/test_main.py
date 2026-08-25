"""插件主体测试:指令解析、语料装载、直答链路、tool schema(spec §60)。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot.api.event import AstrMessageEvent  # 桩包(或真实包)

from astrbot_plugin_feishu_qa.main import FeishuQaPlugin

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def plugin(tmp_path: Path, monkeypatch) -> FeishuQaPlugin:
    """构建带真实 fixture 语料的插件实例。"""
    monkeypatch.setenv("ASTRBOT_DATA_DIR", str(tmp_path))
    config = {
        "WIKI_URL": "https://my.feishu.cn/wiki/test",
        "ENABLED_GROUPS": ["*"],
        "ADMIN_USERS": ["admin1"],
        "SYNC_INTERVAL_HOURS": 0,
        "AUTH_CHECK_HOURS": 0,
    }
    plugin = FeishuQaPlugin(context=None, config=config)

    # 装入真实语料快照(离线)
    from astrbot_plugin_feishu_qa.corpus.builder import build_manifest
    from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
    from astrbot_plugin_feishu_qa.storage.snapshot import SnapshotStore

    xml = (FIXTURES / "qa_r8268.xml").read_text()
    parsed = parse_xml(xml, source_revision=8268)
    manifest = build_manifest(parsed, revision_id=8268, document_id="doc")
    SnapshotStore(plugin.data_root).commit(manifest)
    assert plugin._load_corpus() is True
    return plugin


def run_handler(coro_or_gen):
    import asyncio

    async def consume():
        out = []
        if hasattr(coro_or_gen, "__anext__"):
            async for item in coro_or_gen:
                out.append(item)
        else:
            out.append(await coro_or_gen)
        return out

    return asyncio.run(consume())


class TestStripCommand:
    def test_variants(self):
        f = FeishuQaPlugin._strip_command
        assert f("/问 cakewalk 没声音") == "cakewalk 没声音"
        assert f("问 cakewalk") == "cakewalk"
        assert f("普通聊天") == "普通聊天"


class TestCorpusLoading:
    def test_entries_loaded(self, plugin: FeishuQaPlugin) -> None:
        assert len(plugin._entries) >= 35
        assert plugin._retriever is not None and plugin._router is not None

    def test_load_without_snapshot_keeps_old(self, plugin: FeishuQaPlugin) -> None:
        old_count = len(plugin._entries)
        # 破坏快照后重载 → 失败但保留内存旧值
        plugin.store.snapshot_path.write_text("{bad")
        assert plugin._load_corpus() is False
        assert len(plugin._entries) == old_count


class TestAskFlow:
    def test_direct_answer_stops_event(self, plugin: FeishuQaPlugin) -> None:
        event = AstrMessageEvent(
            message_str="/问 声卡设置没问题但是cakewalk就是没声音", group_id="g1"
        )
        event.is_at_or_wake_command = True
        outs = run_handler(plugin.ask(event))
        assert outs == []  # 直答经 event.send 发送,不经生成器产出
        assert len(event.sent) == 1, "应发送一条合并转发消息"
        assert event.is_stopped(), "高置信直答必须阻断后续 LLM 流水线"

    def test_miss_reply_without_stop(self, plugin: FeishuQaPlugin) -> None:
        event = AstrMessageEvent(message_str="/问 今天天气怎么样", group_id="g1")
        event.is_at_or_wake_command = True
        outs = run_handler(plugin.ask(event))
        text = outs[0][1]
        assert "没有" in text
        assert not event.is_stopped()

    def test_whitelist_off_denies(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("ASTRBOT_DATA_DIR", str(tmp_path))
        plugin = FeishuQaPlugin(
            context=None,
            config={"ENABLED_GROUPS": [], "SYNC_INTERVAL_HOURS": 0, "AUTH_CHECK_HOURS": 0},
        )
        # 无语料 + 空白名单:不产生直答内容
        event = AstrMessageEvent(message_str="/问 没声音", group_id="g1")
        event.is_at_or_wake_command = True
        outs = run_handler(plugin.ask(event))
        assert all("找到一个相关问题" not in str(o) for o in outs)


class TestGroupWakeListener:
    def test_non_wake_message_ignored(self, plugin: FeishuQaPlugin) -> None:
        event = AstrMessageEvent(message_str="日常聊天内容", group_id="g1")
        event.is_at_or_wake_command = False
        run_handler(plugin.on_group_message(event))
        assert not event.sent and not event.is_stopped(), "未唤醒消息必须零响应"


class TestFaqCitationGuidance:
    """on_llm_request 钩子:FAQ 出处引用规范注入。"""

    def test_hook_registered(self, plugin: FeishuQaPlugin) -> None:
        assert getattr(plugin.add_faq_citation_guidance, "_filter", ("",))[0] == (
            "on_llm_request"
        )
        assert getattr(plugin.qa_send_answer, "_llm_tool_name", "") == (
            "qa_send_answer"
        )


    def test_hook_appends_guidance(self, plugin: FeishuQaPlugin) -> None:
        class Req:
            def __init__(self) -> None:
                self.system_prompt = "base-prompt"

        req = Req()
        run_handler(plugin.add_faq_citation_guidance(AstrMessageEvent(), req))
        assert req.system_prompt.startswith("base-prompt")
        assert "章节直达链接" in req.system_prompt
        assert "qa_send_answer" not in req.system_prompt  # 附链已自动化,模型无需调用

    def test_guidance_handles_empty_system_prompt(self, plugin) -> None:
        class Req:
            system_prompt = ""

        req = Req()
        run_handler(plugin.add_faq_citation_guidance(AstrMessageEvent(), req))
        assert req.system_prompt.startswith("\n[FAQ 引用规范]")



class TestQaSendAnswer:
    """qa_send_answer 工具:命中条目 → 飞书文档章节直达链接列表。"""

    def test_tool_registered_with_docstring(self, plugin: FeishuQaPlugin) -> None:
        fn = plugin.qa_send_answer
        assert getattr(fn, "_llm_tool_name", "") == "qa_send_answer"
        doc = (fn.__doc__ or "").strip()
        assert "Args:" in doc and "entry_ids" in doc and "逗号" in doc

    def test_invalid_entry_rejected_without_send(
        self, plugin: FeishuQaPlugin
    ) -> None:
        event = AstrMessageEvent()
        out = run_handler(plugin.qa_send_answer(event, entry_ids="not-an-id"))
        assert isinstance(out[0], str) and "没有可投递" in out[0]
        out = run_handler(plugin.qa_send_answer(event, entry_ids="qa_" + "f" * 16))
        assert isinstance(out[0], str) and "没有可投递" in out[0]
        out = run_handler(plugin.qa_send_answer(event, entry_ids=""))
        assert isinstance(out[0], str) and "没有可投递" in out[0]
        assert event.sent == [], "校验失败不得发送任何消息"

    def test_qq_official_emits_markdown_hyperlink(
        self, plugin: FeishuQaPlugin, monkeypatch
    ) -> None:
        """qq_official 走原生 markdown(msg_type=2),标题为可点击超链接。"""
        entry = next(e for e in plugin._entries if e.source_locator)
        event = AstrMessageEvent()
        monkeypatch.setattr(
            event, "get_platform_name", lambda: "qq_official", raising=False
        )
        out = run_handler(plugin.qa_send_answer(event, entry_ids=entry.id))
        assert isinstance(out[0], str) and "直达链接" in out[0] and "勿复述" in out[0]
        assert len(event.sent) == 1
        all_text = "".join(
            c[1]
            for c in event.sent[0].chain
            if isinstance(c, tuple) and c[0] == "plain"
        )
        expected = (
            f"1. [{entry.raw_title}]"
            f"(https://my.feishu.cn/wiki/test#{entry.source_locator})"
        )
        assert expected in all_text
        assert "命中 1 条肖闻的解答" in all_text
        assert "Xiaowenn《有福同享全家桶Q&A汇总》" in all_text


    def test_generic_platform_falls_back_to_bare_url(
        self, plugin: FeishuQaPlugin
    ) -> None:
        entry = next(e for e in plugin._entries if e.source_locator)
        event = AstrMessageEvent()  # 桩无平台名 → 非 qq_official 分支
        run_handler(plugin.qa_send_answer(event, entry_ids=entry.id))
        all_text = "".join(
            c.text for c in event.sent[0].chain[0].content if c.type == "Plain"
        )
        assert f"【{entry.raw_title}】" in all_text
        assert f"👉 https://my.feishu.cn/wiki/test#{entry.source_locator}" in all_text
        assert "](" not in all_text, "非官方平台不得输出 markdown 字面量"

    def test_multi_entry_merged_into_single_send(self, plugin) -> None:
        picks = [e for e in plugin._entries if e.source_locator][:2]
        event = AstrMessageEvent()
        out = run_handler(
            plugin.qa_send_answer(event, entry_ids=",".join(e.id for e in picks))
        )
        assert f"已投递{len(picks)}条章节直达链接" in out[0]
        assert len(event.sent) == 1, "多条目也必须合并为一次发送"
        all_text = "".join(
            c.text for c in event.sent[0].chain[0].content if c.type == "Plain"
        )
        for e in picks:
            assert f"test#{e.source_locator}" in all_text
            assert e.raw_title in all_text

    def test_mixed_valid_invalid_skips_bad_ids(self, plugin) -> None:
        good = next(e for e in plugin._entries if e.source_locator)
        event = AstrMessageEvent()
        out = run_handler(
            plugin.qa_send_answer(event, entry_ids=f"{good.id}, bad123")
        )
        assert "已跳过:bad123" in out[0]
        assert len(event.sent) == 1, "合法条目应照常投递"

    def test_entry_without_locator_falls_back_to_doc_root(
        self, plugin: FeishuQaPlugin
    ) -> None:
        noloc = next((e for e in plugin._entries if not e.source_locator), None)
        if noloc is None:
            noloc = plugin._entries[0]
            noloc.source_locator = ""  # 人为清空定位
        event = AstrMessageEvent()
        run_handler(plugin.qa_send_answer(event, entry_ids=noloc.id))
        all_text = "".join(
            c.text for c in event.sent[0].chain[0].content if c.type == "Plain"
        )
        assert "https://my.feishu.cn/wiki/test\n" in all_text + "\n"

    def test_short_code_resolves_entry(self, plugin: FeishuQaPlugin) -> None:
        """[ref:短码] → 短码解析 → 链接列表(魔法链接主路径)。"""
        entry = next(
            e for e in plugin._entries if e.source_locator and e.images
        )
        code = entry.id[3:8]
        event = AstrMessageEvent()
        out = run_handler(plugin.qa_send_answer(event, entry_ids=code))
        assert isinstance(out[0], str) and "直达链接" in out[0]
        all_text = "".join(
            c.text for c in event.sent[0].chain[0].content if c.type == "Plain"
        )
        assert f"test#{entry.source_locator}" in all_text

    def test_unknown_code_rejected_without_send(self, plugin) -> None:
        event = AstrMessageEvent()
        out = run_handler(plugin.qa_send_answer(event, entry_ids="zzzzz"))
        assert isinstance(out[0], str) and "没有可投递" in out[0]
        assert event.sent == [], "未知短码不得发送任何消息"


class TestAutoFaqLinks:
    """astr_kb_search 命中 FAQ 后的确定性自动附链(on_llm_tool_respond)。"""

    def _tool_result(self, codes):
        text = "".join(
            f"【全家桶FAQ > 章节】标题\n正文\n[ref:{c}]\n相关度: 1.00\n\n"
            for c in codes
        )
        return SimpleNamespace(content=[SimpleNamespace(text=text)])

    def _tool(self, name="astr_kb_search"):
        return SimpleNamespace(name=name)

    def test_auto_sends_markdown_links_for_qq_official(
        self, plugin, monkeypatch
    ) -> None:
        entry = next(e for e in plugin._entries if e.source_locator)
        event = AstrMessageEvent()
        monkeypatch.setattr(
            event, "get_platform_name", lambda: "qq_official", raising=False
        )
        run_handler(plugin.auto_send_faq_links(
            event,
            self._tool(),
            None,
            self._tool_result([entry.id[3:8]]),
        ))
        assert len(event.sent) == 1
        joined = "\n".join(
            c[1] for c in event.sent[0].chain if isinstance(c, tuple) and c[0] == "plain"
        )
        assert f"[{entry.raw_title}]" \
            f"(https://my.feishu.cn/wiki/test#{entry.source_locator})" in joined

    def test_dedupes_within_same_event(self, plugin) -> None:
        entry = next(e for e in plugin._entries if e.source_locator)
        event = AstrMessageEvent()
        result = self._tool_result([entry.id[3:8]])
        run_handler(plugin.auto_send_faq_links(event, self._tool(), None, result))
        first = len(event.sent)
        assert first >= 1
        run_handler(plugin.auto_send_faq_links(event, self._tool(), None, result))
        assert len(event.sent) == first, "同轮重复命中不得重复发链接"

    def test_non_kb_tool_ignored(self, plugin) -> None:
        tool = SimpleNamespace(name="other_tool")
        event = AstrMessageEvent()
        run_handler(plugin.auto_send_faq_links(event, tool, None, self._tool_result([])))
        assert event.sent == []


class TestAdminCommands:
    def test_status_denies_non_admin(self, plugin: FeishuQaPlugin) -> None:
        event = AstrMessageEvent(sender_id="nobody")
        outs = run_handler(plugin.qa_status(event))
        assert "仅管理员" in outs[0][1]

    def test_status_shows_revision(self, plugin: FeishuQaPlugin) -> None:
        event = AstrMessageEvent(sender_id="admin1")
        outs = run_handler(plugin.qa_status(event))
        text = outs[0][1]
        assert "revision: 8268" in text


class TestMergedForwardCapability:
    """合并转发能力探测(Phase 4):qq_official 排除,其余放行由构建失败兜底。"""

    def test_qq_official_不支持合并转发(self):
        assert FeishuQaPlugin._supports_merged_forward("qq_official") is False

    def test_aiocqhttp_支持合并转发(self):
        assert FeishuQaPlugin._supports_merged_forward("aiocqhttp") is True

    def test_未知平台默认尝试构建并依赖回退(self):
        assert FeishuQaPlugin._supports_merged_forward("unknown_platform") is True


class TestGatewayDegradation:
    """网关缺失时优雅降级:初始化不崩、本地语料继续服务。"""

    def _plugin(self, tmp_path: Path, monkeypatch) -> FeishuQaPlugin:
        monkeypatch.setenv("ASTRBOT_DATA_DIR", str(tmp_path))
        return FeishuQaPlugin(
            context=None,
            config={
                "ENABLED_GROUPS": ["*"],
                "ADMIN_USERS": ["admin1"],
                "SYNC_INTERVAL_HOURS": 0,
                "AUTH_CHECK_HOURS": 0,
            },
        )

    def test_gateway_absent_at_init(self, tmp_path: Path, monkeypatch) -> None:
        plugin = self._plugin(tmp_path, monkeypatch)
        # context=None → platform_manager 不可达 → 网关解析为 None
        assert plugin._get_gateway() is None
        assert plugin.adapter.available is False

    def test_sync_without_gateway_raises_but_plugin_alive(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from astrbot_plugin_feishu_qa.adapter.gateway import GatewayUnavailableError

        plugin = self._plugin(tmp_path, monkeypatch)
        with pytest.raises(GatewayUnavailableError):
            asyncio.run(plugin.sync_once())
        # 降级不崩溃:本地语料装载与检索照常工作
        from astrbot_plugin_feishu_qa.corpus.builder import build_manifest
        from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
        from astrbot_plugin_feishu_qa.storage.snapshot import SnapshotStore

        xml = (FIXTURES / "qa_r8268.xml").read_text()
        parsed = parse_xml(xml, source_revision=8268)
        manifest = build_manifest(parsed, revision_id=8268, document_id="doc")
        SnapshotStore(plugin.data_root).commit(manifest)
        assert plugin._load_corpus() is True
        assert plugin._retriever is not None

    def test_status_reports_missing_gateway(self, tmp_path: Path, monkeypatch) -> None:
        plugin = self._plugin(tmp_path, monkeypatch)
        event = AstrMessageEvent(sender_id="admin1")
        outs = run_handler(plugin.qa_status(event))
        assert "未接入" in outs[0][1]


class TestSyncWithFakeGateway:
    """注入假网关验证 sync_once 全链路(fetch→parse→commit)。"""



    def _plugin(self, tmp_path: Path, monkeypatch, gateway) -> FeishuQaPlugin:
        monkeypatch.setenv("ASTRBOT_DATA_DIR", str(tmp_path))
        plugin = FeishuQaPlugin(
            context=None,
            config={"ENABLED_GROUPS": ["*"], "SYNC_INTERVAL_HOURS": 0, "AUTH_CHECK_HOURS": 0},
        )
        from astrbot_plugin_feishu_qa.adapter.gateway import GatewayClient

        plugin.adapter = GatewayClient(
            doc_ref="https://my.feishu.cn/wiki/test", resolver=lambda: gateway
        )
        return plugin

    def test_sync_once_full_pipeline(self, tmp_path: Path, monkeypatch) -> None:
        from test_adapter import FakeGateway

        xml = (FIXTURES / "qa_r8268.xml").read_text()
        gw = FakeGateway(fetch_results={"https://my.feishu.cn/wiki/test": xml})
        plugin = self._plugin(tmp_path, monkeypatch, gw)

        result = asyncio.run(plugin.sync_once())

        assert result["status"] == "synced"
        assert result["image_failures"] == 0
        assert result["added"] >= 35
        assert (
            "fetch_doc",
            "https://my.feishu.cn/wiki/test",
            "xml",
            "with-ids",
        ) in gw.calls
        # 同步后检索器已热替换为快照内容
        assert plugin._retriever is not None and len(plugin._entries) >= 35

    def test_sync_twice_reports_unchanged(self, tmp_path: Path, monkeypatch) -> None:
        from test_adapter import FakeGateway

        xml = (FIXTURES / "qa_r8268.xml").read_text()
        gw = FakeGateway(fetch_results={"https://my.feishu.cn/wiki/test": xml})
        plugin = self._plugin(tmp_path, monkeypatch, gw)

        asyncio.run(plugin.sync_once())
        result2 = asyncio.run(plugin.sync_once())
        assert result2["status"] == "unchanged"

    def test_sync_heals_missing_images(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """快照未变但图片文件丢失时,不得走 unchanged 快速路径,必须补下载。"""
        from test_adapter import FakeGateway

        xml = (FIXTURES / "qa_r8268.xml").read_text()
        gw = FakeGateway(fetch_results={"https://my.feishu.cn/wiki/test": xml})
        plugin = self._plugin(tmp_path, monkeypatch, gw)

        assert asyncio.run(plugin.sync_once())["status"] == "synced"
        # 模拟数据丢失:删掉全部已下载图片
        for f in (Path(plugin.data_root) / "images").glob("*"):
            f.unlink()
        calls_before = len([c for c in gw.calls if c[0] == "download_media"])

        result2 = asyncio.run(plugin.sync_once())

        assert result2["status"] == "synced", "缺图时不得快速返回 unchanged"
        redownloaded = len([c for c in gw.calls if c[0] == "download_media"])
        assert redownloaded > calls_before, "必须重新发起图片下载"
