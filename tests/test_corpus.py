"""语料解析与构建测试(真实 r8268 fixture,spec §53/§56/§66)。"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

from astrbot_plugin_feishu_qa.corpus.builder import (
    build_manifest,
    diff_manifests,
    manifest_content_hash,
)
from astrbot_plugin_feishu_qa.corpus.model import (
    derive_entry_id,
    extract_symptom_tags,
    normalize_title,
)
from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
from astrbot_plugin_feishu_qa.storage.snapshot import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def real_xml() -> str:
    return (FIXTURES / "qa_r8268.xml").read_text()


@pytest.fixture(scope="module")
def parsed(real_xml: str):
    return parse_xml(real_xml, source_revision=8268)


class TestParserRealFixture:
    def test_entry_count_matches_baseline(self, parsed) -> None:
        # 基线约 40+ 条;不写死精确值(spec §0),但不得异常丢失
        assert 35 <= len(parsed.entries) <= 50

    def test_image_count_matches_baseline(self, parsed) -> None:
        assert 90 <= parsed.image_count <= 130

    def test_symptom_tags_extracted(self, parsed) -> None:
        tagged = [e for e in parsed.entries if e.symptom_tags]
        assert len(tagged) >= 20
        sample = next(e for e in tagged if "没声音" in "".join(e.symptom_tags))
        assert "【" not in sample.title  # 标题正文不含标签壳
        assert sample.raw_title.startswith("1、") or "【" in sample.raw_title

    def test_locator_kept_as_metadata_id_content_derived(self, parsed) -> None:
        for entry in parsed.entries:
            assert entry.source_locator, "真实文档每个标题都有块 ID"
            # 实测块 ID 跨 revision 漂移,ID 必须内容派生而非块 ID
            assert not entry.id.endswith(entry.source_locator)
            assert entry.id.startswith("qa_")

    def test_ids_stable_within_revision(self, real_xml: str) -> None:
        again = parse_xml(real_xml, source_revision=8268)
        old = parse_xml(real_xml, source_revision=8268)
        ids1 = {normalize_title(e.raw_title): e.id for e in old.entries}
        for e in again.entries:
            assert ids1[normalize_title(e.raw_title)] == e.id

    def test_multi_image_binding(self, parsed) -> None:
        multi = [e for e in parsed.entries if len(e.images) >= 2]
        assert multi, "真实文档存在多图条目"
        all_ids = [img.image_id for e in parsed.entries for img in e.images]
        assert len(all_ids) == len(set(all_ids)), "图片不得交叉绑定"

    def test_grid_nested_images_captured(self, parsed) -> None:
        # 真实 fixture 中有 grid 内嵌图片(31 张),总数须覆盖
        total_tokens = {img.file_token for e in parsed.entries for img in e.images}
        assert len(total_tokens) >= 100

    def test_body_preserves_paths_and_tags(self, parsed) -> None:
        joined = "\n".join(e.body for e in parsed.entries)
        assert "C:\\Program Files\\Cakewalk\\VstPlugins" in joined
        assert "快捷键P" in joined or "快捷键 P" in joined

    def test_body_strips_internal_urls(self, parsed) -> None:
        for e in parsed.entries:
            assert "internal-api-drive-stream" not in e.body
            assert "feishucdn" not in e.body


class TestParserEdgeCases:
    def _wrap(self, inner: str) -> str:
        return inner

    def test_missing_h1(self) -> None:
        result = parse_xml("<h2 id='c'>分类</h2><h3 id='q'>【标签】问题</h3><p id='p1'>答</p>")
        assert len(result.entries) == 1
        assert result.entries[0].section_path == ["分类"]

    def test_missing_h2_direct_h3_under_h1(self) -> None:
        result = parse_xml(
            "<h1 id='s'>章节</h1><h3 id='q'>【闪退】打不开</h3><p id='p1'>重装</p>"
        )
        entry = result.entries[0]
        assert entry.category == ""
        assert entry.section_path == ["章节"]
        assert entry.body == "重装"

    def test_empty_body_no_crash_with_diagnostic(self) -> None:
        result = parse_xml("<h3 id='q'>空条目</h3>")
        assert len(result.entries) == 1
        assert result.entries[0].body == ""
        assert result.diagnostics

    def test_repeated_titles_in_different_sections_distinct(self) -> None:
        _, title = extract_symptom_tags("重复")
        a = derive_entry_id(["一、宿主"], title, "")
        b = derive_entry_id(["二、音源"], title, "")
        assert a != b
        # 同章节重复标题视为同一身份(内容相同即重复)
        assert a == derive_entry_id(["一、宿主"], title, "")

    def test_invalid_xml_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            parse_xml("<h1><未闭合>")

    def test_lists_rendered_as_lines(self) -> None:
        xml = (
            "<h3 id='q'>列表</h3>"
            "<ol><li id='l1'>步骤一</li><li id='l2'>步骤二</li></ol>"
        )
        entry = parse_xml(xml).entries[0]
        assert "• 步骤一" in entry.body and "• 步骤二" in entry.body


class TestManifestAndIdempotency:
    def test_build_twice_same_content_hash(self, parsed) -> None:
        m1 = build_manifest(parsed, revision_id=8268, document_id="docX")
        m2 = build_manifest(parsed, revision_id=8268, document_id="docX")
        assert manifest_content_hash(m1) == manifest_content_hash(m2)

    def test_manifest_counts(self, parsed) -> None:
        manifest = build_manifest(parsed, revision_id=8268, document_id="docX")
        assert manifest["entry_count"] == len(parsed.entries)
        assert manifest["image_count"] == parsed.image_count

    def test_diff_detects_change(self, parsed) -> None:
        m1 = build_manifest(parsed, revision_id=8268, document_id="d")
        changed = json.loads(json.dumps(m1))
        changed["entries"][0]["body"] += "(补充)"
        diff = diff_manifests(m1, changed)
        assert diff["changed"] and diff["updated"] == 1 and diff["added"] == 0


class TestSnapshotStore:
    def test_commit_and_load_roundtrip(self, tmp_path: Path, parsed) -> None:
        store = SnapshotStore(tmp_path)
        assert store.load() is None
        manifest = build_manifest(parsed, revision_id=8268, document_id="d")
        store.commit(manifest)
        loaded = store.load()
        assert loaded is not None
        assert loaded["revision_id"] == 8268
        assert manifest_content_hash(loaded) == manifest_content_hash(manifest)

    def test_corrupt_snapshot_returns_none_not_raise(self, tmp_path: Path) -> None:
        store = SnapshotStore(tmp_path)
        tmp_path.mkdir(exist_ok=True)
        store.snapshot_path.write_text("{broken json")
        assert store.load() is None

    def test_failed_write_keeps_old_snapshot(self, tmp_path: Path, parsed) -> None:
        store = SnapshotStore(tmp_path)
        old = build_manifest(parsed, revision_id=100, document_id="d")
        store.commit(old)
        # 模拟坏 manifest(不可序列化)→ commit 抛异常 → 旧快照完好
        bad_manifest = {"entries": [{"bad": object()}]}
        with contextlib.suppress(TypeError):
            store.commit(bad_manifest)
        assert store.load()["revision_id"] == 100


class TestCrossRevisionStability:
    """r8268 vs r8394 真实双基线:块 ID 漂移下内容派生 ID 必须稳定。"""

    def test_same_title_entries_share_id_across_revisions(self) -> None:
        old = parse_xml(
            (FIXTURES / "qa_r8268.xml").read_text(), source_revision=8268
        )
        new = parse_xml(
            (FIXTURES / "qa_r8394.xml").read_text(), source_revision=8394
        )
        old_by_norm = {normalize_title(e.raw_title): e.id for e in old.entries}
        stable = sum(
            1
            for e in new.entries
            if old_by_norm.get(normalize_title(e.raw_title)) == e.id
        )
        assert stable >= 40, f"跨 revision ID 稳定数不足: {stable}"

    def test_real_incremental_diff_detects_new_entry(self) -> None:
        old_m = build_manifest(
            parse_xml((FIXTURES / "qa_r8268.xml").read_text(), source_revision=8268),
            revision_id=8268,
            document_id="d",
        )
        new_m = build_manifest(
            parse_xml((FIXTURES / "qa_r8394.xml").read_text(), source_revision=8394),
            revision_id=8394,
            document_id="d",
        )
        diff = diff_manifests(old_m, new_m)
        assert diff["added"] == 1 and diff["removed"] == 0 and diff["changed"]
