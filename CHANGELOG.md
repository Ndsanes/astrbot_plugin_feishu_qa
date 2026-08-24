# 更新日志

## v0.3.0 (2026-08-24)

### 变更

- **lark-cli 收口为飞书网关**:删除插件自管的 lark-cli 子进程调用、vendored
  二进制自举与 `FEISHU_APP_ID`/`FEISHU_APP_SECRET` 播种逻辑;文档抓取、图片
  下载、设备授权(start/finish)、登录态查询、`/learn` 写回全部改走
  `lark_cli` 平台适配器(astrbot_plugin_lark_cli_platform)的网关公开方法。
  认证、登录态、TAT 刷新与限速由网关统一负责。
- 新增 `adapter/gateway.py`:`GatewayClient` 薄客户端,构造时注入网关解析器
  (每次操作前重新解析,适配器晚启动自动恢复);网关未就绪时 WARNING 降级,
  本地语料继续服务,不崩溃。
- 授权提醒改为经网关 `send_text` 以 bot 身份私聊推送授权链接(原交互式卡片
  通道随 lark-cli 直连一并移除)。
- 依赖 `astrbot-lark-kit` 升至 v0.2.2(仅作纯逻辑库:auth 状态解析/健康判定,
  无子进程);打包脚本不再携带 lark-cli 二进制。
- 已知限制:网关 `fetch_doc` 只返回正文文本,同步的 revision 以 -1 占位,
  "未变化"判定退化为内容 diff(功能等价)。
- 依赖 `astrbot-lark-kit` 已发版为独立包(v0.1.0),正式安装走 git URL;开发模式仍以工作区源码优先,vendored 副本兜底。

## v0.2.0 (2026-08-24)

### 新增

- **/learn 写回飞书(二期)**:管理员 `/learn ok` 确认后,自动把候选 QA 以完整块追加到配置的**副本文档**(`LEARNING_WRITEBACK_DOC_URL`;留空则保持旧行为仅落盘)。写回前双重查重(标题归一化比对 + 已同步记录键),已存在则跳过;单次完整提交,失败时候选保留并提示人工处理。
- **合并转发能力探测**:qq_official 明确走普通多图消息,其余平台尝试 OneBot 合并转发、构建失败自动回退(原已知限制 #4 收口)。

### 变更

- 依赖 `astrbot-lark-kit` 已发版为独立包(v0.1.0),正式安装走 git URL;开发模式仍以工作区源码优先,vendored 副本兜底。
- 版本基线:测试 136 项全绿(含写回模块、离线 diff 门禁与检索回归)。

## v0.1.0

- 首个公开版本:确定性检索直答 + 截图、`/qa_sync` 同步、`/learn` 候选工作流(落盘待人工同步)、登录态健康监控与一键重登卡片。
