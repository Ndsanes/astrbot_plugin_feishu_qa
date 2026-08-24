# astrbot_plugin_feishu_qa

飞书 Q&A 文档驱动的 QQ 群领域问答机器人。**高置信问题零 LLM 调用**,确定性检索直答原文+截图;仅在必要时把检索片段交给主 Agent 受限作答。

## 架构

```
飞书 wiki ──lark_cli 平台网关──▶ GatewayClient(含 auth 健康监控)
                            │
                       QaCorpus(条目↔截图稳定绑定)
                            │
              ┌─────────────┴────────────┐
        确定性检索(Tier 0)         AstrBot 主 Agent
              │                         │
    高置信→直答+合并转发图片      低置信→search_feishu_qa 工具受限作答
```

飞书访问(文档抓取/图片下载/授权/写回)全部转发给 `lark_cli` 平台适配器
(astrbot_plugin_lark_cli_platform)暴露的网关对象;本插件不自管 lark-cli、
不持有凭据。网关未就绪时自动降级:本地语料继续服务,飞书拉取/授权暂不可用。

## 安装

1. 将本目录放入 AstrBot `data/plugins/`
2. 部署并启用 `lark_cli` 平台适配器(astrbot_plugin_lark_cli_platform),完成登录
3. WebUI 配置:`WIKI_URL` + `ENABLED_GROUPS`(留空不启用任何群)

## 使用

| 指令 | 说明 | 权限 |
|---|---|---|
| `/问 <问题>` | 确定性检索直答(0 LLM) | 白名单群全员 |
| `@机器人 <问题>` | 高置信直答;否则主 Agent 调 search_feishu_qa | 白名单群全员 |
| `/qa_status` | revision / 条目数 / 图片数 / 登录态 | 管理员 |
| `/qa_sync` | 手动同步飞书文档 | 管理员 |
| `/qa_reload` | 重载本地语料 | 管理员 |
| `/learn` (`ok`/`no`) | 从最近群聊提取候选 QA,确认后入库待写 | 管理员 |

## 数据

持久化于 AstrBot `data/plugin_data/astrbot_plugin_feishu_qa/`:
`corpus.json` 快照(原子替换)、`images/` 截图、`pending_learn.json` 待写候选。

## 设计要点

- 同步失败 ≠ 问答失效:快照损坏/同步异常时旧语料继续服务
- 图片以 file_token 下载落盘后本地发送(飞书临时 URL 不可直接给 QQ)
- QA 条目 ID 取自飞书块 ID,跨 revision 稳定
- 登录态过期:后台定时检查,提醒管理员 `lark-cli auth login` 重登
- 动态检索内容走 `extra_user_content_parts`,不污染 system prompt 缓存
