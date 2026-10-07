"""插件主体测试:指令解析、语料装载、直答链路、tool schema(spec §60)。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot.api.event import AstrMessageEvent  # 桩包(或真实包)
from astrbot.api.message_components import Reply

from astrbot_plugin_feishu_qa.corpus.model import normalize_title
from astrbot_plugin_feishu_qa.learn.candidate import MAX_HISTORY_CHARS
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

    def test_chitchat_marked_not_qa_relevant(self, plugin: FeishuQaPlugin) -> None:
        event = AstrMessageEvent(message_str="@bot ping!", group_id="g1")
        event.is_at_or_wake_command = True
        run_handler(plugin.on_group_message(event))
        assert not event.is_stopped(), "未命中高置信度直达的消息放行给主 Agent"
        assert event.get_extra("_qa_relevant") is False, "日常寒暄 ping! 必须标记为非 QA 业务提问"


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
        ev = AstrMessageEvent()
        ev.set_extra("_qa_relevant", True)
        run_handler(plugin.add_faq_citation_guidance(ev, req))
        assert req.system_prompt.startswith("base-prompt")
        assert "章节直达链接" in req.system_prompt
        assert "qa_send_answer" not in req.system_prompt  # 附链已自动化,模型无需调用

    def test_guidance_handles_empty_system_prompt(self, plugin) -> None:
        class Req:
            system_prompt = ""

        req = Req()
        ev = AstrMessageEvent()
        ev.set_extra("_qa_relevant", True)
        run_handler(plugin.add_faq_citation_guidance(ev, req))
        assert req.system_prompt.startswith("\n[FAQ 引用规范]")

    def test_chitchat_drops_qa_tool_and_skips_guidance(self, plugin: FeishuQaPlugin) -> None:
        """日常闲聊/非问答:剥离 qa_send_answer 工具且不追加 FAQ 提示词。"""
        class MockToolSet:
            def __init__(self) -> None:
                self.tools = ["qa_send_answer", "send_message_to_user"]

            def remove_tool(self, name: str) -> None:
                self.tools = [t for t in self.tools if t != name]

        class Req:
            def __init__(self) -> None:
                self.system_prompt = "base-prompt"
                self.func_tool = MockToolSet()

        req = Req()
        ev = AstrMessageEvent()
        run_handler(plugin.add_faq_citation_guidance(ev, req))

        assert req.system_prompt == "base-prompt"
        assert "qa_send_answer" not in req.func_tool.tools
        assert "send_message_to_user" in req.func_tool.tools



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
        assert isinstance(out[0], str) and "勿复述" in out[0]
        assert len(event.sent) == 1
        all_text = "".join(
            c[1]
            for c in event.sent[0].chain
            if isinstance(c, tuple) and c[0] == "plain"
        )
        expected = (
            f"1. [{normalize_title(entry.raw_title)}]"
            f"(https://my.feishu.cn/wiki/test#{entry.source_locator})"
        )
        assert expected in all_text
        # 四条投递路径统一格式后,头部只有这一个
        assert "以下章节与你的问题相关:" in all_text
        assert "来源:肖闻 Xiaowenn 的 Q&A 文档" in all_text
        # 回归:raw_title 自带 "1、",直接拼进列表会渲染成 "1. [1、【x】](url)"
        assert f"[{entry.raw_title}]" not in all_text


    def test_generic_platform_falls_back_to_bare_url(
        self, plugin: FeishuQaPlugin
    ) -> None:
        entry = next(e for e in plugin._entries if e.source_locator)
        event = AstrMessageEvent()  # 桩无平台名 → 非 qq_official 分支
        run_handler(plugin.qa_send_answer(event, entry_ids=entry.id))
        all_text = "".join(
            c.text for c in event.sent[0].chain[0].content if c.type == "Plain"
        )
        assert f"、{normalize_title(entry.raw_title)}👉 " in all_text
        assert f"👉 https://my.feishu.cn/wiki/test#{entry.source_locator}" in all_text
        assert "](" not in all_text, "非官方平台不得输出 markdown 字面量"
        # 回归:裸链接分支不得把条目自身序号拼进列表,也不得给已带症状标签的
        # 标题再套一层 【】(那会渲染成 【【tag】…】)
        assert f"、{entry.raw_title}" not in all_text
        assert "【【" not in all_text

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
            assert normalize_title(e.raw_title) in all_text

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
        assert f"[{normalize_title(entry.raw_title)}]" \
            f"(https://my.feishu.cn/wiki/test#{entry.source_locator})" in joined
        # 回归:自动附链同样不得把条目自身序号拼进列表
        assert f"[{entry.raw_title}]" not in joined

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


class TestAutoFaqLinksRelevance:
    """自动附链的相关性闸门(2026-09-15 线上误附事故)。

    线上事故:用户问"Cakewalk 的混音台怎么打开",模型检索后,插件把检索结果里
    前三条 FAQ 直接当链接发出——分别是"连接MIDI设备""Clip重叠""搜索不到自带音源",
    与提问毫无关系。根因是附链只顾检索顺序,而 KB 的相关度是**为该次检索词**服务的:
    靠前的 FAQ 只说明它与"Cakewalk"同域,不代表答得上用户的问题。
    现要求:用**用户原问题**本地判定,须同时满足 本地 MEDIUM+ 且 头部覆盖主题词。
    """

    # 事故现场:用户问混音台,模型检索返回这三个不该附链的条目
    INCIDENT_CODES = ["c1a91", "c118a", "8a197"]
    INCIDENT_QUESTION = "Cakewalk 的混音台怎么打开"

    def _tool_result(self, codes):
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    text="".join(
                        f"【全家桶FAQ > 章节】标题\n正文\n[ref:{c}]\n相关度: 1.00\n\n"
                        for c in codes
                    )
                )
            ]
        )

    def _tool(self, name="astr_kb_search"):
        return SimpleNamespace(name=name)

    def _send(self, plugin, question, codes):
        event = AstrMessageEvent(message_str=question)
        run_handler(
            plugin.auto_send_faq_links(
                event, self._tool(), None, self._tool_result(codes)
            )
        )
        return event

    @staticmethod
    def _sent_text(event) -> str:
        """取已发消息的文本:兼容 合并转发(Node) 与 普通链 两种形态。"""
        parts = []
        for chain in event.sent:
            for item in chain.chain:
                if isinstance(item, tuple) and item[0] == "plain":
                    parts.append(item[1])
                elif getattr(item, "content", None):
                    parts.extend(
                        c.text for c in item.content if getattr(c, "text", None)
                    )
        return "\n".join(parts)

    def test_incident_question_sends_no_links(self, plugin) -> None:
        """问混音台时,不得把"连接MIDI设备/Clip重叠/自带音源"当答案附上。"""
        event = self._send(plugin, self.INCIDENT_QUESTION, self.INCIDENT_CODES)
        assert event.sent == [], (
            "无关条目被当作直达章节发出:"
            + str([e.raw_title for e in plugin._entries])
        )

    def test_mixed_candidates_only_relevant_survives(self, plugin) -> None:
        """候选中混入真相关条目时,只附相关的那个。"""
        event = self._send(
            plugin, "cakewalk 怎么登录激活", ["c1a91", "63591", "c118a"]
        )
        assert len(event.sent) == 1
        joined = self._sent_text(event)
        assert "登录激活" in joined
        assert "Clip重叠" not in joined and "先按这边操作" not in joined

    def test_links_sorted_by_relevance_not_retrieval_order(self, plugin) -> None:
        """检索顺序可能把边缘条目排前面;输出须按本地相关度降序。"""
        # 两个都真相关,但**传入顺序相反**;两种输入都应得到同一输出顺序,
        # 证明排序依据是相关度而非检索顺序。
        for codes in (["53903", "63591"], ["63591", "53903"]):
            event = self._send(plugin, "cakewalk 怎么登录激活", codes)
            joined = self._sent_text(event)
            assert "如何登录激活" in joined and "无法激活" in joined
            assert joined.index("如何登录激活") < joined.index("无法激活"), (
                f"附链未按相关度降序排列(input={codes})"
            )

    def test_question_without_topic_words_still_links(self, plugin) -> None:
        """纯领域词提问(无主题词)时闸门不生效,避免把正常提问一律拒绝。"""
        event = self._send(plugin, "cakewalk", ["63591", "8a197"])
        assert len(event.sent) == 1, "无主题词的正常提问不应被误拦"

    def test_no_question_falls_back_without_crash(self, plugin) -> None:
        """事件无正文时无法判定,退化为按检索顺序截断(不得异常)。"""
        event = self._send(plugin, "", ["63591"])
        assert len(event.sent) == 1


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


class TestLearnMaterialResolution:
    """/learn 素材取用优先级(2026-09-16)。

    实测确认:官方 QQ 机器人在**服务端**就把合并转发展开成纯文本
    (message_type=102,[群聊的聊天记录] + === 消息 N === + [发送者]),
    随 content 下发,所以插件拿 message_str 即完整转录——这条路既
    不需要开 group_message_history_enable,也不落额外存储。
    优先级:聊天记录转录 > 引用消息 > 平台历史(aiocqhttp 专属)。
    """

    TRANSCRIPT = (
        "[At:qq_official] [群聊的聊天记录]\n"
        "=== 消息 1 ===\n[消息内容] 混音台怎么开\n[发送者] 群友A\n\n"
        "=== 消息 2 ===\n[消息内容] 顶部菜单 视图→控制台,或按 Alt+2\n[发送者] 小闻\n"
    )

    CANDIDATE = (
        '{"is_candidate": true, "question": "混音台怎么打开",'
        ' "answer": "顶部菜单 视图→控制台,或按 Alt+2",'
        ' "symptom_tags": ["混音台"], "category": "unknown",'
        ' "evidence": ["群友A: 混音台怎么开"], "confidence": 0.9}'
    )

    class _LLM:
        """记录 prompt 的假 LLM:断言素材是否真的喂进去了。"""

        def __init__(self, reply):
            self.prompts: list[str] = []
            self._reply = reply

        async def get_current_chat_provider_id(self, umo=None):
            return "fake"

        async def llm_generate(self, chat_provider_id=None, prompt=None):
            self.prompts.append(prompt or "")
            return SimpleNamespace(completion_text=self._reply)

    @pytest.fixture()
    def learned(self, plugin: FeishuQaPlugin):
        llm = self._LLM(self.CANDIDATE)
        plugin.context = llm
        return plugin, llm

    def _event(self, message_str, messages=None, sender_id="admin1"):
        """管理员事件:plugin fixture 的 ADMIN_USERS 是 ["admin1"]。"""
        return AstrMessageEvent(
            message_str=message_str, sender_id=sender_id, messages=messages
        )

    def test_transcript_is_used_without_history_api(self, learned) -> None:
        """核心断言:转录直接进 prompt,不依赖 aiocqhttp 历史接口。"""
        plugin, llm = learned
        outs = run_handler(plugin.learn(self._event(self.TRANSCRIPT)))
        assert llm.prompts, "未调用 LLM"
        assert "视图→控制台" in llm.prompts[0], "转录内容没有进入 prompt"
        assert "素材来源: 聊天记录转录" in outs[0][1]

    def test_quoted_message_used_when_no_transcript(self, learned) -> None:
        """引用路径:被引用内容作为素材。"""
        plugin, llm = learned
        quote = Reply(message_str="导出报错就换成 44.1kHz 再导一次")
        event = self._event("/learn", messages=[quote])
        outs = run_handler(plugin.learn(event))
        assert llm.prompts and "44.1kHz" in llm.prompts[0]
        assert "素材来源: 引用消息" in outs[0][1]

    def test_no_material_gives_actionable_hint(self, learned) -> None:
        """三条路都取不到时,必须给出可操作的用法提示(而非死胡同)。"""
        plugin, llm = learned
        outs = run_handler(plugin.learn(self._event("/learn")))
        text = outs[0][1]
        assert "合并转发" in text and "引用" in text
        assert not llm.prompts, "无素材时不应调用 LLM"

    def test_non_admin_rejected(self, learned) -> None:
        plugin, llm = learned
        outs = run_handler(plugin.learn(self._event(self.TRANSCRIPT, sender_id="nobody")))
        assert "仅管理员" in outs[0][1]
        assert not llm.prompts

    def test_long_transcript_not_silently_dropped(self, learned) -> None:
        """超长转录(>4000 字)曾是静默失败:prompt 素材区为空。"""
        plugin, llm = learned
        big = (
            "[群聊的聊天记录]\n"
            + "\n".join(
                f"=== 消息 {i} ===\n[消息内容] 问题内容{i}\n[发送者] 群友{i}"
                for i in range(1, 130)
            )
        )
        assert len(big) > MAX_HISTORY_CHARS
        run_handler(plugin.learn(self._event(big)))
        body = llm.prompts[0].split("消息记录:")[1].split("输出格式:")[0].strip()
        assert body, "超长素材被静默丢弃"


class TestLearnConfirmFlow:
    """`/learn ok` 确认分支(2026-09-16 线上实测暴露的解析 bug)。

    线上现象:候选正常产出,但 `/learn ok` 回复"没能取到可分析的素材"。
    根因:WakingCheckStage 在唤醒检查时**已把 wake_prefix("/")剥掉**
    (waking_check/stage.py:130),handler 收到的是 `learn ok`,而旧
    `_strip_command` 只认 "问" 系列前缀,剥不出 "ok" → 确认/放弃分支永不命中
    → 一路掉进素材分析分支。已在 _strip_command 增加显式指令名参数。
    """

    def test_learn_ok_detected(self) -> None:
        f = FeishuQaPlugin._strip_command
        for raw in ("learn ok", "/learn ok"):
            assert f(raw, "learn", "学习").lower() in ("ok", "确认", "yes"), raw

    def test_learn_no_detected(self) -> None:
        f = FeishuQaPlugin._strip_command
        for raw in ("learn no", "/learn no"):
            assert f(raw, "learn", "学习").lower() in ("no", "取消", "放弃"), raw

    def test_ask_command_args(self) -> None:
        f = FeishuQaPlugin._strip_command
        assert f("/问 xxx", "问", "qa", "Q&A") == "xxx"
        assert f("问 xxx", "问", "qa", "Q&A") == "xxx"
        assert f("qa xxx", "问", "qa", "Q&A") == "xxx"
        assert f("问 cakewalk 没声音", "问", "qa", "Q&A") == "cakewalk 没声音"

    def test_bare_command_yields_empty(self) -> None:
        f = FeishuQaPlugin._strip_command
        assert f("learn", "learn", "学习") == ""
        assert f("/learn", "learn", "学习") == ""

    def test_plain_chat_untouched(self) -> None:
        f = FeishuQaPlugin._strip_command
        assert f("普通聊天", "learn", "学习") == "普通聊天"

    def test_学习_alias_registered(self) -> None:
        """`/学习` 必须是 learn 的别名。

        线上实测(01:29):用户发 `/学习`,但当时只注册了 `learn` 一个指令名,
        命令过滤器不认 → handler 从未触发 → 消息流进 LLM,机器人只回了句
        "学到了 记下("。而 `on_group_message` 在群里只处理 @ 唤醒,不管指令,
        所以整条链路静默走偏。
        """
        meta = getattr(FeishuQaPlugin.learn, "_filter", None)
        assert meta is not None, "learn 未注册为指令"
        kind, args, kwargs = meta
        assert kind == "command"
        names = {args[0]} | set(kwargs.get("alias") or set())
        assert "学习" in names, f"/学习 未注册为别名(当前: {names})"
        assert "learn" in names


class TestLearnConfirmWithPending:
    """确认分支的端到端行为(不落盘/不写回,只看分支走向)。"""

    class _LLM:
        def __init__(self, reply):
            self.prompts = []
            self._reply = reply

        async def get_current_chat_provider_id(self, umo=None):
            return "fake"

        async def llm_generate(self, chat_provider_id=None, prompt=None):
            self.prompts.append(prompt or "")
            return SimpleNamespace(completion_text=self._reply)

    CANDIDATE = TestLearnMaterialResolution.CANDIDATE

    def test_ok_without_pending_says_so(self, plugin, monkeypatch) -> None:
        """没有待确认候选时,ok 应提示先跑分析,而不是走素材分支。"""
        plugin.context = self._LLM(self.CANDIDATE)
        outs = run_handler(
            plugin.learn(AstrMessageEvent(message_str="learn ok", sender_id="admin1"))
        )
        assert "没有待确认的候选" in outs[0][1]

    def test_ok_with_pending_records_candidate(self, plugin, tmp_path) -> None:
        """有候选时,ok 走收录分支(未配写回文档 → 落 pending_learn.json)。"""
        plugin.context = self._LLM(self.CANDIDATE)
        run_handler(
            plugin.learn(
                AstrMessageEvent(
                    message_str=TestLearnMaterialResolution.TRANSCRIPT,
                    sender_id="admin1",
                    group_id="g1",
                )
            )
        )
        assert plugin._pending_learn, "分析阶段应产出候选"
        outs = run_handler(
            plugin.learn(
                AstrMessageEvent(
                    message_str="learn ok", sender_id="admin1", group_id="g1"
                )
            )
        )
        text = outs[0][1]
        assert "没有待确认" not in text, f"ok 未命中确认分支: {text}"
        assert "已收录" in text or "写回" in text
        assert (plugin.data_root / "pending_learn.json").is_file()


class TestCommandTextNotTreatedAsQuestion:
    """指令路径守卫:任何 `/` 开头的文本都不得被当成提问送进判定链路。

    线上实测(2026-09-27):`/jev_probe midi 键盘怎么连 cakewalk` 被 @ 之后走的是
    `on_group_message` 而非命令处理器,探针文本里的 "midi 键盘怎么连" 被判成
    真实提问并触发了 Jev——恰好答对是运气。原守卫只放过 /问 与 /qa。
    """

    @pytest.mark.parametrize(
        "text",
        [
            "/jev_probe midi 键盘怎么连", "/qa_sync", "/qa_reload",
            "/learn", "/学习 ok", "/qa_status",
        ],
    )
    def test_slash_prefixed_text_returns_without_routing(
        self, plugin: FeishuQaPlugin, text: str
    ) -> None:
        event = AstrMessageEvent(message_str=text)
        event.is_at_or_wake_command = True
        run_handler(plugin.on_group_message(event))
        assert event.sent == [], f"{text} 不应被当成提问投递"
        assert event.stopped is False

    def test_normal_question_still_routed(self, plugin: FeishuQaPlugin) -> None:
        event = AstrMessageEvent(message_str="cakewalk没声音怎么办")
        event.is_at_or_wake_command = True
        run_handler(plugin.on_group_message(event))
        assert len(event.sent) == 1, "普通提问仍应走直答"
        assert event.stopped is True


class TestQaSendAnswerDedup:
    """会话级去重:自动附链已发过的条目,模型再点不再重发。

    线上实测 2026-09-27:用户问"cakewalk 打不开了",Jev 闸门发出【打开就闪退】,
    主 Agent 随后又调 qa_send_answer 重发同一条,用户收到两条重复的链接消息。
    `qa_send_answer` 原先从不检查 `_faq_links_sent`——那是自动附链专用的。
    """

    def test_skips_entry_already_sent_by_auto_links(self, plugin: FeishuQaPlugin) -> None:
        entry = next(e for e in plugin._entries if e.source_locator)
        event = AstrMessageEvent(message_str="cakewalk打不开了")
        event.set_extra("_faq_links_sent", {entry.id})
        out = run_handler(plugin.qa_send_answer(event, entry_ids=entry.id))
        assert "未重复投递" in out[0]
        assert event.sent == [], "已发过的条目不得重复投递"

    def test_still_sends_fresh_entries(self, plugin: FeishuQaPlugin) -> None:
        picks = [e for e in plugin._entries if e.source_locator][:2]
        event = AstrMessageEvent(message_str="问题")
        event.set_extra("_faq_links_sent", {picks[0].id})
        out = run_handler(plugin.qa_send_answer(event, entry_ids=",".join(p.id for p in picks)))
        assert "已投递1条" in out[0], "只应投递未发过的那条"
        assert len(event.sent) == 1
        text = "".join(
            c.text for c in event.sent[0].chain[0].content if c.type == "Plain"
        )
        assert picks[1].source_locator in text
        assert picks[0].source_locator not in text

    def test_unknown_code_still_rejected(self, plugin: FeishuQaPlugin) -> None:
        event = AstrMessageEvent(message_str="x")
        out = run_handler(plugin.qa_send_answer(event, entry_ids="zzzzz"))
        assert "没有可投递" in out[0]
        assert event.sent == []

    def test_no_prior_sends_unchanged(self, plugin: FeishuQaPlugin) -> None:
        picks = [e for e in plugin._entries if e.source_locator][:2]
        event = AstrMessageEvent(message_str="问题")
        out = run_handler(plugin.qa_send_answer(event, entry_ids=",".join(p.id for p in picks)))
        assert "已投递2条" in out[0]
        assert len(event.sent) == 1
