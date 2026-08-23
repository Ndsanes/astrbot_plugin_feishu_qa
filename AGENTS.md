# Repository Guidelines

## Project Overview

Workspace with two cooperating Python packages that form a Feishu-wiki-driven Q&A bot for QQ groups on AstrBot:

- `astrbot_plugin_feishu_qa/` — the AstrBot plugin (`Star` subclass in `main.py`). Fetches a Feishu Q&A document, builds a local corpus snapshot, answers group questions with deterministic retrieval (zero LLM calls at high confidence) and falls back to the main Agent's `search_feishu_qa` tool only when confidence is low.
- `astrbot_lark_kit/` — shared thin wrapper around the external `lark-cli` binary: subprocess invocation, envelope parsing, auth-status health checks, rate limiting, typed errors. The plugin imports it as a sibling package.
- `AstrBot/docs/` — official VitePress-based documentation source. Contains user guides, configuration references, API documentation, and plugin development tutorials in Markdown. Readable as plain text without building; VitePress adds navigation and search when served.

Docs, docstrings, comments, and user-facing strings are in Chinese. Keep that convention.

## Architecture & Data Flow

```
Feishu wiki ──lark-cli──▶ LarkAdapter (+ AuthKeeper health monitor)
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

## Key Directories

- `astrbot_plugin_feishu_qa/main.py` — plugin entry: commands (`/问`, `/qa_status`, `/qa_sync`, `/qa_reload`, `/learn ok|no`), message handlers, tool registration, background sync/auth tasks
- `astrbot_plugin_feishu_qa/adapter/` — `lark.py` (`LarkAdapter`, doc fetch/media download/auth polling), `auth.py` (`AuthKeeper`)
- `astrbot_plugin_feishu_qa/corpus/` — `model.py` (dataclasses `QaEntry`/`QaImage`, ID derivation), `parser.py` (doc XML → entries), `builder.py` (manifest build/diff/content hash)
- `astrbot_plugin_feishu_qa/retrieval/scorer.py` — deterministic `Retriever`, `Confidence` levels
- `astrbot_plugin_feishu_qa/answer/` — `router.py` (whitelist + confidence routing, pure logic), `direct.py` (direct-answer formatting)
- `astrbot_plugin_feishu_qa/storage/snapshot.py` — atomic snapshot persistence (`SnapshotStore`)
- `astrbot_plugin_feishu_qa/learn/candidate.py` — `/learn` candidate extraction, pending-QA store (never writes back to Feishu)
- `astrbot_plugin_feishu_qa/tools/build_qa_corpus.py` — offline/online corpus build CLI
- `astrbot_lark_kit/` — `cli.py` (`run_lark_cli` / `run_lark_cli_json`), `envelope.py`, `errors.py` (`LarkKitError` hierarchy), `rate_limit.py` (`RateLimiter`), `auth.py`
- `tests/` under each package; `astrbot_plugin_feishu_qa/tests/fixtures/` holds real-corpus snapshots (`qa_r8268.xml/json/md`, `qa_r8394.xml`, `meta.json`, `retrieval_queries.json`)

## Development Commands

Run from the workspace root:

```bash
# Tests (whole suite)
pytest -q                                   # ~97 tests across both packages

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
- **Plugin imports must be relative**: AstrBot loads plugins as `data.plugins.<目录名>.main` and does NOT put `data/plugins/` on `sys.path`, so top-level package imports (`from astrbot_plugin_feishu_qa.x import …`) fail at runtime (verified against a real instance). Use `from .x import …` everywhere. The `astrbot_lark_kit` dependency is imported top-level first (dev: workspace root via `pythonpath = ["."]`) with an ImportError fallback to the vendored `..astrbot_lark_kit` sub-package that `tools/package_astrbot_zip.sh` bakes into the zip. Deployment-layout regression gate: [astrbot_plugin_feishu_qa/tests/test_deployment_layout.py](astrbot_plugin_feishu_qa/tests/test_deployment_layout.py).
- **Async everywhere at the adapter boundary**: `async def` for anything touching `lark-cli`; pure logic (`Retriever`, `AnswerRouter.route`, parsers) stays synchronous. Handlers are async generators yielding message chain items; high-confidence direct answers call `event.stop_event()` to block the LLM pipeline.
- **Errors**: kit raises a stable exception hierarchy rooted at `LarkKitError` (`AuthRequiredError`, `CliNotFoundError`, `CliTimeoutError`, `CliExecutionError`, `CliInvalidOutputError`). Downstream code never inspects raw envelopes. Envelope-shaped CLI output goes through `run_lark_cli`; bare JSON (`auth status`) through `run_lark_cli_json`.
- **Degradation over failure**: per-image download failures are warnings; corrupt snapshots return `None` and callers keep serving old state; auth expiry triggers admin notification (`ADMIN_OPEN_ID`) plus re-auth card instead of crashing.
- **Config** comes from `_conf_schema.json` keys passed as `AstrBotConfig` dict (e.g. `WIKI_URL`, `ENABLED_GROUPS`, thresholds). Empty `ENABLED_GROUPS` = disabled everywhere; `["*"]` = all groups.
- **Persistence root** is `data/plugin_data/astrbot_plugin_feishu_qa/` (`corpus.json`, `images/`, `pending_learn.json`); tests redirect it via the `ASTRBOT_DATA_DIR` env var.

