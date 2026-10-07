"""语料快照的原子持久化(spec §48-§49)。

reader 永远读 immutable snapshot;writer 写 staging 目录,
构建+校验成功后原子替换。同步失败绝不破坏旧数据。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger("feishu_qa.storage")

SNAPSHOT_NAME = "corpus.json"
STAGING_DIRNAME = ".staging"
# 各来源的分片目录。corpus.json 是这些分片的合并视图(2026-10-07 多来源改造),
# 单一来源不得直接改写它——否则两个来源会互相覆盖。
SOURCES_DIRNAME = "corpus_sources"


class SnapshotStore:
    """管理 data 根下的语料快照与图片目录。"""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.snapshot_path = self.data_root / SNAPSHOT_NAME
        self.images_dir = self.data_root / "images"
        self.staging_dir = self.data_root / STAGING_DIRNAME

    # ── 读 ──

    def load(self) -> dict | None:
        """读取当前快照;不存在或损坏返回 None(调用方保留内存旧值)。"""
        try:
            return json.loads(self.snapshot_path.read_text())
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("[snapshot] 读取失败(视为无快照): %s", exc)
            return None

    # ── 写(staging → atomic swap)──

    def commit(
        self,
        manifest: dict,
        *,
        staged_images: dict[Path, str] | None = None,
    ) -> None:
        """提交新快照。

        staged_images: {staging 内文件路径 → 相对 images/ 的最终文件名}。
        流程:写 staging/corpus.json → 换入图片 → 原子替换 corpus.json。
        """
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        staging_snapshot = self.staging_dir / SNAPSHOT_NAME
        tmp_json = self.staging_dir / "corpus.json.tmp"
        tmp_json.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))

        if staged_images:
            self.images_dir.mkdir(parents=True, exist_ok=True)
            for src, final_name in staged_images.items():
                target = self.images_dir / final_name
                if not target.exists() and src.is_file():
                    shutil.copy2(src, target)

        os.replace(tmp_json, staging_snapshot)
        os.replace(staging_snapshot, self.snapshot_path)
        logger.info("[snapshot] 已提交 revision=%s", manifest.get("revision_id"))

    def image_path(self, local_path: str) -> Path:
        """把语料中的相对路径解析为绝对路径。"""
        return self.data_root / local_path

    # ── 分片(多来源) ──

    def slice_path(self, source: str) -> Path:
        """某个来源的分片文件路径。"""
        return self.data_root / SOURCES_DIRNAME / f"{source}.json"

    def load_slice(self, source: str) -> dict | None:
        """读某个来源的分片;不存在或损坏返回 None(视为该来源暂无数据)。"""
        try:
            return json.loads(self.slice_path(source).read_text())
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("[snapshot] 分片 %s 读取失败(视为无): %s", source, exc)
            return None

    def save_slice(self, source: str, manifest: dict) -> None:
        """原子写某个来源的分片。

        分片必须让**每个来源各自落盘**:合并后的 ``corpus.json`` 是由全部分片
        重算出来的派生视图,任何单一来源都无权直接覆盖它。
        """
        path = self.slice_path(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
        os.replace(tmp, path)
        logger.info(
            "[snapshot] 已保存分片 source=%s entries=%s",
            source,
            manifest.get("entry_count"),
        )

