"""QA 语料数据模型(spec §9-§11)。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field


@dataclass
class QaImage:
    """一张截图的稳定引用。"""

    image_id: str  # 内容 hash 或 file_token 派生
    file_token: str  # lark-cli media-download 用 token
    name: str = ""  # 原始文件名(可空)
    local_path: str = ""  # 相对 data 根的路径,下载后填充


@dataclass
class QaEntry:
    """一条 QA(spec §9)。

    id 稳定性(spec §10):优先使用飞书块 ID(source_locator),
    无块 ID 时退回 sha256(section_path + title) 截断。
    """

    id: str
    section_path: list[str]  # 如 ["一、Cakewalk Sonar相关问答汇总", "（三）报错闪退相关："]
    category: str  # 二级分类(section_path[-1],无则 "")
    symptom_tags: list[str]
    title: str  # 去掉【标签】后的标题正文
    raw_title: str  # 原样标题
    body: str
    images: list[QaImage] = field(default_factory=list)
    source_locator: str = ""  # 飞书块 ID 等
    source_revision: int = -1
    content_hash: str = ""

    def compute_content_hash(self) -> str:
        payload = json.dumps(
            {
                "section_path": self.section_path,
                "title": self.title,
                "body": self.body,
                "images": [img.image_id for img in self.images],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)


_SEQ_PREFIX_RE = re.compile(r"^\s*\d+(\.\d+)?[、.．]\s*")
_CN_ORDINAL_RE = re.compile(r"^\s*[一二三四五六七八九十]+[、.．]\s*")


def normalize_title(raw_title: str) -> str:
    """去掉条目序号前缀(如"1、"/"2.1、"),保留症状标签与正文。

    序号会随 UP 主增删条目漂移,不属于身份。
    """
    return _SEQ_PREFIX_RE.sub("", raw_title.strip())


def normalize_section(section: str) -> str:
    """去掉章节中文/数字序号前缀(如"一、"),保留主题词。"""
    return _CN_ORDINAL_RE.sub("", _SEQ_PREFIX_RE.sub("", section.strip()))


def derive_entry_id(section_path: list[str], title: str, source_locator: str) -> str:
    """稳定派生 ID(spec §10)。

    实测修正(r8268→r8394):飞书块 ID 在任何结构编辑后全部重新生成,
    跨 revision **不可**作为身份。因此一律用内容派生:
    sha256(规范化章节 + 去序号标题)[:16];source_locator 仅作元数据保留。
    """
    section = normalize_section(section_path[0]) if section_path else ""
    basis = hashlib.sha256(
        (section + "|" + normalize_title(title)).encode("utf-8")
    ).hexdigest()[:16]
    return f"qa_{basis}"


def extract_symptom_tags(raw_title: str) -> tuple[list[str], str]:
    """从标题提取【症状标签】,返回 (tags, 去标签后的标题)。"""
    tags = re.findall(r"【([^】]+)】", raw_title)
    stripped = re.sub(r"【[^】]*】", "", raw_title).strip()
    return tags, stripped
