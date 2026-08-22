"""插件主体测试:指令解析、语料装载、直答链路、tool schema(spec §60)。"""

from __future__ import annotations

import json
from pathlib import Path

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


class TestLlmToolSchema:
    def test_tool_registered_with_name_and_docstring(self, plugin: FeishuQaPlugin) -> None:
        fn = plugin.search_feishu_qa
        assert getattr(fn, "_llm_tool_name", "") == "search_feishu_qa"
        doc = (fn.__doc__ or "").strip()
        assert doc, "llm tool 必须有描述(AstrBot 解析生成 schema)"
        assert "Args:" in doc, "docstring 必须含 Args: 段否则参数 schema 为空"

    def test_search_tool_returns_structured_json(self, plugin: FeishuQaPlugin) -> None:
        event = AstrMessageEvent()
        outs = run_handler(plugin.search_feishu_qa(event, query="cakewalk 没声音"))
        payload = json.loads(outs[0][1])
        assert payload["matches"], "相关 query 应有命中"
        match = payload["matches"][0]
        for key in ("title", "section", "body"):
            assert key in match


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
