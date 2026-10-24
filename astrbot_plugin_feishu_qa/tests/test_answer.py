"""直答与路由测试(spec §22/§29/§74)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_plugin_feishu_qa.answer.direct import (
    SOURCE_ATTRIBUTION,
    format_direct_answer,
)
from astrbot_plugin_feishu_qa.answer.router import AnswerRouter
from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
from astrbot_plugin_feishu_qa.retrieval.scorer import Retriever
from astrbot_plugin_feishu_qa.storage.snapshot import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    xml = (FIXTURES / "qa_r8268.xml").read_text()
    parsed = parse_xml(xml, source_revision=8268)
    from astrbot_plugin_feishu_qa.corpus.builder import build_manifest

    manifest = build_manifest(parsed, revision_id=8268, document_id="doc")
    data_root = tmp_path_factory.mktemp("qa_data")
    store = SnapshotStore(data_root)
    store.commit(manifest)
    retriever = Retriever(parsed.entries)
    return {"parsed": parsed, "store": store, "retriever": retriever, "manifest": manifest}


class TestWhitelist:
    def test_empty_whitelist_denies_everything(self, env) -> None:
        router = AnswerRouter(env["retriever"], store=env["store"], enabled_groups=[])
        plan = router.route("cakewalk 没声音", group_id="123")
        assert plan.kind == "denied"

    def test_star_enables_all(self, env) -> None:
        router = AnswerRouter(env["retriever"], store=env["store"], enabled_groups=["*"])
        assert router.group_enabled("任意群") is True
        plan = router.route("cakewalk 没声音", group_id="任意群")
        assert plan.kind in ("direct", "miss")

    def test_unlisted_group_denied(self, env) -> None:
        router = AnswerRouter(
            env["retriever"], store=env["store"], enabled_groups=["111"]
        )
        assert router.route("没声音", group_id="222").kind == "denied"
        assert router.route("没声音", group_id=None).kind == "denied"
        assert router.route("没声音", group_id="111").kind != "denied"

    def test_umo_entry_matches_exact_umo(self, env) -> None:
        umo = "inst_a:GroupMessage:grp_1"
        router = AnswerRouter(
            env["retriever"], store=env["store"], enabled_groups=[umo]
        )
        assert router.group_enabled("grp_1", umo=umo) is True
        assert router.route("没声音", group_id="grp_1", umo=umo).kind != "denied"

    def test_umo_entry_rejects_same_group_other_instance(self, env) -> None:
        router = AnswerRouter(
            env["retriever"],
            store=env["store"],
            enabled_groups=["inst_a:GroupMessage:grp_1"],
        )
        # 同群 openid 挂在另一实例下:UMO 精确匹配不通过
        other = "inst_b:GroupMessage:grp_1"
        assert router.group_enabled("grp_1", umo=other) is False
        assert router.route("没声音", group_id="grp_1", umo=other).kind == "denied"

    def test_bare_entries_ignore_umo_dimension(self, env) -> None:
        router = AnswerRouter(
            env["retriever"], store=env["store"], enabled_groups=["grp_1"]
        )
        # 裸 ID 条目继续按 get_group_id 匹配,与 UMO 无关(向后兼容)
        assert (
            router.group_enabled("grp_1", umo="inst_x:GroupMessage:grp_1") is True
        )


class TestDirectAnswer:
    def test_high_confidence_direct_with_attribution(self, env) -> None:
        router = AnswerRouter(env["retriever"], store=env["store"], enabled_groups=["*"])
        plan = router.route("声卡设置没问题但是cakewalk就是没声音", group_id="1")
        assert plan.kind == "direct"
        assert plan.direct is not None
        assert "找到一个相关问题" in plan.direct.text
        assert SOURCE_ATTRIBUTION in plan.direct.text
        assert plan.direct.entry_id.startswith("qa_")

    def test_images_attached_when_files_exist(self, env, tmp_path: Path) -> None:
        # 找一个多图条目,把其图片文件造出来
        entry = next(e for e in env["parsed"].entries if len(e.images) >= 2)
        for img in entry.images[:2]:
            p = env["store"].image_path(f"images/{img.image_id}.png")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"fakepng")
        answer = format_direct_answer(entry, store=env["store"], max_images=3)
        assert len(answer.image_paths) == 2
        for path in answer.image_paths:
            assert Path(path).is_file()

    def test_missing_image_files_do_not_break_answer(self, env) -> None:
        entry = next(e for e in env["parsed"].entries if e.images)
        answer = format_direct_answer(entry, store=env["store"])
        assert answer.text  # 文字仍在,图片缺失不抛错

    def test_max_images_capped(self, env) -> None:
        entry = max(env["parsed"].entries, key=lambda e: len(e.images))
        for img in entry.images:
            p = env["store"].image_path(f"images/{img.image_id}.png")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        answer = format_direct_answer(entry, store=env["store"], max_images=2)
        assert len(answer.image_paths) <= 2


class TestMiss:
    def test_irrelevant_query_miss_reply(self, env) -> None:
        router = AnswerRouter(env["retriever"], store=env["store"], enabled_groups=["*"])
        plan = router.route("今天上海天气怎么样", group_id="1")
        assert plan.kind == "miss"


class TestProactiveHintFormat:
    """主动答版式:两行,越短越好(2026-10-24 收敛)。"""

    def _entry(self, env):
        return env["parsed"].entries[0]

    def test_markdown_two_lines(self, env) -> None:
        from astrbot_plugin_feishu_qa.answer.direct import (
            PROACTIVE_HINT_HEADER,
            format_proactive_hint,
        )
        from astrbot_plugin_feishu_qa.corpus.model import normalize_title

        entry = self._entry(env)
        answer = format_proactive_hint(
            entry, url_of=lambda e: "https://x/y#q1", markdown=True
        )
        title = normalize_title(entry.title)
        assert answer.text == f"{PROACTIVE_HINT_HEADER}\n- [{title}](https://x/y#q1)"
        assert "【" not in answer.text, "症状标签与标题重复,主动答里不再重复一遍"
        assert answer.image_paths == []
        assert answer.entry_id == entry.id

    def test_plain_fallback_for_other_platforms(self, env) -> None:
        from astrbot_plugin_feishu_qa.answer.direct import format_proactive_hint

        entry = self._entry(env)
        text = format_proactive_hint(
            entry, url_of=lambda e: "https://x/y#q1", markdown=False
        ).text
        assert text.count("\n") == 1, "只该有两行"
        assert text.splitlines()[1].startswith("- ")
        assert "👉 https://x/y#q1" in text
        assert "来源" not in text

    def test_ordinal_stripped(self, env) -> None:
        from astrbot_plugin_feishu_qa.answer.direct import format_proactive_hint

        entry = self._entry(env)
        text = format_proactive_hint(
            entry, url_of=lambda e: "u", markdown=True, lead="X"
        ).text
        assert "1、" not in text and "2.1、" not in text


class TestIntentGate:
    """线上误答的回归:分数够高,但问的是语料答不了的那类问题。"""

    @staticmethod
    def _retriever() -> Retriever:
        """复刻线上那条把「键盘」当证据的条目(症状标签含双字泛名词)。"""
        from astrbot_plugin_feishu_qa.corpus.model import QaEntry

        entry = QaEntry(
            id="qa_midi_keyboard",
            section_path=["如何连接设备"],
            category="如何连接设备",
            symptom_tags=["MIDI键盘", "电子琴", "键盘", "键盘连不上"],
            title="连接 USB MIDI 键盘",
            raw_title="连接 USB MIDI 键盘",
            body="把键盘用 USB 线连到电脑,在偏好设置里选择 MIDI 输入设备。",
            source="fuuumusic",
        )
        # 阈值压低,让这条在"没有闸门时"确实会被直答——否则测不出闸门的作用
        return Retriever([entry], high_threshold=2.0, medium_threshold=1.0)

    def test_high_score_purchase_question_is_refused(self) -> None:
        router = AnswerRouter(self._retriever(), enabled_groups=["*"])
        plan = router.route("脑袋，键盘和鼠标推荐哪些？", group_id="1")
        # 不承接:既不是 direct,也不是 miss(后者会放行主 Agent 代答)
        assert plan.kind == "refused"
        assert plan.note == "intent:purchase_advice"
        assert plan.candidates, "cands 要保留，日志才能看出「差点答出去哪一条」"
        assert plan.candidates[0].confidence == "HIGH", "没有闸门时这条会被直答"

    def test_same_words_as_troubleshooting_still_answers(self) -> None:
        router = AnswerRouter(self._retriever(), enabled_groups=["*"])
        plan = router.route("键盘连不上电脑", group_id="1")
        assert plan.kind == "direct"
        assert plan.note == ""


class TestMetricsContract:
    def test_plan_kinds_are_finite_set(self, env) -> None:
        router = AnswerRouter(env["retriever"], store=env["store"], enabled_groups=["*"])
        allowed = {"denied", "refused", "direct", "tentative", "miss"}
        for query in ("没声音", "天气怎么样", "", "键盘鼠标推荐哪个牌子"):
            plan = router.route(query, group_id="g")
            assert plan.kind in allowed
