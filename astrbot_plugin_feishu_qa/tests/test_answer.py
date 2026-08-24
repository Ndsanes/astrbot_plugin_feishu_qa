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


class TestMetricsContract:
    def test_plan_kinds_are_finite_set(self, env) -> None:
        router = AnswerRouter(env["retriever"], store=env["store"], enabled_groups=["*"])
        allowed = {"denied", "direct", "miss"}
        for query in ("没声音", "天气怎么样", ""):
            plan = router.route(query, group_id="g")
            assert plan.kind in allowed
