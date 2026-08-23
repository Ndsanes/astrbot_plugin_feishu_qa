
---

# lark-cli 平台化工程（v1.2，2026-08-24）

## 完成项
1. **astrbot_lark_kit v0.2.0**：`bootstrap.py`（自举下载，sha256 校验、版本感知幂等、防路径穿越、不支持平台显式拒绝）、`state.py`（resolve_state_home 统一登录态目录约定）、`events.py`（EventStream：`event consume im.message.receive_v1 --as bot` NDJSON 流、NormalizedLarkMessage 归一化、5 分钟 TTL 去重、bot 自消息过滤、bounded backoff 重启 1s→30s、GeneratorExit/Cancel 双路径子进程回收）、`messaging.py`（LarkMessenger.send_text/send_image 固定 bot 身份，oc_/ou_ 分派）。tag v0.2.0 已推送。
2. **feishu_qa 接入**：adapter.bundled_cli_path/vendor_dir 属性；initialize 时后台 ensure_bundled_cli 自举（terminate 可取消）；PATH 兜底保留。
3. **astrbot_plugin_lark_cli_platform**（新插件）：@register_platform_adapter("lark_cli")，bot 身份收发，无任何身份选择配置；group/p2p 映射、UMO 白名单（空=不限制）、send_by_session oc_/ou_ 分派、bootstrap_cli 配置。zip 形式已上传实例并安装成功。

## 验收结果
| 项 | 结果 |
|---|---|
| PHASE_A_KIT bootstrap tests/checksum/path-traversal/idempotence | PASS（10 用例） |
| PHASE_A_KIT version v0.2.0 / clean install smoke | PASS |
| PHASE_A_FEISHU_QA bundled_cli_path/bootstrap task/regression | PASS |
| PHASE_A_DEPLOY binary found/sync/config restored | zip 部署完成、配置恢复、failed 空；CLI found 日志行 NOT VERIFIED（日志 API 无 scope，待用户看一眼面板日志） |
| PHASE_B_KIT event normalization/message transport/lifecycle | PASS（12 用例含 cancel/orphan/dedup/self-message） |
| PHASE_B_PLATFORM 单测（映射/发送/元数据） | PASS（17 用例） |
| 平台插件实例安装 | PASS |
| PHASE_B_PLATFORM WebUI visible/group receive/private/send/image/send_by_session/reconnect/live bootstrap | NOT VERIFIED——需在 WebUI 创建 lark_cli 平台实例且飞书侧开启 im.message.receive_v1 事件订阅后人工验证 |

## 测试数量
kit 55+10=65？实际：astrbot_lark_kit 65、astrbot_plugin_lark_cli_platform 17、astrbot_plugin_feishu_qa 94，全套 **176 passed**（pytest 输出为准），ruff 全绿。

## 版本与部署来源
- astrbot_lark_kit v0.2.0（GitHub tag）
- feishu_qa v0.2.0（zip+vendor 部署；requirements 钉 kit @v0.2.0）
- astrbot_plugin_lark_cli_platform v0.1.0（zip+vendor+自带 vendored kit 副本部署；优先 vendored 导入，规避 site-packages 旧版遮蔽）

## 已知限制 / NOT VERIFIED
1. 实例 site-packages 存在 pip 安装的 astrbot_lark_kit v0.2.0（AstrBot 按 requirements 自动装），该版本不含 events/messaging——新插件已用 vendored-first 规避；未来若发 kit v0.3.0 应同步升 requirements。
2. 真实收发/图片/私聊/自环/重连实测：NOT VERIFIED，依赖用户在 WebUI 创建平台实例并在飞书开发者后台为 app(cli_aae92eb59cf85cd8) 开启 im.message.receive_v1 长连接事件。
3. bili_verify 生产代码历史 lint 欠账未清理。

## 下一步人工操作
1. WebUI → 机器人 → 新增平台 → 选择 lark_cli → 启用（enabled_chats 留空=不限制）。
2. 在机器人所在的飞书群里发一条消息，观察是否收到回复；再验证主动发送/图片。
3. 补 feishu_qa 的 ENABLED_GROUPS / ADMIN_USERS 配置（重装后仍为空）。
4. 清理草稿文档中的验证条目。
