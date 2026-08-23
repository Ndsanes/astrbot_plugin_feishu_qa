# FINAL REPORT — PDF → AstrBot 知识库预处理流水线

日期: 2026-08-23 | 输入: Cakewalk Sonar Reference Guide.pdf (1900 页, 50MB)

## 1. Implementation

`tools/knowledge/build_knowledge.py` + `kb_pipeline/`(extract/cleaner/structure/
chunker/context_llm/baseline/evaluate)+ `run_benchmark.py` + `make_dataset.py`。
测试 11 项(单元+集成),全套 pytest 通过。

## 2. Chosen parser

pymupdf 1.28.2(AGPL-3.0,PyPI)。理由:零 torch 依赖、wheel 直装、
可按 span 字号做标题识别。marker/docling/mineru 未安装
(均为重量级框架,对本手册的纯文本版收益有限,留作 fallback)。

## 3. Chunk strategy

字号聚类定级(正文众数 9pt;19→L1 章、14→L2 节、12→L3 小节);
折行按句末标点合并成语义段;连续编号段聚合为原子列表块;
L1/L2 边界强制开新块;超限列表按步骤边界分组;
默认 min=250 / max=500 tokens(approximate_chars_4 计数)。

## 4. Breadcrumb strategy

`【Document > Chapter > Section > Subsection】`前缀拼入块首,
进入 embedding 与 sparse 索引输入。

## 5. LLM Context strategy

已实现并实测(bge-m3 视角外 BM25-lite sparse 代理):
每批 24 块、ox-alpha-free 生成、sha256 缓存、重跑零调用。
结果未达启用门槛(见 §7),最终推荐 breadcrumb only。

## 6. Dataset

43 queries:exact 6 / paraphrase 8 / symptom 6 / procedure 8 / concept 4 /
cross-language 6 / distractor(unanswerable) 5。ground truth = 相关小节标题串。

## 7. Benchmark(BM25-lite sparse 代理,38 answerable + 5 unanswerable)

| Pipeline | R@1 | R@3 | R@5 | MRR@5 | FP@3 |
|---|---|---|---|---|---|
| Baseline (9119 块) | 0.2895 | 0.4474 | 0.5263 | 0.3781 | 0 |
| Breadcrumb (3166 块) | **0.3947** | 0.5000 | 0.5789 | 0.4583 | 0 |
| Breadcrumb+LLM | 0.3684 | **0.5263** | 0.5789 | **0.4605** | 0 |

- breadcrumb vs baseline: R@1 **+36.3%**, MRR@5 **+21.2%**, R@5 +10.0%
- LLM vs breadcrumb: R@3 +2.6pp, MRR@5 +0.5pp, R@1 −6.7pp → 无显著净收益
- 三方案 FP@3 均为 0

## 8. Cost(LLM contextualization)

1900 页 → 3166 chunks;121 batches × ~24 块;估算 input ≈ 0.9M tokens、
output ≈ 90K tokens;墙钟 ~35 分钟(5 并发);缓存命中后重跑 0 调用。

## 9. AstrBot integration

预分块导入:`POST /knowledge-bases/{kb_id}/documents/import`,
payload documents[].chunks 直接入库不二次切片(源码 kb_helper.py:262 验证)。
WebUI 手动上传 txt 则被 RecursiveCharacter(500c/100o) 二次切片,
breadcrumb 仅存于首片——推荐配置 chunk_size ≥ 1200 字符。
当前 API key 无 KB scope,在线检索验证 pending manual upload(§59 如实标注)。

## 9.5 Online verification(真实 AstrBot 检索)

语料已导入线上知识库 "Cakewalk sonar"(替换指向,旧库改名保留未删),
通过 `POST /knowledge-bases/{kb}/retrieve`(bge-m3 dense + sparse + Qwen3-Reranker)
对同一 43 query 集实测:

| R@1 | R@3 | R@5 | MRR@5 | FP@3 |
|---|---|---|---|---|
| 0.5526 | 0.7632 | 0.8684 | 0.6697 | 0/5 |

对比离线 sparse 代理(Breadcrumb R@5=0.579):真实混合检索把 R@5 推到 0.868。
模糊中文问题抽查均命中相关章节(导出音频→Exporting audio, 录音延迟→Audio Sync)。

## 10. Limitations

- benchmark 用 BM25-lite sparse 代理,未含真实 dense/rerank 路径;
  LLM context 的语义增益在 dense 下可能更好,本数据无法证明
- 在线 AstrBot 检索验证待手动上传后补测
- token 计数为近似值;4 个块 >575 tokens(编号列表原子性优先)
- pymupdf 为 AGPL-3.0,商用闭源分发需替换

## 11. Recommendation

**使用 Breadcrumb 方案**(即 `--context-mode breadcrumb`,默认值)。
LLM context 未证明值得其复杂度与成本,保留 `--context-mode llm` 与缓存供复验。
