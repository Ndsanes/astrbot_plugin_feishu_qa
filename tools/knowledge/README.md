# build_knowledge — PDF → AstrBot 知识库预处理流水线

把长篇技术手册 PDF 转换成对 embedding + sparse/dense 检索友好的知识库语料,
并提供 baseline / breadcrumb / breadcrumb+LLM 三方案的真实召回评测。

## 解决什么问题

整本 PDF 直接压平入库会丢失标题层级、混入页眉页脚噪声、切块跨语义边界。
本工具恢复文档结构、按结构分块、给每块加面包屑前缀,并用真实查询集量化收益。

## 为什么不能直接 pdftotext

pdftotext 式提取抹平 heading hierarchy / 段落边界 / 列表结构——这些本身就是检索信号。

## 为什么需要 breadcrumb

技术手册的查询大多是主题式("怎么同步控制台"),面包屑让标题词进入
embedding 与 sparse 索引,不再依赖正文恰好含同义词。实测(见 eval/benchmark.json):
MRR@5 相对 baseline 提升 ~21%,R@1 提升 ~36%。

## 何时启用 LLM Contextualization

`--context-mode llm`(需外部提供 LLM 后端)在本次 Cakewalk 实测中
R@3 +2.6pp 但 R@1 -2.6pp,MRR 基本持平——**默认不启用**。
当手册语言与查询语言差异大、或章节标题信息量低时可再实验。

## 运行

```bash
python build_knowledge.py --input "Cakewalk Sonar Reference Guide.pdf" \
    --output ./output/cakewalk --mode both \
    --min-tokens 250 --max-tokens 500
python run_benchmark.py --output ./output/cakewalk \
    --llm-chunks ./output/cakewalk/llm_chunks   # 若已做 LLM enrichment
python make_dataset.py   # 重新生成评测集
```

## 上传 AstrBot

推荐走**预分块导入**路径(AstrBot `POST /knowledge-bases/{kb_id}/documents/import`,
payload `{"documents": [{"file_name": "...", "chunks": ["0001_....txt", ...]}]}`),
块内容原样入库、不被二次切片。若用 WebUI 手动上传 txt,AstrBot 会以
RecursiveCharacter(500 字符/100 重叠) 二次切片,breadcrumb 前缀只在首段保留。

## 检查更新 / 自动同步知识库

官方手册固定托管在
`https://bandlab.github.io/cakewalk/docs/Cakewalk%20Sonar%20Reference%20Guide.pdf`。
`kb_sync.py` 把"检查 → 重建 → 导入"串成一条命令:

```bash
python tools/knowledge/kb_sync.py --check        # 只检查(0=最新 / 2=有更新 / 3=缺基线)
python tools/knowledge/kb_sync.py --all --kb-name "Cakewalk sonar" --replace
python tools/knowledge/kb_sync.py --list-docs --kb-name "Cakewalk sonar"
python tools/knowledge/kb_sync.py --upload --dry-run   # 只生成 payload
```

- **检查**先比远端 `ETag + Content-Length`,不一致才下载核对 `sha256`。
  官方文件被重新发布但内容没变时不会被误判成更新(2026-10-24 实测:
  `last-modified` 变了、正文与分块逐字节不变)。
- `manifest.json` 的 `source` 记录 `sha256 / bytes / page_count / version
  / pdf_mod_date`,这是判定基线;`source/remote.json` 缓存远端校验头。
- **导入**走预分块接口(`documents/import`),块原样入库;`--split N` 用于
  网关限制请求体时拆成 N 个文档。导入是后台任务,脚本会轮询到完成。
- `--replace` 会删除名字以 `--doc-prefix`(默认 `Cakewalk Sonar Reference
  Guide`)开头的旧文档,**先跑 `--list-docs` 确认匹配范围**。
- 退出码:`0` 最新/成功、`2` 有更新、`3` 缺基线(先 `--build --force`)、
  `1` 出错。cron 可以据此决定是否继续。

每天自动跑一次(API Key 30 天过期,过期后 `--check` 仍可用,上传会失败):

```cron
0 5 * * * cd <repo> && .venv/bin/python tools/knowledge/kb_sync.py \
    --all --kb-name "Cakewalk sonar" --replace >> output/cakewalk/sync.log 2>&1
```

## 推荐 KB 配置

- Embedding: BAAI/bge-m3(多语,中问英答可用)
- Sparse: enabled(Dense: enabled)
- Chunk size / overlap: 预分块导入时由本工具控制(≈300 tokens/块);
  手动上传时建议 chunk_size ≥ 1200 字符以减少二次切割破坏 breadcrumb

## 如何重新 benchmark

```bash
python run_benchmark.py --output ./output/cakewalk --llm-chunks ./output/cakewalk/llm_chunks
```

## Parser 与许可证

- 提取: pymupdf 1.28.2(PyPI wheel,**AGPL-3.0**)——内部实验工具用途;
  若未来需要商用分发闭源衍生品,请替换为 pypdf(MIT)并接受布局精度损失
- BM25 代理/指标/分块: 标准库实现,无额外依赖
