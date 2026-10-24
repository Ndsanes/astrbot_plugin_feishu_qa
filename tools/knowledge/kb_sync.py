#!/usr/bin/env python3
"""检查官方 PDF 是否更新,并在更新时重建 + 同步到 AstrBot 知识库。

背景:知识库「Cakewalk sonar」里的 3166 块来自
``bandlab.github.io/cakewalk/docs/Cakewalk Sonar Reference Guide.pdf``。
官方手册更新后需要重跑 ``build_knowledge.py`` 再整库替换,手动做容易忘、
也容易漏掉"文件被重新发布但内容没变"这种情况。本脚本把三步串起来:

  check  → HEAD 取 ETag/大小;不一致才下载并比 sha256(内容没变就只更新校验缓存)
  build  → 有变化时重跑流水线(run_pipeline),打印页数/块数/版本差异
  upload → 预分块导入 AstrBot(``documents/import``),可选删除旧文档

用法:
  python kb_sync.py --check                      # 只检查(退出码 0=最新, 2=有更新)
  python kb_sync.py --all --kb-name "Cakewalk sonar" --replace
  python kb_sync.py --upload --kb-id <uuid> --dry-run   # 只生成 payload
  python kb_sync.py --list-kbs
  python kb_sync.py --list-docs --kb-name "Cakewalk sonar"

自动化(cron,每天 05:00):
  0 5 * * * cd <repo> && .venv/bin/python tools/knowledge/kb_sync.py \
      --all --kb-name "Cakewalk sonar" --replace >> output/cakewalk/sync.log 2>&1

注意:API Key 30 天过期;KB 写入需要 JWT(客户端发现 403 scope 不足会自动
用 .env 里的管理员账号登录)。检查/构建部分不需要任何凭据。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT))

OFFICIAL_PDF_URL = (
    "https://bandlab.github.io/cakewalk/docs/Cakewalk%20Sonar%20Reference%20Guide.pdf"
)
DEFAULT_OUTPUT = REPO_ROOT / "output" / "cakewalk"
PDF_FILENAME = "Cakewalk Sonar Reference Guide.pdf"
SOURCE_DIRNAME = "source"
REMOTE_STATE = "remote.json"
DEFAULT_KB_NAME = "Cakewalk sonar"
DEFAULT_DOC_PREFIX = "Cakewalk Sonar Reference Guide"
_USER_AGENT = "Mozilla/5.0 (compatible; astrbot-kb-sync)"
CHUNK_CHARS_LIMIT = 4000  # 预分块不应被二次切片,这里只做异常提示


# ── 远端与本地文件 ──


@dataclass
class RemoteInfo:
    url: str
    bytes: int = 0
    etag: str = ""
    last_modified: str = ""


def remote_info(url: str = OFFICIAL_PDF_URL, *, timeout: float = 30.0) -> RemoteInfo:
    """HEAD 取远端大小与校验头;服务器不支持 HEAD 时抛错(调用方按 unknown 处理)。"""
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": _USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        headers = response.headers
        return RemoteInfo(
            url=url,
            bytes=int(headers.get("Content-Length") or 0),
            etag=(headers.get("ETag") or "").strip('"'),
            last_modified=headers.get("Last-Modified") or "",
        )


def file_sha256(path: Path, *, block: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(block):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path, *, timeout: float = 60.0) -> tuple[str, int]:
    """流式下载并返回 (sha256, 字节数)。先写 .part,成功后原子替换。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    total = 0
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with (
        urllib.request.urlopen(request, timeout=timeout) as response,  # noqa: S310
        tmp.open("wb") as handle,
    ):
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            handle.write(chunk)
            total += len(chunk)
    tmp.replace(dest)
    return digest.hexdigest(), total


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 状态文件 ──


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def manifest_path(output: Path) -> Path:
    return output / "manifest.json"


def pdf_path(output: Path) -> Path:
    return output / SOURCE_DIRNAME / PDF_FILENAME


def remote_state_path(output: Path) -> Path:
    return output / SOURCE_DIRNAME / REMOTE_STATE


def record_remote_state(output: Path, remote: RemoteInfo, sha256: str) -> None:
    save_json(
        remote_state_path(output),
        {
            "url": remote.url,
            "bytes": remote.bytes,
            "etag": remote.etag,
            "last_modified": remote.last_modified,
            "sha256": sha256,
            "checked_at": utc_now(),
        },
    )