## Important Files

- [astrbot_plugin_feishu_qa/main.py](astrbot_plugin_feishu_qa/main.py) — entry point, `@register`d `FeishuQaPlugin(Star)`
- [astrbot_plugin_feishu_qa/_conf_schema.json](astrbot_plugin_feishu_qa/_conf_schema.json) — WebUI config schema (single source of config truth)
- [astrbot_plugin_feishu_qa/metadata.yaml](astrbot_plugin_feishu_qa/metadata.yaml) — plugin manifest; `support_platforms: aiocqhttp` only, requires `astrbot >=4.5.7`
- [astrbot_plugin_feishu_qa/corpus/model.py](astrbot_plugin_feishu_qa/corpus/model.py) — ID stability rules (read before touching parsing/identity)
- [astrbot_lark_kit/__init__.py](astrbot_lark_kit/__init__.py) — kit public API surface
- [astrbot_plugin_feishu_qa/tests/VERIFICATION_REPORT.md](astrbot_plugin_feishu_qa/tests/VERIFICATION_REPORT.md) — acceptance gates and known platform limitations (e.g. merged-forward messages not adapted for `qq_official`)

## Runtime/Tooling Preferences

- Plain Python 3.12+; **no third-party pip dependencies** (spec §0.14 minimal-dependency principle). `requirements.txt` is intentionally empty of packages — `lark-kit` is consumed as sibling source via `pythonpath = ["."]`.
- `lark-cli` is **carried by the plugin**: packaging script downloads official release binaries (linux-amd64/arm64, sha256-verified) into `vendor/lark-cli/<platform>/`. Resolution order: explicit `bin_path` injection (tests) → vendored binary → `LARK_CLI_PATH` env → PATH. Auth state is redirected via subprocess `HOME` into `<plugin data>/lark_cli_home/` so login survives container rebuilds; admins authorize headlessly with `/qa_auth_login` or the auto-pushed re-auth card.
- No lockfile, no package manager beyond pip; do not add dependencies without strong justification.

## Testing & QA

- pytest with `asyncio_mode = "auto"` (bare `async def test_*` works, no decorators) and `testpaths` limited to both `tests/` directories.
- When the real `astrbot` package isn't installed, [conftest.py](astrbot_plugin_feishu_qa/tests/conftest.py) prepends `tests/stubs/` containing a minimal fake `astrbot` package (`api.event.AstrMessageEvent`, `api.star.Star`, etc.) so the suite runs offline.
- Tests use real fixture corpora (`qa_r8268.xml`, revision-tagged) rather than mocks for parser/builder/retrieval paths; `test_main.py` builds a live plugin instance against a `tmp_path` data dir via `monkeypatch.setenv("ASTRBOT_DATA_DIR", ...)`.
- Retrieval regression queries live in `tests/fixtures/retrieval_queries.json`; keep them passing when touching `retrieval/scorer.py`.
- Corpus builds must stay idempotent: building twice must yield identical `content_hash` (enforced by `--check-idempotent`).


## AstrBot 实例 OpenAPI 访问

- 已部署实例：`https://astrbot.ngames.work/`，凭据与配置在仓库根目录 [.env](.env)（已 gitignore）：`ASTRBOT_BASE_URL`、`ASTRBOT_API_KEY`、`ASTRBOT_API_KEY_ISSUED_AT`（Key 有效期 30 天）。
- 封装客户端：[astrbot_api.py](astrbot_api.py)，零第三方依赖（urllib）。用法：

```python
from astrbot_api import AstrBotClient
api = AstrBotClient()
api.get("/api/v1/plugins")
api.post("/api/v1/plugins/reload", json={})
```

  命令行探测：`python3 astrbot_api.py plugins`。Key 过期前 ≤7 天、已过期、以及 401 时会打印更新提醒（抛 `ApiKeyExpired`）。
- 本地 API 规范：[.reference/astrbot-openapi.json](.reference/astrbot-openapi.json)（224 个端点，来自实例 `/api/v1/openapi.json`；Scalar 文档页 `https://docs.astrbot.app/scalar.html` 只是渲染壳）。查端点：

```bash
python3 -c "import json; spec=json.load(open('.reference/astrbot-openapi.json')); print('\n'.join(spec['paths']))"
```
