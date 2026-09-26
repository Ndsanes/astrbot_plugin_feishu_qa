"""决策日志 / 问题级记忆 / 模糊档文案测试(v0.9.0)。"""

from __future__ import annotations

import json
import time

import pytest

from astrbot_plugin_feishu_qa.answer.direct import (
    TENTATIVE_HEADER,
    format_tentative_links,
)
from astrbot_plugin_feishu_qa.corpus.model import normalize_title
from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
from astrbot_plugin_feishu_qa.retrieval.scorer import Retriever
from astrbot_plugin_feishu_qa.storage.decision_log import (
    CandidateRecord,
    DecisionCache,
    DecisionLog,
    DecisionRecord,
    normalize_query,
    query_hash,
)

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def entries():
    xml = (FIXTURES / "qa_r8268.xml").read_text()
    return parse_xml(xml, source_revision=8268).entries


# ── 规范化与哈希 ──


class TestNormalize:
    def test_whitespace_collapsed_and_lowercased(self) -> None:
        assert normalize_query("  CakeWalk   没声音 \n") == "cakewalk 没声音"

    def test_hash_is_stable_across_whitespace(self) -> None:
        assert query_hash("没声音") == query_hash("  没声音  ")

    def test_hash_distinguishes_different_questions(self) -> None:
        assert query_hash("混音台在哪") != query_hash("混音台怎么打开")

    def test_hash_is_16_hex(self) -> None:
        h = query_hash("任意问题")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)


# ── 落盘 ──


def _rec(**kw) -> DecisionRecord:
    base = dict(stage="route", action="direct", query="cakewalk 没声音怎么办")
    base.update(kw)
    return DecisionRecord(**base)


class TestDecisionLogWrite:
    def test_writes_jsonl(self, tmp_path) -> None:
        log = DecisionLog(tmp_path)
        log.record(_rec())
        lines = log.path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["action"] == "direct"

    def test_disabled_writes_nothing(self, tmp_path) -> None:
        log = DecisionLog(tmp_path, enabled=False)
        log.record(_rec())
        assert not log.path.exists()

    def test_disabled_flag_ignored_when_set(self, tmp_path) -> None:
        log = DecisionLog(tmp_path, enabled=True, text_mode="bogus")
        log.record(_rec())
        # 未知模式降级为 truncate,而不是抛异常
        assert log.path.is_file()


class TestDataMinimization:
    def test_truncate_cuts_text_and_flags_it(self, tmp_path) -> None:
        log = DecisionLog(tmp_path, text_mode="truncate", max_text_chars=10)
        log.record(_rec(query="这是一个非常非常长的用户提问内容需要被截断"))
        row = json.loads(log.path.read_text(encoding="utf-8").strip())
        assert len(row["q"]) == 10
        assert row["q_trunc"] is True

    def test_short_text_not_flagged(self, tmp_path) -> None:
        log = DecisionLog(tmp_path, text_mode="truncate", max_text_chars=200)
        log.record(_rec(query="短问题"))
        row = json.loads(log.path.read_text(encoding="utf-8").strip())
        assert row["q_trunc"] is False

    def test_hash_mode_keeps_no_plaintext(self, tmp_path) -> None:
        log = DecisionLog(tmp_path, text_mode="hash")
        log.record(_rec(query="我要偷偷藏起来的内容"))
        raw = log.path.read_text(encoding="utf-8")
        assert "偷偷藏起来" not in raw
        row = json.loads(raw.strip())
        assert row["q"] == ""
        # 聚合键仍可用
        assert row["qh"] == query_hash("我要偷偷藏起来的内容")

    def test_hash_key_present_in_all_modes(self, tmp_path) -> None:
        for mode in ("truncate", "hash", "full"):
            log = DecisionLog(tmp_path / mode, text_mode=mode)
            log.record(_rec())
            row = json.loads(log.path.read_text(encoding="utf-8").strip())
            assert row["qh"] == query_hash("cakewalk 没声音怎么办")


class TestRotation:
    def test_rotates_and_keeps_bounded_files(self, tmp_path) -> None:
        log = DecisionLog(tmp_path, max_bytes=1024, keep_files=2)
        for i in range(200):
            log.record(_rec(query=f"问题{i}" + "x" * 40))
        rotated = sorted(p.name for p in tmp_path.glob("decisions.jsonl*"))
        # keep_files 含主文件本身:主文件 + 至多 keep_files-1 份历史
        assert rotated == ["decisions.jsonl", "decisions.jsonl.1"]

    def test_total_volume_stays_bounded(self, tmp_path) -> None:
        log = DecisionLog(tmp_path, max_bytes=1024, keep_files=3)
        for i in range(500):
            log.record(_rec(query=f"问题{i}" + "x" * 60))
        total = sum(p.stat().st_size for p in tmp_path.glob("decisions.jsonl*"))
        # 每份最多在阈值附近翻倍(单条记录可能越过阈值),总量仍然有界
        assert total <= 1024 * 3 * 2

    def test_read_all_spans_rotated_files_without_loss(self, tmp_path) -> None:
        # 刚好触发一次轮转,且总容量仍够放 → 不该丢任何一条
        log = DecisionLog(tmp_path, max_bytes=1024, keep_files=3)
        for i in range(15):
            log.record(_rec(query=f"问题{i}"))
        assert (tmp_path / "decisions.jsonl.1").is_file()  # 确实轮转过
        rows = log.read_all()
        assert len(rows) == 15
        assert [r["q"] for r in rows[:3]] == ["问题0", "问题1", "问题2"]
        assert rows[-1]["q"] == "问题14"

    def test_read_all_drops_oldest_beyond_keep(self, tmp_path) -> None:
        log = DecisionLog(tmp_path, max_bytes=512, keep_files=2)
        for i in range(200):
            log.record(_rec(query=f"问题{i}"))
        rows = log.read_all()
        # 容量有限时丢弃最旧是预期行为,而不是 bug
        assert len(rows) < 200
        assert rows[-1]["q"] == "问题199"


