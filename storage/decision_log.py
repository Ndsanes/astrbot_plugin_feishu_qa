"""路由决策日志(可观测性)。

目的:把"为什么答/为什么不答"变成可回读的数据,而不是日志里的一行
INFO。`HIGH_CONFIDENCE_THRESHOLD=9.0` 这类切点此前只有 20 条手挑样本
校准过,真实分布必须靠线上观测回填。

**数据最小化(engineering-discipline / 用户数据落盘专项)**:群里问问题
的是真人,原话属于用户数据。本模块的约束:
  1. `text_mode="truncate"`(默认):原文按 `max_text_chars` 截断后落盘,
     足以复现判定又不留全文档案;
  2. `text_mode="hash"`(推荐用于长期留存):只留 sha256 前 16 位,配
     同一问题跨次聚合的能力,不留任何原文;
  3. `text_mode="full"`:仅在明确需要逐句复盘时临时开启,配合轮转限制体积。
无论哪种模式,`query_hash` 恒定写入,这是聚合分析的连接键。

**永不阻塞业务**:任何写盘异常一律吞掉并降级为静默,决策日志挂了不影响
机器人回答问题——它只是观测手段,不是链路依赖。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("feishu_qa.storage")

LOG_NAME = "decisions.jsonl"
# 轮转阈值与保留份数:默认 2MB × 3 份,群聊量级下够放半年以上。
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_KEEP_FILES = 3

_WS_RE = re.compile(r"\s+")


def normalize_query(text: str) -> str:
    """规范化问题:去首尾空白、折叠空白、lower。

    仅用于同一性比较与聚合键,不参与检索打分(检索仍吃用户原话)。
    """
    return _WS_RE.sub(" ", (text or "").strip()).lower()


def query_hash(text: str) -> str:
    """规范化问题的稳定短哈希,跨轮次可连接。"""
    return hashlib.sha256(normalize_query(text).encode("utf-8")).hexdigest()[:16]


@dataclass
class CandidateRecord:
    """单条候选的打分明细(给阈值调参用,不给用户看)。"""

    entry_id: str
    title: str
    score: float
    confidence: str
    supported: bool

    def to_dict(self) -> dict:
        return {
            "id": self.entry_id,
            "title": self.title,
            "score": round(self.score, 3),
            "conf": self.confidence,
            "supported": self.supported,
        }


@dataclass
class DecisionRecord:
    """一次路由决策的完整快照。

    action 取值契约(与 main.py 的实际动作一一对应):
      denied              白名单外,零响应
      direct              高置信直答
      miss                证据不足,放行主 Agent
      tentative_suppressed 落在 MEDIUM 区、模糊档已开启 → 已按承诺抑制
      tentative_sent      落在 MEDIUM 区、模糊档已开启 → 已投递模糊链接
      tentative_disabled  落在 MEDIUM 区、模糊档关闭 → 放行主 Agent
      links_sent          附链判定通过并已投递
      links_suppressed    附链判定不达标,已抑制
    """

    stage: str  # route | linkable
    action: str
    query: str
    candidates: list[CandidateRecord] = field(default_factory=list)
    thresholds: dict[str, float] = field(default_factory=dict)
    revision: int | str | None = None
    cached: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self, *, text_mode: str, max_text_chars: int, ts: float) -> dict:
        q = self.query or ""
        if text_mode == "hash":
            shown = ""
        elif text_mode == "full":
            shown = q
        else:
            shown = q[:max_text_chars]
        return {
            "ts": round(ts, 3),
            "stage": self.stage,
            "action": self.action,
            # query_hash 恒定写入:truncate/hash 两种模式下都是唯一连接键。
            "qh": query_hash(q),
            "q": shown,
            "q_trunc": len(q) > len(shown),
            "cands": [c.to_dict() for c in self.candidates],
            "th": self.thresholds,
            "rev": self.revision,
            "cached": self.cached,
            **({"extra": self.extra} if self.extra else {}),
        }


class DecisionLog:
    """JSONL 追加写的决策日志,带体积轮转。

    写盘失败一律静默降级:观测手段不得成为业务链路依赖。
    """

    def __init__(
        self,
        data_root: Path,
        *,
        enabled: bool = True,
        text_mode: str = "truncate",
        max_text_chars: int = 200,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep_files: int = DEFAULT_KEEP_FILES,
    ) -> None:
        self.path = Path(data_root) / LOG_NAME
        self.enabled = bool(enabled)
        self.text_mode = text_mode if text_mode in ("truncate", "hash", "full") else "truncate"
        self.max_text_chars = max(0, int(max_text_chars))
        self.max_bytes = max(1024, int(max_bytes))
        self.keep_files = max(1, int(keep_files))

    def record(self, rec: DecisionRecord, *, ts: float | None = None) -> bool:
        """追加一条决策。失败只 debug 日志,绝不抛出。

        返回是否真的写进了磁盘——决策日志是"事后唯一的真相来源",写盘失败
        此前完全静默,一旦目录权限或磁盘出问题,线上表现只是"日志里啥都没有",
        无法与"根本没记录"区分。
        """
        if not self.enabled:
            return False
        try:
            payload = rec.to_dict(
                text_mode=self.text_mode,
                max_text_chars=self.max_text_chars,
                ts=time.time() if ts is None else ts,
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._rotate_if_needed()
            return True
        except Exception as exc:  # 观测不得影响业务
            logger.warning("[decision_log] 写入失败(已忽略): %s", exc)
            return False

    def _rotated_path(self, index: int) -> Path:
        """第 index 份轮转文件;index=0 即当前主文件。"""
        return self.path if index == 0 else self.path.with_name(f"{self.path.name}.{index}")

    def _rotate_if_needed(self) -> None:
        try:
            if not self.path.is_file() or self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        # 整体后移一份:最旧的直接丢弃。注意 Path.replace 是"把自己移到目标",
        # 方向写反会静默失败(曾经踩过:每次都去移动一个不存在的文件,日志无限
        # 增长而轮转从未发生)。
        # keep_files 含主文件本身,即历史最多 keep_files-1 份。
        try:
            oldest = self._rotated_path(self.keep_files - 1)
            if oldest.is_file():
                oldest.unlink()
            for i in range(self.keep_files - 2, 0, -1):
                src = self._rotated_path(i)
                if src.is_file():
                    src.replace(self._rotated_path(i + 1))
            self.path.replace(self._rotated_path(1))
            self.path.touch()
        except OSError as exc:
            logger.debug("[decision_log] 轮转失败(已忽略): %s", exc)

    def read_all(self, *, limit: int | None = None) -> list[dict]:
        """回读全部(跨轮转文件,旧→新)。仅供分析与测试。"""
        records: list[dict] = []
        files: list[Path] = []
        for i in range(self.keep_files, 0, -1):
            p = self.path if i == 1 else self.path.with_suffix(self.path.suffix + f".{i - 1}")
            if p.is_file():
                files.append(p)
        for p in files:
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
            except (OSError, json.JSONDecodeError):
                continue
        if limit is not None and len(records) > limit:
            return records[-limit:]
        return records


class DecisionCache:
    """问题级决策记忆,消除"同一问题两次给出不同结果"。

    确定性打分器本身对同一输入是稳定的,本缓存真正解决的是:
      1. 语料在两次提问之间同步过 → 同问题分数变了 → 答案翻面;
      2. 将来接入带抖动的模型(如 Jev)后,同问题多次判定不一致。
    TTL 到期或语料 revision 变化即失效,不做长期记忆。
    """

    def __init__(self, *, ttl_seconds: float = 240.0, max_items: int = 256) -> None:
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_items = max(1, int(max_items))
        self._store: dict[str, tuple[float, int | str | None, str, tuple[str, ...]]] = {}

    def get(
        self, query: str, *, revision: int | str | None, now: float | None = None
    ) -> dict | None:
        """取回仍然有效的决策;语料 revision 变化或过期则视为未命中。"""
        if self.ttl_seconds <= 0:
            return None
        now = time.time() if now is None else now
        item = self._store.get(query_hash(query))
        if item is None:
            return None
        ts, cached_rev, action, entry_ids = item
        if now - ts > self.ttl_seconds:
            return None
        if cached_rev != revision:
            return None
        return {"action": action, "entry_ids": entry_ids}

    def put(
        self,
        query: str,
        *,
        action: str,
        entry_ids: Iterable[str] = (),
        revision: int | str | None = None,
        now: float | None = None,
    ) -> None:
        if self.ttl_seconds <= 0:
            return
        now = time.time() if now is None else now
        if len(self._store) >= self.max_items:
            # 简单 FIFO:按写入时间淘汰最旧的一批。
            for key in sorted(self._store, key=lambda k: self._store[k][0])[
                : max(1, len(self._store) // 4)
            ]:
                self._store.pop(key, None)
        self._store[query_hash(query)] = (
            now,
            revision,
            action,
            tuple(entry_ids),
        )

    def clear(self) -> None:
        self._store.clear()
