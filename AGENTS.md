# Repository Guidelines

## Project Overview

Workspace with cooperating Python packages for AstrBot bots bridging Feishu/Lark and QQ:

- `astrbot_plugin_feishu_qa/` — the AstrBot plugin (`Star` subclass in `main.py`). Fetches a Feishu Q&A document, builds a local corpus snapshot, answers group questions with deterministic retrieval (zero LLM calls at high confidence) and falls back to the main Agent's `search_feishu_qa` tool only when confidence is low. **零 kit 依赖**：飞书访问全部经 `astrbot_plugin_lark_cli_platform` 的 gateway 转发。
- `astrbot_plugin_lark_cli_platform/` — 飞书平台适配器：lark-cli 事件流接入 AstrBot Platform API，并向其他插件暴露 `gateway.*`（消息发送固定 bot 身份；能力透传按需选身份）。是 `astrbot_lark_kit` 的唯一消费者，打包时把 kit 源码烘焙进插件 zip。
- `astrbot_lark_kit/` — shared thin wrapper around the external `lark-cli` binary: subprocess invocation, envelope parsing, auth-status health checks, bootstrap download, rate limiting, typed errors, and pure platform-identity parsing (`platforms.py`). **已发版为独立包**（GitHub `Ndsanes/astrbot_lark_kit`，tag v0.2.7）。
- `astrbot_plugin_bili_verify_feishu/` — 独立嵌套 git 仓库（公开），B 站入群审核插件；多维表格经 bot 身份走同一 gateway 读写。
- `AstrBot/docs/` — official VitePress-based documentation source. Contains user guides, configuration references, API documentation, and plugin development tutorials in Markdown. Readable as plain text without building; VitePress adds navigation and search when served.

Docs, docstrings, comments, and user-facing strings are in Chinese. Keep that convention.

## Architecture & Data Flow

```
Feishu wiki ──lark-cli 平台适配器 gateway──▶ GatewayClient (feishu_qa)
                              │
                        corpus parse/build → SnapshotStore (atomic commit)
                              │
              ┌───────────────┴───────────────┐
      deterministic retrieval (Tier 0)   AstrBot main Agent
   HIGH confidence → direct answer +    LOW → search_feishu_qa tool
   merged-forward images, stop_event        with restricted snippets
```

Key flow points an editor must preserve:

