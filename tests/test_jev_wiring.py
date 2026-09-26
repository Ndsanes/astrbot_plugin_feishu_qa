"""Jev 接线测试(不联网、不需要 key,靠注入假 transport)。

纯策略在 test_jev_policy.py 已覆盖;这里覆盖**接线**——bug 恰恰住在
"判完之后有没有真的改行为"这一步,纯函数测试看不见。

覆盖的不变量:
  - Jev 说够格 → 真的自己答了并阻断 Agent;
  - Jev 说交回 → 真的不答不阻断;
  - Jev 不可用/未启用 → 行为与 v0.9.0 完全一致(走本地判定);
  - API key 绝不出现在决策日志里。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot.api.event import AstrMessageEvent

from astrbot_plugin_feishu_qa.jev.client import JevClient
from astrbot_plugin_feishu_qa.jev.policy import SYNTHESIS_QID, sufficient_qid
from astrbot_plugin_feishu_qa.main import FeishuQaPlugin

FIXTURES = Path(__file__).parent / "fixtures"

# MEDIUM 区的真实问题(2026-09-26 实测 3.56 分,top-1 为错答)
MEDIUM_QUERY = "效果器怎么添加"
# HIGH 区(实测 20.31)
HIGH_QUERY = "声卡设置没问题但是cakewalk就是没声音"
# 本地 linkable_entries 闸门实测放行 2 条的查询(用于 D 位点的"与 v0.9.0 一致"对照)
GATE_PASSING_QUERY = "cakewalk没有自带模板怎么办"


def _answer(synthesis: float, sufficients: list[float], *, n: int | None = None) -> dict:
    # 候选数不足时补 0.0:_usable 要求"全部候选都答到",少一条就算不可用,
    # 这是刻意设计(缺失不等于低分),测试里必须把每条都答上。
    vals = list(sufficients) + [0.0] * max(0, (n or len(sufficients)) - len(sufficients))
    nouls = {SYNTHESIS_QID: synthesis}
    for i, v in enumerate(vals):
        nouls[sufficient_qid(i)] = v
    return {
        "model": "jev-1.13.0",
        "answers": {
            qid: {"type": "noul", "noul": v} for qid, v in nouls.items()
        },
        "usage": {"input_tokens": 1200, "output_tokens": 30},
    }


def _patch_jev(monkeypatch, payload=None, *, raises: Exception | None = None):
    """让插件里的 JevClient 走假 transport。记录每次调用以便断言。"""
    calls: list[dict] = []

    def fake(url, headers, body, timeout):
        calls.append(json.loads(body))
        if raises is not None:
            raise raises
        return payload

    monkeypatch.setattr(JevClient, "_http_post", staticmethod(fake))
    return calls


@pytest.fixture()
def make_plugin(tmp_path: Path, monkeypatch):
    def _make(**cfg_extra):
        monkeypatch.setenv("ASTRBOT_DATA_DIR", str(tmp_path))
        config = {
            "WIKI_URL": "https://my.feishu.cn/wiki/test",
            "ENABLED_GROUPS": ["*"],
            "ADMIN_USERS": ["admin1"],
            "SYNC_INTERVAL_HOURS": 0,
        }
        config.update(cfg_extra)
        p = FeishuQaPlugin(context=None, config=config)
        from astrbot_plugin_feishu_qa.corpus.builder import build_manifest
        from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
        from astrbot_plugin_feishu_qa.storage.snapshot import SnapshotStore

        xml = (FIXTURES / "qa_r8268.xml").read_text()
        parsed = parse_xml(xml, source_revision=8268)
        SnapshotStore(p.data_root).commit(
            build_manifest(parsed, revision_id=8268, document_id="doc")
        )
        assert p._load_corpus() is True
        return p

    return _make


def _at_event(query: str = MEDIUM_QUERY) -> AstrMessageEvent:
    """@ 唤醒已被 AstrBot 内核 WakingCheckStage 剥掉(见 v0.8.8 修复记录),
    handler 收到的是裸问题;`_strip_wake` 只做 strip。"""
    ev = AstrMessageEvent()
    ev.message_str = query
    ev.is_at_or_wake_command = True
    return ev


def _sent_text(ev) -> str:
    """取出已发送链里的全部文本。

    链有两种形态:合并转发 Node(Node.content 里的 Plain),或 qq_official 的
    raw ("plain", text) 元组。这里都收,避免测试绑死其中一种。
    """
    out: list[str] = []
    for item in ev.sent[0].chain:
        if isinstance(item, tuple):
            out.append(str(item[1]))
        elif hasattr(item, "content"):
            out.extend(
                str(getattr(c, "text", "")) for c in item.content
                if getattr(c, "text", None)
            )
    return "\n".join(out)


def _run(coro):
    import asyncio

    async def consume():
        if hasattr(coro, "__anext__"):
            async for _ in coro:
                pass
        else:
            await coro

    asyncio.run(consume())


def _decisions(plugin) -> list[dict]:
    return plugin._decision_log.read_all()


# ── C 位点 ──


class TestHookC:
    def test_jev_says_sufficient_bot_answers(self, make_plugin, monkeypatch) -> None:
        calls = _patch_jev(monkeypatch, _answer(0.0, [0.95, 0.02, 0.01], n=3))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        ev = _at_event()
        _run(p.on_group_message(ev))
        assert len(ev.sent) == 1, "Jev 判定够格就必须自己答"
        assert ev.stopped is True, "自己答了必须阻断主 Agent"
        assert "可能与你的问题相关" in _sent_text(ev)
        assert len(calls) == 1, "应只发一次 Jev 调用(一次并行求值全部问题)"

    def test_jev_says_synthesis_hands_off(self, make_plugin, monkeypatch) -> None:
        _patch_jev(monkeypatch, _answer(0.95, [0.9, 0.1, 0.1], n=3))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        ev = _at_event()
        _run(p.on_group_message(ev))
        assert ev.sent == [], "需要综合时不得自己答"
        assert ev.stopped is not True, "交回主 Agent 就不能 stop_event"

    def test_jev_none_sufficient_hands_off(self, make_plugin, monkeypatch) -> None:
        _patch_jev(monkeypatch, _answer(0.0, [0.1, 0.05, 0.02], n=3))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        ev = _at_event()
        _run(p.on_group_message(ev))
        assert ev.sent == []
        assert ev.stopped is not True

    def test_one_call_carries_synthesis_and_all_candidates(
        self, make_plugin, monkeypatch
    ) -> None:
        calls = _patch_jev(monkeypatch, _answer(0.0, [0.9, 0.1, 0.1], n=3))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        _run(p.on_group_message(_at_event()))
        body = calls[0]
        assert SYNTHESIS_QID in body["questions"]
        assert sufficient_qid(0) in body["questions"]
        assert body["state"]["question"] == MEDIUM_QUERY
        assert len(body["state"]["candidates"]) >= 1

    def test_jev_timeout_falls_back_to_local_switch(
        self, make_plugin, monkeypatch
    ) -> None:
        """Jev 挂掉时,行为必须与 v0.9.0 一致:由 TENTATIVE_ANSWER_ENABLED 决定。"""
        _patch_jev(monkeypatch, raises=TimeoutError())
        p = make_plugin(
            JEV_ENABLED=True, JEV_API_KEY="k", TENTATIVE_ANSWER_ENABLED=True
        )
        ev = _at_event()
        _run(p.on_group_message(ev))
        assert len(ev.sent) == 1, "Jev 挂了仍应由本地模糊档接管"
        assert ev.stopped is True

    def test_jev_timeout_and_switch_off_behaves_like_v090(
        self, make_plugin, monkeypatch
    ) -> None:
        _patch_jev(monkeypatch, raises=TimeoutError())
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k", TENTATIVE_ANSWER_ENABLED=False)
        ev = _at_event()
        _run(p.on_group_message(ev))
        assert ev.sent == []
        assert ev.stopped is not True

    def test_jev_disabled_makes_no_call(self, make_plugin, monkeypatch) -> None:
        calls = _patch_jev(monkeypatch, _answer(0.0, [0.99], n=3))
        p = make_plugin(JEV_ENABLED=False, JEV_API_KEY="k")
        ev = _at_event()
        _run(p.on_group_message(ev))
        assert calls == [], "未启用时不得发起任何 Jev 调用"
        assert ev.sent == []

    def test_enabled_without_key_is_treated_as_disabled(
        self, make_plugin, monkeypatch
    ) -> None:
        calls = _patch_jev(monkeypatch, _answer(0.0, [0.99], n=3))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="")
        assert p._jev is None
        _run(p.on_group_message(_at_event()))
        assert calls == []

    def test_high_query_never_reaches_jev(self, make_plugin, monkeypatch) -> None:
        """HIGH 走直答,不该为它花一次 Jev 调用。"""
        calls = _patch_jev(monkeypatch, _answer(0.0, [0.9], n=3))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        ev = _at_event(HIGH_QUERY)
        _run(p.on_group_message(ev))
        assert calls == [], "高置信直答不经过 Jev"
        assert ev.stopped is True

    def test_decision_log_records_jev_action_and_nouls(
        self, make_plugin, monkeypatch
    ) -> None:
        _patch_jev(monkeypatch, _answer(0.0, [0.95, 0.02], n=3))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        _run(p.on_group_message(_at_event()))
        rows = [r for r in _decisions(p) if r["action"].startswith("jev_")]
        assert rows, "必须落 jev_* 记录"
        assert rows[-1]["extra"]["jev_action"] == "answer_self"
        assert SYNTHESIS_QID in rows[-1]["extra"]["jev_nouls"]
        assert rows[-1]["extra"]["jev_picked"]

    def test_api_key_never_written_to_decision_log(
        self, make_plugin, monkeypatch
    ) -> None:
        _patch_jev(monkeypatch, _answer(0.0, [0.9], n=3))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="SUPER-SECRET-KEY")
        _run(p.on_group_message(_at_event()))
        raw = p._decision_log.path.read_text(encoding="utf-8")
        assert "SUPER-SECRET-KEY" not in raw


# ── D 位点 ──


def _kb_result(codes: list[str]):
    text = "".join(
        f"【全家桶FAQ > 章节】标题\n正文\n[ref:{c}]\n相关度: 1.00\n\n" for c in codes
    )
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def _tool(name="astr_kb_search"):
    return SimpleNamespace(name=name)


class TestHookD:
    def _two_entries(self, p):
        return [e for e in p._entries if e.source_locator][:2]

    def test_jev_selects_only_high_noul_entries(self, make_plugin, monkeypatch) -> None:
        # 候选0 本地分更高但 noul 更低 → Jev 应当否掉它
        _patch_jev(monkeypatch, _answer(0.0, [0.10, 0.95], n=2))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        entries = self._two_entries(p)
        ev = _at_event()
        _run(
            p.auto_send_faq_links(
                ev, _tool(), None, _kb_result([entries[0].id[3:8], entries[1].id[3:8]])
            )
        )
        text = _sent_text(ev)
        assert len(ev.sent) == 1
        assert entries[0].source_locator not in text, "noul 不够的候选不应附"
        assert entries[1].source_locator in text

    def test_jev_suppresses_all_links(self, make_plugin, monkeypatch) -> None:
        _patch_jev(monkeypatch, _answer(0.0, [0.05, 0.02], n=2))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        entries = self._two_entries(p)
        ev = _at_event()
        _run(
            p.auto_send_faq_links(
                ev, _tool(), None, _kb_result([entries[0].id[3:8], entries[1].id[3:8]])
            )
        )
        assert ev.sent == [], "Jev 认为都不够格时不得发任何链接"
        rows = [r for r in _decisions(p) if r["action"] == "jev_links_suppressed"]
        assert rows, "抑制必须留痕"

    def test_jev_unusable_falls_back_to_local_gate(
        self, make_plugin, monkeypatch
    ) -> None:
        _patch_jev(monkeypatch, raises=OSError("down"))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        entries = self._two_entries(p)
        ev = _at_event(GATE_PASSING_QUERY)
        _run(
            p.auto_send_faq_links(
                ev, _tool(), None, _kb_result([entries[0].id[3:8], entries[1].id[3:8]])
            )
        )
        # 回落 linkable_entries:该问题与条目同域,本地闸门应放行
        assert len(ev.sent) == 1, "Jev 挂了必须回落到本地闸门,不能整个不附链"

    def test_jev_disabled_uses_local_gate_only(self, make_plugin, monkeypatch) -> None:
        calls = _patch_jev(monkeypatch, _answer(0.0, [0.0, 0.0], n=2))
        p = make_plugin(JEV_ENABLED=False, JEV_API_KEY="k")
        entries = self._two_entries(p)
        ev = _at_event(GATE_PASSING_QUERY)
        _run(
            p.auto_send_faq_links(
                ev, _tool(), None, _kb_result([entries[0].id[3:8], entries[1].id[3:8]])
            )
        )
        assert calls == []
        assert len(ev.sent) == 1, "未启用 Jev 时行为与 v0.9.0 一致(本地闸门放行)"

    def test_d_uses_user_original_question_not_retrieval_query(
        self, make_plugin, monkeypatch
    ) -> None:
        """9·15 事故的根因守卫:state 里必须是用户原问题。"""
        calls = _patch_jev(monkeypatch, _answer(0.0, [0.9, 0.9], n=2))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        entries = self._two_entries(p)
        ev = _at_event()
        _run(
            p.auto_send_faq_links(
                ev, _tool(), None, _kb_result([entries[0].id[3:8], entries[1].id[3:8]])
            )
        )
        assert calls, "D 位点应发起调用"
        assert calls[0]["state"]["question"] == MEDIUM_QUERY
        assert "@小脑呆" not in calls[0]["state"]["question"], "必须剥掉 @ 唤醒"

    def test_d_candidate_text_is_truncated(self, make_plugin, monkeypatch) -> None:
        """长 state 会稀释准确率,候选正文必须截断。"""
        calls = _patch_jev(monkeypatch, _answer(0.0, [0.9, 0.9], n=2))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        entries = self._two_entries(p)
        ev = _at_event()
        _run(
            p.auto_send_faq_links(
                ev, _tool(), None, _kb_result([entries[0].id[3:8], entries[1].id[3:8]])
            )
        )
        for c in calls[0]["state"]["candidates"]:
            assert len(c["text"]) <= 600
