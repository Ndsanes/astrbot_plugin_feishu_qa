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

    def exists(self) -> bool:
        return self.snapshot_path.is_file()

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