# ── 检查 ──


@dataclass
class CheckResult:
    status: str  # up_to_date | update_available | unknown | error
    reason: str
    remote: RemoteInfo | None = None
    source: dict = field(default_factory=dict)
    sha256: str = ""

    @property
    def changed(self) -> bool:
        return self.status in ("update_available", "unknown")


def check(output: Path, *, deep: bool = False) -> CheckResult:
    """判断官方 PDF 是否有更新。

    先比 ETag + 大小(零下载);不一致时才下载核对 sha256,避免"文件被重新
    发布但内容没变"被误报成更新。``deep=True`` 强制下载核对。
    """
    manifest = load_json(manifest_path(output))
    source = dict(manifest.get("source") or {})
    local_pdf = pdf_path(output)

    try:
        remote = remote_info()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return CheckResult("error", f"HEAD 失败: {exc}", None, source)

    if not source.get("sha256"):
        return CheckResult(
            "unknown",
            "manifest 里没有 source.sha256(先跑一次 --build --force 记录基线)",
            remote,
            source,
        )

    state = load_json(remote_state_path(output))
    if (
        not deep
        and state.get("etag")
        and remote.etag
        and state["etag"] == remote.etag
        and int(state.get("bytes") or 0) == remote.bytes
    ):
        return CheckResult(
            "up_to_date", "ETag/大小未变(未下载)", remote, source, source["sha256"]
        )

    if not local_pdf.is_file() or state.get("sha256") != source["sha256"]:
        sha, size = download(remote.url, local_pdf)
    else:
        sha, size = file_sha256(local_pdf), local_pdf.stat().st_size

    if sha == source["sha256"]:
        record_remote_state(output, remote, sha)
        return CheckResult(
            "up_to_date",
            f"内容 sha256 未变(仅远端元数据变化,{size} 字节)",
            remote,
            source,
            sha,
        )
    return CheckResult(
        "update_available",
        f"sha256 {source['sha256'][:12]}… → {sha[:12]}…(旧 "
        f"{source.get('bytes')} → 新 {remote.bytes} 字节)",
        remote,
        source,
        sha,
    )


# ── 构建 ──


def build(output: Path, *, remote: RemoteInfo | None = None) -> dict:
    """用当前 PDF 重跑流水线;返回新 manifest。"""
    from build_knowledge import run_pipeline

    pdf = pdf_path(output)
    if not pdf.is_file():
        raise FileNotFoundError(f"缺少 PDF: {pdf}(先跑 --check 或提供 --pdf)")
    old = load_json(manifest_path(output))
    manifest = run_pipeline(
        pdf,
        output,
        mode="both",
        context_mode="breadcrumb",
        source_url=(remote.url if remote else OFFICIAL_PDF_URL),
    )
    if remote is None:
        with contextlib.suppress(Exception):
            remote = remote_info()
    if remote is not None:
        record_remote_state(output, remote, str(manifest["source"]["sha256"]))

    old_chunks = int((old.get("enhanced") or {}).get("chunk_count") or 0)
    new_chunks = int((manifest.get("enhanced") or {}).get("chunk_count") or 0)
    print(
        f"[build] pages={manifest['page_count']} version="
        f"{manifest['source'].get('version') or '-'} chunks={new_chunks}"
        f"(旧 {old_chunks})"
    )
    return manifest


# ── 上传 AstrBot 知识库 ──


def load_chunks(output: Path) -> list[str]:
    chunks_dir = output / "chunks"
    files = sorted(chunks_dir.glob("*.txt"))
    if not files:
        raise FileNotFoundError(f"缺少分块目录: {chunks_dir}(先跑 --build)")
    chunks = [path.read_text(encoding="utf-8") for path in files]
    too_long = [i for i, c in enumerate(chunks) if len(c) > CHUNK_CHARS_LIMIT]
    if too_long:
        print(f"[warn] {len(too_long)} 块超过 {CHUNK_CHARS_LIMIT} 字符,首块 {too_long[0]}")
    return chunks


_BREADCRUMB_RE = re.compile(r"^【(.+?)】", re.S)
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def chunk_chapter(chunk: str) -> str:
    """从块首的面包屑取一级章节(【文档 > 章节 > …】 的第二段)。

    章节是"增量重建"的最小单位:手册更新通常落在少数章节,按章节拆文档后
    未变化的章节可以整篇跳过,不必重新 embedding。
    """
    match = _BREADCRUMB_RE.match(chunk.lstrip())
    if not match:
        return ""
    parts = [part.strip() for part in match.group(1).split(">")]
    if len(parts) >= 2:
        return parts[1]
    return parts[0] if parts else ""


