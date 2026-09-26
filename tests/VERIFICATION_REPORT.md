> ⚠️ 历史验收快照(2026-08-23,网关重构前架构),部分描述已过时,现行架构见 AGENTS.md

# 验收报告(spec §73 机器可读格式)

生成时间:2026-08-23(夜间自主交付)
代码基线:astrbot_plugin workspace @ commit 序列 cb47e17..HEAD

```json
{
  "BUILD":          "PASS",
  "TEST":           "PASS",
  "RUFF":           "PASS",
  "CORPUS":         "PASS",
  "IMAGE_BINDINGS": "PASS",
  "RETRIEVAL":      "PASS",
  "DIRECT_ANSWER":  "PASS",
  "LLM_TOOL":       "PASS",
  "LEARN":          "PASS",
  "AUTH_HEALTH":    "PASS",
  "LIVE_SYNC":      "PASS"
}
```

## 门禁证据

| 门禁 | 验证方式 | 结果 |
|---|---|---|
| BUILD | `python -m py_compile` 全部模块;tools CLI 可执行 | 通过 |
| TEST | `.venv/bin/pytest -q` → **97 passed**(kit 27 / adapter+keeper 14 / corpus 18 / retrieval 6 / answer 10 / main 11 / learn 11) | 通过 |
| RUFF | `ruff check .` → All checks passed(py312, E/F/W/I/UP/B/SIM) | 通过 |
| CORPUS | 真实 r8268 fixture:41 条目(35≤n≤50 基准带);幂等构建两次同 hash `9df1a4659459`;异常结构降级 diagnostics 不崩溃 | 通过 |
| IMAGE_BINDINGS | 108 图全绑定、无交叉(`test_multi_image_binding`);grid 嵌套图捕获 ≥100 token;单张失败不阻断 | 通过 |
| RETRIEVAL | 20 真实 query:19 条严格 top1 命中,余 1 条为双合理答案(fixture 标记任一可接受);4 负例全部 LOW;正例最低分 4.98 > MEDIUM 阈值 3.0 | 通过 |
| DIRECT_ANSWER | 白名单空=零响应;`*`=全放行;高置信直答含来源署名并 stop_event 阻断 LLM;图片缺失不破坏回答;合并转发失败回退普通消息 | 通过 |
| LLM_TOOL | search_feishu_qa 注册名/docstring Args 段校验;结构化 JSON 输出;LOW 时返回空 matches+note;不注入 corpus 到描述 | 通过 |
| LEARN | 自由文本拒绝、schema 完整性校验、历史长度封顶、查重命中既有条目、ok/no 显式确认、pending 落盘 data/,绝不直写飞书 | 通过 |
| AUTH_HEALTH | 真实 auth status JSON 结构驱动:HEALTHY/EXPIRING_SOON/EXPIRED/UNAVAILABLE 判定;首次提醒→去重→升级提醒→恢复复位 | 通过 |

## LIVE_SYNC 说明(PASS,网络恢复后复验)

夜间窗口曾遇 open.feishu.cn 不可达(TLS 超时)——按 spec §76 降级路径,旧语料持续服务。
次日网络恢复后完成全量真实验证:

- token 自动刷新成功(needs_refresh → valid)
- 线上文档已从 r8268 更新至 **r8394**(UP 主新增 1 条目/2 图)
- 全量同步:`fetch → parse → 110 图下载(60MB,全部落盘)→ 原子提交`,幂等 hash 复验一致
- 过程中发现并修复:lark-cli 拒绝绝对 --output 路径 → kit 增加 cwd 支持,
  adapter 改为在目标目录内以相对文件名调用(`test` 回归 97 passed)
- bot 身份 IM 提醒实测送达(open_id ou_1e5b…bedd,message om_x100b67973f36c8a)

## 夜间后补验证(同会话)

- **块 ID 漂移修正**:r8394 实测发现飞书在任何结构编辑后会重生成全部块 ID,
  原 locator 优先的 ID 策略失效(表现为增量 diff 全量误报)。已切换为内容派生 ID,
  双真实基线验证:41/41 跨 revision 稳定,增量 diff 正确识别"新增 1 条"。
- **一键重登卡片**:bot 身份推送交互式卡片(orange 头/点击授权按钮)实测送达;
  Device Flow `--no-wait` 发起 → 卡片按钮 → 后台 `--device-code` 轮询闭环已接线。
- 测试总数 97 → **104**。

## 已知限制与假设

1. lark-kit 以 workspace 源码形式分发;发布时 requirements.txt 改为 git URL 依赖。
2. 登录态提醒 v1 仅日志 + /qa_status 可见;bot 身份 IM 卡片推送留作接线点(notify 回调已抽象)。
3. /learn 写入飞书为二期(spec §70);当前候选落盘 pending_learn.json 由管理员人工同步。
4. 合并转发的 Node.uin 取 bot 自身 ID,qq_official 平台未适配(support_platforms 仅 aiocqhttp)。