1. **Sync failure ≠ broken Q&A.** `SnapshotStore` writes to `.staging/`, then atomically swaps `corpus.json`. On load failure or sync exception, the old in-memory corpus keeps serving (`main.py` `_load_corpus` returns False and keeps old entries).
2. **Stable entry IDs are content-derived**, not Feishu block IDs. Feishu regenerates all block IDs on any structural edit, so `corpus/model.py::derive_entry_id` uses `sha256(normalized_section + "|" + ordinal-stripped_title)[:16]` → `qa_<hex>`. `source_locator` is metadata only.
3. **Images are downloaded by `file_token` to disk** before sending (Feishu temp URLs don't work on QQ). A single image download failure is logged, never fatal.
4. **Confidence routing** lives in `answer/router.py`: whitelist gate first (`denied` = zero response), then top-1 score vs `HIGH_CONFIDENCE_THRESHOLD` (direct) / `MEDIUM_CONFIDENCE_THRESHOLD` (agent tool) / below (miss text). All zero-LLM except the agent path.
5. **Retrieval content injected into the agent goes through `extra_user_content_parts`**, never into the system prompt (keeps prompt cache intact).
6. **多来源语料分片**:`corpus.json` 是**派生视图**,由 `corpus_sources/<source>.json` 全部分片经 `merge_manifests` 重算。任何一个来源的同步**只能写自己的分片**,不得直接 `commit` 到 `corpus.json` — 否则两个来源会每轮同步互相覆盖(2026-10-07 线上事故:飞书 47 条与网页 76 条轮流消失)。合并按 entry ID 去重、飞书优先(网页 FAQ 段是飞书文档的镜像,同章节+同标题 → 同 ID)。条目 `source` 字段决定直达链接与署名的生成方式(飞书拼块锚点、网页用其自身 URL)。

## Key Directories

- `astrbot_plugin_feishu_qa/main.py` — plugin entry: commands (`/问`, `/qa_status`, `/qa_sync`, `/qa_reload`, `/learn ok|no`), message handlers, tool registration, background sync/auth tasks
- `astrbot_plugin_feishu_qa/adapter/` — `gateway.py`（`GatewayClient`，对平台网关公开方法的薄封装；resolver 每次调用时解析，天然支持适配器晚启动）
- `astrbot_plugin_feishu_qa/corpus/` — `model.py` (dataclasses `QaEntry`/`QaImage`, ID derivation), `parser.py` (doc XML → entries), `builder.py` (manifest build/diff/content hash)
- `astrbot_plugin_feishu_qa/retrieval/scorer.py` — deterministic `Retriever`, `Confidence` levels
- `astrbot_plugin_feishu_qa/answer/` — `router.py` (whitelist + confidence routing, pure logic), `direct.py` (direct-answer formatting)
- `astrbot_plugin_feishu_qa/storage/snapshot.py` — atomic snapshot persistence (`SnapshotStore`)
- `astrbot_plugin_feishu_qa/learn/` — `candidate.py` `/learn` candidate extraction + pending-QA store; `writeback.py` 写回纯逻辑（块构造、查重守卫、记录键）。真实写回只允许指向 `LEARNING_WRITEBACK_DOC_URL` 配置的**副本文档**，生产文档写回是人工步骤
- `astrbot_plugin_feishu_qa/fuuumusic.py` — 小闻的奇妙屋(https://www.fuuumusic.com) 语料接入:抓取问答聚合页 `/cakewalk-sonar-faq/all`(100 条 = 47 FAQ + 53 篇官方文档翻译),按站点自身的 `<details class="qa-item" id="qN" data-qtitle="【症状标签】问题？">` 结构拆成逐条 `QaEntry`(复用飞书条目字段形态,故检索/闸门/Jev/链接渲染无需改动);`resolve_sections` 处理章节白名单(过期即退化为不过滤);所有 HTTP 经 `asyncio.to_thread`(**绝不可在协程里直接跑同步 urllib**,会卡死整个机器人)
- `astrbot_plugin_feishu_qa/tools/build_qa_corpus.py` — 离线/在线 corpus build CLI
- `astrbot_lark_kit/` — `cli.py` / `envelope.py` / `errors.py`（含 `UmoParseError`）/ `rate_limit.py` / `auth.py` / `bootstrap.py` / `events.py` / `messaging.py` / `state.py` / `platforms.py`（UMO → 平台实例身份，纯解析层）；`pyproject.toml` 零运行时依赖可安装
- `tests/` under each package; `astrbot_plugin_feishu_qa/tests/fixtures/` holds real-corpus snapshots (`qa_r8268.xml`, `qa_r8394.xml`, `meta.json`, `retrieval_queries.json`)

## Development Commands

Run from the workspace root:

```bash
# Tests (whole suite)
pytest -q                                   # ~174 tests (kit + feishu_qa); bili_verify 另跑

# Single package / file
pytest astrbot_plugin_feishu_qa/tests/test_main.py -q
pytest astrbot_lark_kit/tests -q

# Lint
ruff check .

# Corpus build (offline uses fixtures; idempotency check builds twice and compares hashes)
python astrbot_plugin_feishu_qa/tools/build_qa_corpus.py --offline
python astrbot_plugin_feishu_qa/tools/build_qa_corpus.py --check-idempotent
```

No build step exists; packages run from source.

## Code Conventions & Common Patterns

- **Python ≥3.12**, `from __future__ import annotations` at every module top, full type hints, modern unions (`str | None`), dataclasses over plain dicts where structure matters (`DocContent`, `AnswerPlan`, `QaEntry`).
- **Ruff**: line-length 100, target py312, rules `E,F,W,I,UP,B,SIM` with `SIM108` ignored. Run before handing off.
- **Plugin imports must be relative**: AstrBot loads plugins as `data.plugins.<目录名>.main` and does NOT put `data/plugins/` on `sys.path`, so top-level package imports (`from astrbot_plugin_feishu_qa.x import …`) fail at runtime (verified against a real instance). Use `from .x import …` everywhere. 跨插件依赖一律走 `lark_cli` 平台网关（运行时经 StarTools/平台管理器解析，不做顶层 import）；`astrbot_lark_kit` 仅被 `astrbot_plugin_lark_cli_platform` 消费，打包时烘焙为 zip 内 vendored 子包。
- **Async everywhere at the adapter boundary**: `async def` for anything touching `lark-cli`; pure logic (`Retriever`, `AnswerRouter.route`, parsers) stays synchronous. Handlers are async generators yielding message chain items; high-confidence direct answers call `event.stop_event()` to block the LLM pipeline.
- **Errors**: kit raises a stable exception hierarchy rooted at `LarkKitError` (`AuthRequiredError`, `CliNotFoundError`, `CliTimeoutError`, `CliExecutionError`, `CliInvalidOutputError`). Downstream code never inspects raw envelopes. Envelope-shaped CLI output goes through `run_lark_cli`; bare JSON (`auth status`) through `run_lark_cli_json`.
- **Degradation over failure**: per-image download failures are warnings; corrupt snapshots return `None` and callers keep serving old state; 登录态过期由平台适配器自动巡检并推送授权卡片，不崩溃业务链路。
- **Config** comes from `_conf_schema.json` keys passed as `AstrBotConfig` dict (e.g. `WIKI_URL`, `ENABLED_GROUPS`, thresholds). Empty `ENABLED_GROUPS` = disabled everywhere; `["*"]` = all groups.
- **Persistence root** is `data/plugin_data/astrbot_plugin_feishu_qa/` (`corpus.json`, `images/`, `pending_learn.json`); tests redirect it via the `ASTRBOT_DATA_DIR` env var.

## Important Files

- [astrbot_plugin_feishu_qa/main.py](astrbot_plugin_feishu_qa/main.py) — entry point, `@register`d `FeishuQaPlugin(Star)`
- [astrbot_plugin_feishu_qa/_conf_schema.json](astrbot_plugin_feishu_qa/_conf_schema.json) — WebUI config schema (single source of config truth)
- [astrbot_plugin_feishu_qa/metadata.yaml](astrbot_plugin_feishu_qa/metadata.yaml) — plugin manifest; `support_platforms: aiocqhttp + qq_official`, requires `astrbot >=4.5.7`
- [astrbot_plugin_feishu_qa/corpus/model.py](astrbot_plugin_feishu_qa/corpus/model.py) — ID stability rules (read before touching parsing/identity)
- [astrbot_lark_kit/__init__.py](astrbot_lark_kit/__init__.py) — kit public API surface
- [astrbot_plugin_feishu_qa/tests/VERIFICATION_REPORT.md](astrbot_plugin_feishu_qa/tests/VERIFICATION_REPORT.md) — 历史验收快照（2026-08-23，网关重构前架构），仅作存档；现行架构以本文件为准

## Runtime/Tooling Preferences

- Plain Python 3.12+; **no third-party pip dependencies** (spec §0.14 minimal-dependency principle). `astrbot_lark_kit` 仅 `astrbot_plugin_lark_cli_platform` 使用：开发时走 workspace 源码（根 `pyproject.toml` 的 `pythonpath=["."]`），发布时打包脚本把 kit 源码烘焙进插件 zip；feishu_qa/bili_verify 对 kit 零依赖。
- `lark-cli` is **carried by the platform plugin**: packaging flow vendors official release binaries (linux-amd64/arm64, sha256-verified) into `vendor/lark-cli/<platform>/`; 缺失时适配器无条件自举下载。Resolution order: explicit `bin_path` injection (tests) → vendored binary → `LARK_CLI_PATH` env → PATH. Auth state is redirected via subprocess `HOME` into `<plugin data>/lark_cli_home/` so login survives container rebuilds; user 登录态由适配器自动巡检维护（启动即首查 + 每 auth_check_hours 巡检），也可用平台命令手动触发设备授权。
- No lockfile, no package manager beyond pip; do not add dependencies without strong justification.

## Testing & QA

- pytest with `asyncio_mode = "auto"` (bare `async def test_*` works, no decorators). 根 `testpaths` 含 `astrbot_lark_kit/tests` 与 `astrbot_plugin_feishu_qa/tests`；bili_verify 是独立仓库（gitignore），测试用 `pytest astrbot_plugin_bili_verify_feishu/tests -q` 显式运行（其 tests/stubs 提供 astrbot 假包）。
- 当前基线（2026-08-24 代码清理轮）：kit 88 + feishu_qa 86 = **174 passed**（根套件）+ bili_verify 49 = 全家桶 223。基线值只增不减，验收标准是"全套 PASS"而非固定数字。
- **bili_verify 白名单条目形态（v0.0.7 教训）**：qq_official 条目须为完整 UMO `实例ID:GroupMessage:群openid`（多 bot 轮询按首段定位归属 bot）；`AdmissionsStore.is_whitelisted` 同时接受裸 openid 与 UMO 全串（按末段匹配）。改任何一侧的数据形态时，轮询、校验、审批三条链路必须同步，否则会出现"能拉到申请但被判非白名单"的静默丢弃。
- When the real `astrbot` package isn't installed, [conftest.py](astrbot_plugin_feishu_qa/tests/conftest.py) prepends `tests/stubs/` containing a minimal fake `astrbot` package (`api.event.AstrMessageEvent`, `api.star.Star`, etc.) so the suite runs offline.
- Tests use real fixture corpora (`qa_r8268.xml`, revision-tagged) rather than mocks for parser/builder/retrieval paths; `test_main.py` builds a live plugin instance against a `tmp_path` data dir via `monkeypatch.setenv("ASTRBOT_DATA_DIR", ...)`.
- Retrieval regression queries live in `tests/fixtures/retrieval_queries.json`; keep them passing when touching `retrieval/scorer.py`.
- Corpus builds must stay idempotent: building twice must yield identical `content_hash` (enforced by `--check-idempotent`).


## AstrBot 实例 OpenAPI 访问

- 已部署实例：`https://astrbot.ngames.work/`，凭据与配置在仓库根目录 [.env](.env)（已 gitignore）：`ASTRBOT_BASE_URL`、`ASTRBOT_API_KEY`、`ASTRBOT_API_KEY_ISSUED_AT`（Key 有效期 30 天）、`ASTRBOT_DASHBOARD_USERNAME/PASSWORD`（管理员面板账号）。
- **scope 不足时自动提权**：仓库 API key 无 logs 等 scope（403 `Insufficient API key scope`），且 API key 无法自签发更高权限。客户端遇 403 会自动用管理员账号登录 `POST /api/v1/auth/login` 换 JWT（scopes=["*"]，注意本实例回包字段是 `data.token`）重试。JWT 落盘缓存 `.astrbot_jwt_cache`（gitignored）：解析 payload `exp` 判过期、401 才强制重登，进程内与跨进程均复用，**不要每次请求都重新登录**。
- 看线上日志：`python3 astrbot_api.py logs [过滤关键词] [条数]`（走 `/api/v1/logs/history`，自动清 ANSI 颜色码）。日志行为 `{"level","time","data"}` 结构，`data` 内嵌完整格式化行。
- 封装客户端：[astrbot_api.py](astrbot_api.py)，零第三方依赖（urllib）。用法：

```python
from astrbot_api import AstrBotClient
api = AstrBotClient()
api.get("/api/v1/plugins")
api.post("/api/v1/plugins/reload", json={})
```

  命令行探测：`python3 astrbot_api.py plugins`。Key 过期前 ≤7 天、已过期、以及 401 时会打印更新提醒（抛 `ApiKeyExpired`）。
- **⚠️ 禁止对 `PUT /api/v1/system-config` 做"GET→修改→PUT 全量回写"**（2026-08-24 确诊，已三次触发 WebUI"旧版本密码存储"提示并一度锁死登录）：该端点是全量替换语义（源码 `update_profile`: "Complete replacement config content"，落盘整节覆盖），而 GET 返回的 `dashboard` 节剥离了 `username/password/pbkdf2_password/jwt_secret` 敏感键——回写即抹掉凭据；`kb_names` 被静默回滚也是同一机制。改实例配置一律走局部端点（bots/plugins/kb 各自 API）。发版部署前跑 `python3 astrbot_api.py health` 检查仪表盘凭据完整性（检查 stats 升级标志 + dashboard.username 是否存在）。若凭据已被抹（login 500 / PATCH 报原密码错误）：API 层无解，需重启容器让 loader 重新生成初始密码（打印在启动日志），再 login + `PATCH /auth/account` 改回 .env 凭据。
- 本地 API 规范：[.reference/astrbot-openapi.json](.reference/astrbot-openapi.json)（224 个端点，来自实例 `/api/v1/openapi.json`；Scalar 文档页 `https://docs.astrbot.app/scalar.html` 只是渲染壳）。查端点：

```bash
python3 -c "import json; spec=json.load(open('.reference/astrbot-openapi.json')); print('\n'.join(spec['paths']))"
```