def safe_doc_name(text: str, *, limit: int = 80) -> str:
    cleaned = _UNSAFE_NAME_RE.sub(" ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit] or "misc"


def build_documents(
    chunks: list[str],
    *,
    version: str,
    sha256: str,
    prefix: str = DEFAULT_DOC_PREFIX,
    split: int = 1,
    group_by: str = "chapter",
    suffix: str = "md",
) -> list[dict]:
    """把分块列表切成待导入文档。

    ``group_by="chapter"``(默认):按一级章节拆文档,文档名带该章节内容的
    sha256 前 8 位。内容没变的章节名字不变 → 导入时直接跳过,只有变化的
    章节需要重新 embedding。
    ``group_by="fixed"``:沿用 1..N 等分(--split),用于网关限制请求体或
    想保持旧结构时;等分边界会随内容漂移,增量收益小。
    """
    version = version or "unknown"
    stamp = f"{prefix} v{version}"
    if group_by == "fixed":
        split = max(1, int(split))
        if split == 1 or len(chunks) <= split:
            return [{"file_name": f"{stamp} ({sha256[:8]}).{suffix}", "chunks": chunks}]
        size = -(-len(chunks) // split)
        documents = []
        for index in range(0, len(chunks), size):
            part = index // size + 1
            documents.append(
                {
                    "file_name": f"{stamp} ({sha256[:8]}) part{part}of{split}.{suffix}",
                    "chunks": chunks[index : index + size],
                }
            )
        return documents

    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for chunk in chunks:
        chapter = safe_doc_name(chunk_chapter(chunk))
        if chapter not in groups:
            groups[chapter] = []
            order.append(chapter)
        groups[chapter].append(chunk)
    documents = []
    for chapter in order:
        body = groups[chapter]
        digest = hashlib.sha256("\n".join(body).encode("utf-8")).hexdigest()[:8]
        documents.append(
            {
                "file_name": f"{stamp} - {chapter} ({digest}).{suffix}",
                "chunks": body,
            }
        )
    return documents


def plan_sync(
    documents: list[dict], existing: list[dict], prefix: str
) -> tuple[list[dict], list[dict]]:
    """对比目标文档与库内文档,返回 (待导入, 待删除)。

    名字里带内容指纹:同名 = 内容没变,跳过;新名字 = 新增/变化章节;
    库内匹配前缀但不在目标集合里的 = 已删除章节或旧版本,待删。
    """
    existing_names = {str(doc.get("doc_name") or "") for doc in existing}
    desired_names = {doc["file_name"] for doc in documents}
    to_import = [doc for doc in documents if doc["file_name"] not in existing_names]
    to_delete = [
        doc
        for doc in matching_docs(existing, prefix)
        if str(doc.get("doc_name") or "") not in desired_names
    ]
    return to_import, to_delete


def payload_path(output: Path) -> Path:
    return output / "kb_payload.json"


def write_payload(output: Path, documents: list[dict]) -> Path:
    path = payload_path(output)
    save_json(path, {"documents": documents, "batch_size": 32, "tasks_limit": 3})
    chunks = sum(len(doc["chunks"]) for doc in documents)
    print(f"[payload] {path} documents={len(documents)} chunks={chunks}")
    return path


def _client():
    from astrbot_api import AstrBotClient

    return AstrBotClient()


def _data(response: dict) -> dict:
    return response.get("data") or {}


def list_kbs(api) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        data = _data(api.get(f"/api/v1/knowledge-bases?page={page}&page_size=100"))
        batch = data.get("items") or []
        items.extend(batch)
        if len(items) >= int(data.get("total") or 0) or not batch:
            return items
        page += 1


def resolve_kb(api, *, kb_id: str = "", kb_name: str = DEFAULT_KB_NAME) -> str:
    """按 id 或名称定位知识库;名称匹配忽略大小写与前后空白。"""
    if kb_id:
        return kb_id
    wanted = (kb_name or "").strip().lower()
    items = list_kbs(api)
    for kb in items:
        if str(kb.get("kb_name") or "").strip().lower() == wanted:
            return str(kb["kb_id"])
    for kb in items:
        if wanted and wanted in str(kb.get("kb_name") or "").strip().lower():
            return str(kb["kb_id"])
    names = ", ".join(str(kb.get("kb_name")) for kb in items[:10])
    raise LookupError(f"没找到知识库 {kb_name!r};现有: {names}")


def list_docs(api, kb_id: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        data = _data(
            api.get(f"/api/v1/knowledge-bases/{kb_id}/documents?page={page}&page_size=100")
        )
        batch = data.get("items") or []
        items.extend(batch)
        if len(items) >= int(data.get("total") or 0) or not batch:
            return items
        page += 1


def matching_docs(docs: list[dict], prefix: str) -> list[dict]:
    needle = (prefix or "").strip().lower()
    if not needle:
        return []
    return [
        doc for doc in docs if str(doc.get("doc_name") or "").strip().lower().startswith(needle)
    ]


def import_documents(api, kb_id: str, documents: list[dict]) -> str:
    response = _data(
        api.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents/import",
            json={"documents": documents, "batch_size": 32, "tasks_limit": 3},
        )
    )
    task_id = str(response.get("task_id") or "")
    if not task_id:
        raise RuntimeError(f"导入未返回 task_id: {response}")
    print(f"[import] task={task_id} documents={len(documents)}")
    return task_id


def wait_task(api, task_id: str, *, timeout: float = 1800.0) -> dict:
    started = time.monotonic()
    while True:
        data = _data(api.get(f"/api/v1/knowledge-bases/tasks/{task_id}"))
        status = str(data.get("status") or "")
        if status == "completed":
            print(f"[import] completed in {time.monotonic() - started:.0f}s")
            return data
        if status == "failed":
            raise RuntimeError(f"导入失败: {data.get('error')}")
        if time.monotonic() - started > timeout:
            raise TimeoutError(f"导入超时({timeout:.0f}s): {data.get('progress')}")
        time.sleep(3)


def kb_stats(api, kb_id: str) -> dict:
    return _data(api.get(f"/api/v1/knowledge-bases/{kb_id}/stats"))


def delete_documents(api, kb_id: str, docs: list[dict]) -> int:
    removed = 0
    for doc in docs:
        doc_id = str(doc.get("doc_id") or "")
        if not doc_id:
            continue
        api.request("DELETE", f"/api/v1/knowledge-bases/{kb_id}/documents/{doc_id}")
        print(f"[replace] deleted {doc.get('doc_name')} ({doc_id})")
        removed += 1
    return removed


# ── CLI ──


def _print_check(result: CheckResult) -> None:
    remote = result.remote
    if remote is not None:
        print(
            f"[remote] bytes={remote.bytes} etag={remote.etag or '-'} "
            f"last-modified={remote.last_modified or '-'}"
        )
    print(f"[check] {result.status}: {result.reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kb_sync.py",
        description="检查 Cakewalk Sonar 官方 PDF 更新 → 重建 → 同步 AstrBot 知识库",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="只检查(默认)")
    actions.add_argument("--build", action="store_true", help="有更新才重建")
    actions.add_argument("--upload", action="store_true", help="重建产物导入知识库")
    actions.add_argument("--all", action="store_true", help="检查 → 按需重建 → 导入")
    actions.add_argument("--list-kbs", action="store_true", help="列出知识库")
    actions.add_argument("--list-docs", action="store_true", help="列出知识库文档")
    parser.add_argument("--force", action="store_true", help="忽略检查,强制重建/上传")
    parser.add_argument("--deep", action="store_true", help="强制下载核对 sha256")
    parser.add_argument("--kb-id", default=os.environ.get("CAKEWALK_KB_ID", ""))
    parser.add_argument(
        "--kb-name",
        default=os.environ.get("CAKEWALK_KB_NAME", DEFAULT_KB_NAME),
        help="知识库名称(找不到时报错并列出前 10 个)",
    )
    parser.add_argument(
        "--doc-prefix",
        default=os.environ.get("CAKEWALK_KB_DOC_PREFIX", DEFAULT_DOC_PREFIX),
        help="旧文档名前缀(--replace 时按它匹配)",
    )
    parser.add_argument(
        "--group-by",
        choices=["chapter", "fixed"],
        default="chapter",
        help="chapter=按一级章节拆文档(默认,便于增量重建);fixed=等分",
    )
    parser.add_argument("--split", type=int, default=1, help="fixed 模式下拆成几个文档")
    parser.add_argument("--replace", action="store_true", help="导入成功后删除旧文档")
    parser.add_argument("--dry-run", action="store_true", help="只生成 payload,不上传")
    parser.add_argument("--pdf", type=Path, default=None, help="手动指定本地 PDF")
    args = parser.parse_args(argv)

    output: Path = args.output.expanduser().resolve()
    if args.pdf:
        target = pdf_path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        if args.pdf.resolve() != target.resolve():
            target.write_bytes(args.pdf.read_bytes())
        print(f"[pdf] 使用本地文件: {target}")

    try:
        if args.list_kbs:
            for kb in list_kbs(_client()):
                print(
                    f"{kb.get('kb_id')}  {kb.get('emoji') or ''} "
                    f"{kb.get('kb_name')}  docs={kb.get('doc_count')} "
                    f"chunks={kb.get('chunk_count')}"
                )
            return 0

        if args.list_docs:
            api = _client()
            kb_id = resolve_kb(api, kb_id=args.kb_id, kb_name=args.kb_name)
            print(f"[kb] {args.kb_name or kb_id} → {kb_id}")
            for doc in list_docs(api, kb_id):
                print(
                    f"{doc.get('doc_id')}  {doc.get('doc_name')}  "
                    f"chunks={doc.get('chunk_count')}"
                )
            return 0

        result = check(output, deep=args.deep or args.force)
        _print_check(result)
        if result.status == "error":
            return 1
        if result.status == "update_available" and result.remote is not None:
            # check() 已把新 PDF 落到 source/;确保状态缓存同步
            record_remote_state(output, result.remote, result.sha256)

        need_build = args.force or result.status in ("update_available", "unknown")
        if args.check and not args.build and not args.upload and not args.all:
            if result.status == "up_to_date":
                return 0
            return 2 if result.status == "update_available" else 3

        if (args.build or args.all) and need_build:
            manifest = build(output, remote=result.remote)
            result = CheckResult(
                "up_to_date", "已重建", result.remote, manifest.get("source") or {}, result.sha256
            )
        elif args.build or args.all:
            manifest = load_json(manifest_path(output))
            print("[build] 内容未变化,跳过重建")

        if not (args.upload or args.all):
            return 0

        manifest = load_json(manifest_path(output))
        source = manifest.get("source") or {}
        if not source:
            print("error: manifest 缺少 source 信息,先跑 --build", file=sys.stderr)
            return 1
        chunks = load_chunks(output)
        documents = build_documents(
            chunks,
            version=str(source.get("version") or ""),
            sha256=str(source.get("sha256") or ""),
            prefix=args.doc_prefix,
            split=args.split,
            group_by=args.group_by,
        )
        write_payload(output, documents)

        if not args.upload and not args.all:
            return 0
        api = _client()
        kb_id = resolve_kb(api, kb_id=args.kb_id, kb_name=args.kb_name)
        print(f"[kb] {args.kb_name or kb_id} → {kb_id}")
        existing = list_docs(api, kb_id)
        to_import, to_delete = plan_sync(documents, existing, args.doc_prefix)
        print(
            f"[sync] 目标 {len(documents)} 个章节文档;新增/变化 {len(to_import)} 个,"
            f"待删旧文档 {len(to_delete)} 个"
        )
        if not to_import and not to_delete:
            # 幂等:cron 每天跑也不会重复 embedding
            print("[kb] 已是最新(章节内容与库内一致),跳过")
            return 0
        if args.dry_run:
            print("[dry-run] 只预览,不导入/不删除")
            return 0
        if to_delete and not args.replace:
            print("[warn] 有旧文档待删除,加 --replace 才会执行(否则会残留旧章节)")
        if to_import:
            preview = "、".join(doc["file_name"] for doc in to_import[:3])
            tail = " …" if len(to_import) > 3 else ""
            print(f"[sync] 需要重建 embedding 的章节: {preview}{tail}")
        before = kb_stats(api, kb_id)
        print(
            f"[kb] before docs={before.get('doc_count')} chunks={before.get('chunk_count')}"
        )
        if to_import:
            task_id = import_documents(api, kb_id, to_import)
            wait_task(api, task_id)
        if to_delete and args.replace:
            delete_documents(api, kb_id, to_delete)
        after = kb_stats(api, kb_id)
        print(
            f"[kb] after docs={after.get('doc_count')} chunks={after.get('chunk_count')}"
        )
        return 0
    except (LookupError, FileNotFoundError, RuntimeError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