class TestWriteObservable:
    def test_record_reports_success(self, tmp_path) -> None:
        log = DecisionLog(tmp_path)
        assert log.record(_rec()) is True
        assert log.path.is_file()

    def test_record_reports_failure_instead_of_raising(self, tmp_path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        assert DecisionLog(blocker / "nested").record(_rec()) is False

    def test_disabled_reports_false(self, tmp_path) -> None:
        assert DecisionLog(tmp_path, enabled=False).record(_rec()) is False


class TestNeverRaises:
    def test_unwritable_path_is_swallowed(self, tmp_path) -> None:
        # 把 data_root 指向一个不可写的路径,写盘必须静默失败而不是炸掉业务
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        log = DecisionLog(blocker / "nested")
        log.record(_rec())  # 不抛异常即通过
        assert not log.path.exists()


# ── 候选明细 ──


class TestCandidateRecord:
    def test_serialises_all_fields(self) -> None:
        c = CandidateRecord("qa_abc", "标题", 3.14159, "MEDIUM", True)
        d = c.to_dict()
        assert d == {
            "id": "qa_abc",
            "title": "标题",
            "score": 3.142,
            "conf": "MEDIUM",
            "supported": True,
        }

    def test_thresholds_and_revision_recorded(self, tmp_path) -> None:
        log = DecisionLog(tmp_path)
        log.record(
            DecisionRecord(
                stage="route",
                action="miss",
                query="q",
                thresholds={"high": 9.0, "medium": 3.0},
                revision=8268,
            )
        )
        row = json.loads(log.path.read_text(encoding="utf-8").strip())
        assert row["th"] == {"high": 9.0, "medium": 3.0}
        assert row["rev"] == 8268


# ── 问题级记忆 ──


class TestDecisionCache:
    def test_roundtrip(self) -> None:
        c = DecisionCache(ttl_seconds=60)
        c.put("问题", action="direct", entry_ids=["qa_1"], revision=8268)
        got = c.get("问题", revision=8268)
        assert got == {"action": "direct", "entry_ids": ("qa_1",)}

    def test_miss_returns_none(self) -> None:
        assert DecisionCache(ttl_seconds=60).get("没存过", revision=8268) is None

    def test_whitespace_insensitive(self) -> None:
        c = DecisionCache(ttl_seconds=60)
        c.put("没声音", action="direct", entry_ids=["qa_1"], revision=1)
        assert c.get("  没声音  ", revision=1) is not None

    def test_expired_returns_none(self) -> None:
        c = DecisionCache(ttl_seconds=60)
        now = time.time()
        c.put("问题", action="direct", entry_ids=["qa_1"], revision=1, now=now)
        assert c.get("问题", revision=1, now=now + 61) is None

    def test_revision_change_invalidates(self) -> None:
        c = DecisionCache(ttl_seconds=600)
        c.put("问题", action="direct", entry_ids=["qa_1"], revision=8268)
        # 语料同步过 → 旧判定对新语料不成立
        assert c.get("问题", revision=8394) is None

    def test_zero_ttl_disables(self) -> None:
        c = DecisionCache(ttl_seconds=0)
        c.put("问题", action="direct", entry_ids=["qa_1"], revision=1)
        assert c.get("问题", revision=1) is None

    def test_bounded_size(self) -> None:
        c = DecisionCache(ttl_seconds=600, max_items=8)
        for i in range(200):
            c.put(f"问题{i}", action="direct", entry_ids=[], revision=1)
        assert len(c._store) <= 8

    def test_clear(self) -> None:
        c = DecisionCache(ttl_seconds=600)
        c.put("问题", action="direct", entry_ids=[], revision=1)
        c.clear()
        assert c.get("问题", revision=1) is None


# ── 模糊档文案 ──


class TestTentativeLinks:
    def _entries(self, entries, n=2):
        return entries[:n]

    def test_header_states_relation_not_answer(self, entries) -> None:
        ans = format_tentative_links(
            self._entries(entries), url_of=lambda e: "http://x", markdown=False
        )
        assert TENTATIVE_HEADER in ans.text
        # 不得声称"这就是答案"(9·15 事故形态)
        for banned in ("找到", "直接命中", "解答", "以下是你要的答案"):
            assert banned not in ans.text
        # 也不得加免责话术:安全由 Jev 置信闸门负责,文案只管表达
        for banned in ("仅供参考", "不一定是答案", "不保证", "请以实际为准"):
            assert banned not in ans.text

    def test_no_body_text_no_images(self, entries) -> None:
        e = entries[0]
        ans = format_tentative_links([e], url_of=lambda x: "http://x", markdown=False)
        body_head = e.body.strip()[:30]
        if body_head:
            assert body_head not in ans.text
        assert ans.image_paths == []

    def test_markdown_mode_links_title(self, entries) -> None:
        e = entries[0]
        ans = format_tentative_links([e], url_of=lambda x: "http://x", markdown=True)
        safe = normalize_title(e.raw_title).replace("[", "［").replace("]", "］")
        assert f"[{safe}](http://x)" in ans.text

    def test_no_duplicate_ordinal_numbering(self, entries) -> None:
        """回归:raw_title 自带 "1、",拼进列表会变成 "1. [1、【xxx】](url)"。"""
        numbered = [e for e in entries if e.raw_title[:1].isdigit()]
        assert numbered, "fixture 里应当有条目带序号前缀"
        ans = format_tentative_links(
            numbered[:3], url_of=lambda e: "http://x", markdown=True
        )
        for line in ans.text.splitlines():
            if line.startswith(("1. ", "2. ", "3. ")):
                # 列表序号后面不得紧跟条目自身的 "1、"/"2、" 前缀
                body = line.split("](")[0]
                assert not body[3:4].isdigit(), f"重复编号: {line}"

    def test_symptom_tags_preserved(self, entries) -> None:
        tagged = next(e for e in entries if e.symptom_tags)
        ans = format_tentative_links([tagged], url_of=lambda x: "http://x", markdown=False)
        assert tagged.symptom_tags[0] in ans.text

    def test_plain_mode_uses_arrow(self, entries) -> None:
        ans = format_tentative_links(
            self._entries(entries), url_of=lambda e: "http://x", markdown=False
        )
        assert "👉 http://x" in ans.text

    def test_plain_mode_has_no_nested_brackets(self, entries) -> None:
        """回归:标题多以症状标签【tag】开头,再套一层会渲染成 【【tag】…】。"""
        ans = format_tentative_links(
            self._entries(entries), url_of=lambda e: "http://x", markdown=False
        )
        assert "【【" not in ans.text

    def test_entry_id_is_first(self, entries) -> None:
        ans = format_tentative_links(
            self._entries(entries), url_of=lambda e: "http://x", markdown=False
        )
        assert ans.entry_id == entries[0].id


# ── router 契约 ──


class TestRouterTentativeContract:
    def _router(self, entries, **kw):
        from astrbot_plugin_feishu_qa.answer.router import AnswerRouter

        return AnswerRouter(Retriever(entries), enabled_groups=["*"], **kw)

    def test_high_stays_direct(self, entries) -> None:
        plan = self._router(entries).route("声卡设置没问题但是cakewalk就是没声音", group_id="g")
        assert plan.kind == "direct"

    def test_medium_reports_tentative_not_miss(self, entries) -> None:
        # "效果器怎么添加"实测 MEDIUM(3.02)且 top-1 为错答
        plan = self._router(entries).route("效果器怎么添加", group_id="g")
        assert plan.kind == "tentative"
        assert plan.candidates

    def test_low_stays_miss(self, entries) -> None:
        plan = self._router(entries).route("今天上海天气怎么样", group_id="g")
        assert plan.kind == "miss"

    def test_chitchat_never_reaches_tentative(self, entries) -> None:
        """安全回归:闲聊不得触发 MEDIUM(否则会往群里发无关链接)。"""
        router = self._router(entries)
        chitchat = [
            "你喜欢我吗", "群主是什么人呢", "在吗", "今天天气怎么样", "晚上好呀",
            "你叫什么名字", "你是机器人吗", "谢谢大佬", "辛苦了", "有人吗",
            "早上好", "晚安啦", "哈哈哈哈", "好的收到", "你多大了",
        ]
        for q in chitchat:
            assert router.route(q, group_id="g").kind != "tentative", q

    def test_candidates_always_populated_for_hits(self, entries) -> None:
        router = self._router(entries)
        for q in ("声卡设置没问题但是cakewalk就是没声音", "效果器怎么添加", "今天上海天气怎么样"):
            assert router.route(q, group_id="g", top_k=3).candidates

    def test_denied_has_no_candidates(self, entries) -> None:
        from astrbot_plugin_feishu_qa.answer.router import AnswerRouter

        router = AnswerRouter(Retriever(entries), enabled_groups=[])
        assert router.route("没声音", group_id="g").candidates == []

    def test_plan_kinds_are_finite_set(self, entries) -> None:
        router = self._router(entries)
        allowed = {"denied", "direct", "miss", "tentative"}
        for query in ("没声音", "天气怎么样", "", "效果器怎么添加"):
            assert router.route(query, group_id="g").kind in allowed
