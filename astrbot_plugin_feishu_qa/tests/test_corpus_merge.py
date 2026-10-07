"""多来源语料合并测试(spec §15 扩展)。

核心回归:2026-10-07 线上事故——飞书同步与网页同步各自整篇覆盖 ``corpus.json``,
两个来源每 12 小时轮流把对方抹掉(实测 47 条与 76 条交替消失)。分片化之后,
任何单一来源的同步都不得影响另一个来源的条目。
"""

from __future__ import annotations

from astrbot_plugin_feishu_qa.corpus.builder import (
    build_manifest,
    manifest_content_hash,
    merge_manifests,
)
from astrbot_plugin_feishu_qa.corpus.model import QaEntry
from astrbot_plugin_feishu_qa.corpus.parser import ParseResult
from astrbot_plugin_feishu_qa.storage.snapshot import SnapshotStore

FEISHU = "feishu"
FUUUMUSIC = "fuuumusic"


def _entry(entry_id: str, title: str, *, source: str, locator: str = "") -> QaEntry:
    return QaEntry(
        id=entry_id,
        section_path=["章节"],
        category="章节",
        symptom_tags=[],
        title=title,
        raw_title=title,
        body=f"{title}的正文",
        source_locator=locator,
        source=source,
    )


def _manifest(entries: list[QaEntry], *, source: str, revision_id: int = 1) -> dict:
    return build_manifest(
        ParseResult(entries=entries),
        revision_id=revision_id,
        document_id=f"{source}_doc",
        source=source,
    )


def _ids(manifest: dict) -> list[str]:
    return [e["id"] for e in manifest["entries"]]


def _merge(store: SnapshotStore) -> dict:
    return merge_manifests(
        [
            (FEISHU, store.load_slice(FEISHU)),
            (FUUUMUSIC, store.load_slice(FUUUMUSIC)),
        ]
    )


class TestMergeManifests:
    def test_union_and_dedupe_by_id_feishu_wins(self) -> None:
        feishu = _manifest(
            [
                _entry("qa_a", "共享条目", source=FEISHU),
                _entry("qa_b", "飞书独有", source=FEISHU),
            ],
            source=FEISHU,
        )
        fuuumusic = _manifest(
            [
                _entry("qa_a", "共享条目", source=FUUUMUSIC),
                _entry("qa_c", "网页独有", source=FUUUMUSIC),
            ],
            source=FUUUMUSIC,
            revision_id=2,
        )
        merged = merge_manifests([(FEISHU, feishu), (FUUUMUSIC, fuuumusic)])

        assert _ids(merged) == ["qa_a", "qa_b", "qa_c"]
        # 网页的 FAQ 段是飞书文档的镜像(同章节同标题 → 同 ID),飞书那份优先
        assert merged["entries"][0]["source"] == FEISHU
        assert merged["entry_count"] == 3
        assert merged["source"] == "merged"
        assert merged["revision_id"] == 2
        assert merged["sources"][FEISHU]["kept"] == 2
        assert merged["sources"][FUUUMUSIC]["kept"] == 1
        assert merged["sources"][FUUUMUSIC]["deduped"] == 1

    def test_content_hash_consistent_with_manifest_content_hash(self) -> None:
        merged = merge_manifests(
            [(FEISHU, _manifest([_entry("qa_a", "条目", source=FEISHU)], source=FEISHU))]
        )
        assert merged["content_hash"] == manifest_content_hash(merged)

    def test_missing_slices_tolerated(self) -> None:
        merged = merge_manifests([(FEISHU, None), (FUUUMUSIC, None)])
        assert merged["entries"] == []
        assert merged["entry_count"] == 0
        assert merged["revision_id"] == -1

    def test_image_count_sums_merged_entries(self) -> None:
        merged = merge_manifests(
            [
                (FEISHU, _manifest([_entry("qa_a", "条目", source=FEISHU)], source=FEISHU)),
                (
                    FUUUMUSIC,
                    _manifest([_entry("qa_b", "条目2", source=FUUUMUSIC)], source=FUUUMUSIC),
                ),
            ]
        )
        assert merged["image_count"] == 0
        assert merged["entry_count"] == 2


class TestSliceStorage:
    def test_roundtrip(self, tmp_path) -> None:
        store = SnapshotStore(tmp_path)
        manifest = _manifest([_entry("qa_a", "条目", source=FEISHU)], source=FEISHU)
        store.save_slice(FEISHU, manifest)
        assert store.load_slice(FEISHU) == manifest

    def test_missing_returns_none(self, tmp_path) -> None:
        assert SnapshotStore(tmp_path).load_slice("nope") is None

    def test_corrupt_returns_none(self, tmp_path) -> None:
        store = SnapshotStore(tmp_path)
        path = store.slice_path(FEISHU)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ 这不是 json")
        assert store.load_slice(FEISHU) is None


class TestSourcesDoNotClobber:
    """线上事故回归:单一来源同步后,另一个来源的条目必须仍在。"""

    def _seed(self, tmp_path) -> SnapshotStore:
        store = SnapshotStore(tmp_path)
        store.save_slice(
            FEISHU,
            _manifest([_entry("qa_feishu", "飞书条目", source=FEISHU)], source=FEISHU),
        )
        store.save_slice(
            FUUUMUSIC,
            _manifest(
                [_entry("qa_fuuu", "网页条目", source=FUUUMUSIC)],
                source=FUUUMUSIC,
                revision_id=2,
            ),
        )
        store.commit(_merge(store))
        return store

    def test_initial_merge_contains_both(self, tmp_path) -> None:
        store = self._seed(tmp_path)
        assert sorted(_ids(store.load())) == ["qa_feishu", "qa_fuuu"]

    def test_feishu_resync_keeps_fuuumusic(self, tmp_path) -> None:
        store = self._seed(tmp_path)
        # 飞书同步:只写自己的分片,然后重算合并
        store.save_slice(
            FEISHU,
            _manifest(
                [_entry("qa_feishu", "飞书条目v2", source=FEISHU)],
                source=FEISHU,
                revision_id=3,
            ),
        )
        store.commit(_merge(store))
        merged = store.load()
        assert sorted(_ids(merged)) == ["qa_feishu", "qa_fuuu"]
        assert merged["sources"][FUUUMUSIC]["kept"] == 1

    def test_fuuumusic_resync_keeps_feishu(self, tmp_path) -> None:
        store = self._seed(tmp_path)
        store.save_slice(
            FUUUMUSIC,
            _manifest(
                [_entry("qa_fuuu", "网页条目v2", source=FUUUMUSIC)],
                source=FUUUMUSIC,
                revision_id=9,
            ),
        )
        store.commit(_merge(store))
        merged = store.load()
        assert sorted(_ids(merged)) == ["qa_feishu", "qa_fuuu"]
        assert merged["sources"][FEISHU]["kept"] == 1

    def test_snapshot_never_written_by_a_single_source(self, tmp_path) -> None:
        """分片目录存在,且合并视图与分片各司其职(单一来源不碰 corpus.json)。"""
        store = self._seed(tmp_path)
        assert store.slice_path(FEISHU).is_file()
        assert store.slice_path(FUUUMUSIC).is_file()
        assert store.load().get("source") == "merged"
