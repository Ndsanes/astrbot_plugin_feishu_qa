
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

## 平台化工程运行时状态(截至 05:35)

已解决并部署:
- kit v0.2.0+ events/messaging/bootstrap/state 全部实现并有测试(全套 176 passed)
- astrbot_plugin_lark_cli_platform 已在实例安装(含 vendor 二进制与 vendored kit)
- 修复链:Platform 签名(2参基类+manager三参调用)、PLUGIN_DIR 错位、
  noexec 数据卷(/tmp 副本执行)、解压丢执行位(chmod 自愈)、
  stderr 诊断(消费进程退出码与错误体已进日志)

当前唯一阻塞:实例上事件消费进程 exit code=5,
stderr="did not become ready within 3s"(事件总线守护进程 3 秒就绪超时);
切回共享登录态目录时为 not_configured(目录指向需保留 lark_cli_home 配置项)。

下一步排查建议(人工,需容器内 shell):
1. docker exec 进容器手动跑:
   HOME=/AstrBot/data/plugin_data/astrbot_plugin_feishu_qa/lark_cli_home \
   /AstrBot/data/plugins/astrbot_plugin_lark_cli_platform/vendor/lark-cli/linux-amd64/lark-cli \
   event consume im.message.receive_v1 --as bot
   观察是偶发 3s 超时(重试即可恢复)还是稳定失败。
2. 若稳定失败:lark-cli event status 看守护进程状态;
   确认开发者后台该应用(cli_a728800c9f789013)的"长连接"事件订阅已保存成功。
3. 就绪超时可尝试预热:先跑一次 event status 让守护进程常驻,再启动平台。
4. 平台实例配置中 lark_cli_home 必须指向共享目录
   /AstrBot/data/plugin_data/astrbot_plugin_feishu_qa/lark_cli_home
   (该键虽已从默认模板移除,仍被兼容读取;或后续把共享逻辑写死进适配器)。

NOT VERIFIED:真实收消息/发消息/图片/私聊/自环/重连——全部依赖上述阻塞解除。

## 阻塞已解除（v1.3，2026-08-24 12:25）

### 补丁（12:37，v0.1.2）
- 事件订阅开通后首条真实消息已进入适配器，但 `meta()` 崩在
  `PlatformMetadata` 缺少 `id` 参数（AstrBot 4.27+ 签名变更）。已修复并部署
  v0.1.2，consumer pid=20204 运行中。待群内再次发消息验证完整收发。

### 补丁（12:53，kit v0.2.2）
- 发送侧报 not_configured(退出码 3):`run_lark_cli` 的 env 参数只用于二进制
  解析,子进程环境走 extra_env,messaging 未传导致 HOME 丢失。已在
  LarkMessenger._send 将 env 同时以 extra_env 注入(kit v0.2.2),附回归测试。
  核心白名单已加入 lark_cli:FriendMessage:oc_23084736ab8557c98dea79d0c0e23846。
  consumer pid=20422 运行中;待再次发消息验证回复送达。

### 补丁（13:03，v0.1.3）
- 群聊消息不回:正文带"@插件Bot "前缀导致命令解析失败。convert_message 现在
  对群聊剥离开头 @提及(私聊不动)。核心白名单已加群会话
  lark_cli:GroupMessage:oc_5ffa089f003c61a817e845dfcf8bf8d1。
  consumer pid=20572 运行中;待群内再测。

**exit=5 根因**：lark-cli 事件总线在 `<HOME>/.lark-cli/events/<appId>/bus.sock` 建
Unix domain socket，Linux 内核限制 AF_UNIX 路径 ≤108 字节；实例数据卷路径过深
（feishu_qa 共享目录形态 111 字符、平台插件自身目录 119 字符），bind 必然失败，
守护进程无法就绪。与登录态指向哪个插件目录无关，升级 lark-cli 1.0.89 也无修复。

**修复链（kit v0.2.1 + 平台插件 v0.1.1，均已部署实例）**：
1. `ensure_short_home`：HOME 路径过深时给子进程用 /tmp 定名 symlink 别名
   （bind 只查传入字符串长度，不解析 symlink），物理数据仍在自己 data 目录。
2. `ensure_bot_credentials`：bot 凭据从平台实例配置的 app_id/app_secret 播种为
   file 型 secret 源（0600），幂等；TAT 由 lark-cli 自管。不跨插件共享登录态。
3. EventStream 子进程持 stdin PIPE——lark-cli consume 把 stdin EOF 当退出信号，
   守护进程环境必须保持 stdin 打开；_terminate 先关 stdin 走优雅清理。

**实例验证**：12:25:30 起 consumer pid=20031 持续运行无退出（此前每 ~1s 必死）。
本地全套 188 passed / ruff 全绿。

剩余人工事项：
1. 飞书开发者后台为 cli_a728800c9f789013 开通 im.message.receive_v1 长连接事件订阅
   （日志中 console precheck 报 access denied 即此）。
2. 真实收发/图片/私聊/自环/重连实测仍 NOT VERIFIED。
3. feishu_qa 的 requirements 仍钉 kit v0.2.0（其未用 events/messaging，暂无碍）；
   下次动 feishu_qa 时升到 v0.2.1。

## 飞书网关收口工程（2026-08-24 下午启动）

**架构决策（用户两轮纠偏后定型）**：认证、登录态、TAT 刷新、限速全部收口在
astrbot_plugin_lark_cli_platform；其他插件**不得**读它的 data 目录、解析
.lark-cli/config.json 或自持 app_id/app_secret，必须走 AstrBot 正式插件间交互：
`context.platform_manager.get_insts()` 找 `meta().name == "lark_cli"` 的适配器实例，
调用其 `gateway` 属性（LarkGateway，适配器 run() 完成后非 None）。
曾否决的方案：① 各插件指到共享登录态目录（"去它目录搞事"）；② 读回凭据分发给
SDK 直连（"非 platform 插件都不管认证"）。

**契约**（'/Users/ndsans/.omp/agent/sessions/-Documents-code-astrbot_plugin/2026-08-24T02-51-28-777Z_01a031ae-1a89-74db-8535-b1b5df4e0b98/local/lark-gateway-contract.md）：send_text/send_image/api'(原始透传)/
fetch_doc/append_doc/download_media/auth_status/auth_login_start/auth_login_finish；
错误 LarkGatewayError；消费方 None/异常一律降级不崩溃。

**分工状态**：
- 平台插件 v0.2.0（主代理）：gateway.py + 8 测试，26 passed / ruff 绿。已完成。
- feishu_qa v0.3.0（FeishuQaGateway 代理）：删自管 lark-cli 全部能力，传输层换网关。
- bili_verify v0.1.0（BiliVerifyGateway 代理）：feishu_client 换网关 api()，
  删 lark-oapi 依赖与凭据必填。
- 待办：全套测试门禁 → 三插件部署实例（平台插件 zip 重打包需含 gateway.py）→ 验证。

注意：feishu_qa 改造后 wiki 文档读取走网关 user 身份 = 共享适配器的用户授权；
首次需要管理员对 cli_a728800c9f789013 完成一次设备授权（原 feishu_qa 自己的
授权态不再使用）。

### 网关收口完成（14:10 部署）
- 实例版本：lark_cli_platform v0.2.0（gateway.py 就绪日志确认）/ feishu_qa v0.3.0 /
  bili_verify v0.1.0；failed 列表空，consumer pid=348。
- 本地基线：214 passed / ruff 全绿（kit 45→45+、feishu_qa 107、bili_verify 53、
  lark_cli_platform 26）。
- 三仓库已推 GitHub 并打 tag：lark_cli_platform@v0.2.0、feishu_qa@v0.3.0、
  bili_verify@v0.1.0。
- 待人工验证：①群聊/私聊收发回归；②feishu_qa /qa_sync 首次运行需管理员对
  cli_a728800c9f789013 做一次设备授权（旧 feishu_qa 自有授权态已弃用，
  新链路走网关 = 适配器登录态目录）；③bili_verify 表格写入实测。

### feishu_qa 登录态管理彻底移除（v0.3.1，14:30 部署）
- 用户定调:qa 里不应有任何登录态管理代码,认证完全由 platform 适配器自管。
- 删除面:adapter/auth.py(AuthKeeper)、/qa_auth_login、_auth_loop/_send_reauth_card、
  gateway 客户端 auth_status/auth_login_* 包装、ADMIN_OPEN_ID/AUTH_CHECK_HOURS/
  AUTH_WARNING_HOURS 配置、tests/test_reauth_card.py、test_adapter 的 auth 用例、
  test_deployment_layout.py(布局门禁随 kit 依赖一起退役)。
- 连带:feishu_qa 现为零第三方依赖(requirements 清空,打包脚本不再 vendor kit,
  build_qa_corpus 去 kit path 注入)。基线 194 passed / ruff 绿。
- 已部署实例 v0.3.1(failed 空),GitHub tag v0.3.1。
- 注意:/qa_sync 首次运行仍需 platform 侧完成一次用户设备授权——但入口应在
  lark_cli 平台适配器实现(qa 不再提供授权命令);平台适配器当前尚无授权命令,
  属遗留缺口。

### 平台侧通知与认证闭环（v0.3.0，14:44 部署）
- 配置新增(平台实例扁平键):notify_umos(UMO 列表,取末段 oc_/ou_ 推卡片;
  已配管理员 p2p)、auth_check_hours(默认 6)、auth_warning_hours(默认 48)。
- kit v0.2.3:LarkMessenger.send_card(interactive)。gateway 增 send_card。
- 适配器:begin_reauth(设备授权+红卡带按钮链接)/_auth_loop 周期健康检查
  (状态恶化自动推卡发起重授权,恢复推绿卡);插件 Star 提供 /lark_auth_login。
- 事故记录:main.py 曾被编辑损坏(方法体成 "...",platform_adapter 导入丢失)
  导致 "Platform adapter not found",重写后恢复;consumer pid=829 正常。
- 基线 200 passed / ruff 绿。kit@v0.2.3、platform@v0.3.0 已推 GitHub tag。

### WebUI 表单 + 白名单归位（v0.3.1，15:06 部署）
- register_platform_adapter 增 config_metadata(参照 line 适配器格式):WebUI 现在
  渲染 app_id/app_secret/notify_umos/auth_check_hours/auth_warning_hours 表单项。
- 按用户定调移除 enabled_chats:白名单由 AstrBot 核心会话设置负责,平台适配器
  不做过滤(default_config_tmpl/_chat_enabled/相关测试全删)。
- tests/stubs 的 register_platform_adapter 对齐真实签名(带 config_metadata 等)。
- 基线 198 passed / ruff 绿;GitHub tag v0.3.1;consumer pid=1113 正常。

### 凭据单一来源语义（kit v0.2.4 / 平台 v0.3.2，15:16 部署）
- 定调(用户):app_id/app_secret 为平台配置必填项,是凭据唯一权威来源。
- ensure_bot_credentials 改为同步语义:与目录存量比对,appId/secret 变化即覆盖;
  一致则不动(用户令牌/TAT 不受影响)。适配器启动时配置缺失记 ERROR(不再有
  "未配置但沿用存量"的模糊 WARN)。
- config_metadata 两项标"必填"。基线 198 passed / ruff 绿。双 tag 已推 GitHub。

### user_auth_enabled 开关（平台 v0.3.3，15:28 部署）
- 新增 bool 配置 user_auth_enabled(默认 true):控制 _auth_loop 周期维护是否启动;
  关闭后仅 /lark_auth_login 手动授权可用。auth_check_hours/auth_warning_hours
  仅在开关开启时生效。consumer pid=1425 正常;基线 198 passed / ruff 绿;tag 已推。

### 流式输出修复（平台 v0.3.4，15:38 部署）
- 现象:命令回复(/sid)正常,但 LLM 生成的回复(普通聊天)不发送——respond.stage
  走 "Applying streaming output (lark_cli)",而适配器 send() 是整条 CLI 投递。
- 根因:register_platform_adapter 的 support_streaming_message 默认 True,核心
  按"支持流式"策略分片下发,适配器无法处理分片。
- 修复:装饰器显式 support_streaming_message=False,核心缓冲整段后一次性 send。
- 教训:新平台适配器若 send() 不支持逐字流式,必须在注册时声明 False。
- consumer pid=1583 正常;tag v0.3.4 已推。

### send_streaming 真修复（平台 v0.3.5，15:48 部署）
- v0.3.4 的 support_streaming_message=False 不足以改变核心行为:LLM 结果仍是
  STREAMING_RESULT,respond.stage 直接调 event.send_streaming()——基类实现是
  空操作(astr_message_event.py:280),回复被静默丢弃。
- 真修复:LarkCliPlatformEvent 覆写 send_streaming——消费 async_stream、聚合
  Plain 全文、经正常 send() 整条下发;空聚合跳过。31 passed;tag v0.3.5 已推。

### 重载卡死修复（kit v0.2.5 / 平台 v0.3.6，15:55 部署）
- 根因:events._terminate 里先 `await proc.stdin.wait_closed()` 再 close()——
  stdin 为保活一直握着,该 await 永不完成 → 平台 terminate 挂死(即此前多次
  PATCH enabled 接口超时的真因)。
- 修复:先 close() 再限时(3s)wait_closed。附回归测试(fake stdin 永挂,
  断言 _terminate 5s 内返回)。
- 基线 201 passed / ruff 绿;kit@v0.2.5、platform@v0.3.6 已推 tag。

## 双身份收口与网关通配层（kit v0.2.6 / 平台 v0.4.0，2026-08-24 16:40 部署）

**动因**：lark-cli 每个方法声明 `access_tokens`（实测 231 个 typed 方法：
42 仅 user、8 仅 bot、181 双身份；另有 im feed/flag 等 user-only shortcut），
而 CLI `defaultAs=auto` 在双身份并存时把未钉身份的调用静默解析成 user——
管理员一旦完成设备授权，`gateway.api()` 的全部透传就会悄悄换身份。
普查数据：tmp/lark_cli_api_survey.json。

**改动**：
- kit v0.2.6：`apply_identity(args, identity)` + run_lark_cli/run_lark_cli_json
  新增 identity kwarg（None=不注入）。已推 GitHub tag v0.2.6。
- 平台 v0.4.0：`api()` 新增 `as_identity="bot"`（默认钉 bot，行为确定性）；
  新增 **`call(cli_args, *, as_identity="bot", timeout_s, cwd)` 通配层**——任意一次性
  lark-cli 命令直接透传，不逐能力包装；守卫禁代理 auth/config/profile/update/
  doctor/event/help/__complete 与自带 --as/--profile。消息面仍固定 bot 不变。
  已推 GitHub tag v0.4.0 并部署实例（zip 卸载重装通道）。

**部署通道结论**：`POST /plugins/install/upload` 对已存在目录报
"目录已存在"（即此前"插件操作失败"的真因）；正确姿势 = 先 DELETE
`/plugins/{id}` 再上传。实例当前无 user 授权态，卸载零损失（bot 凭据由配置自动播种）。
注意 `POST /plugins/{id}/update`(repo) 会用仓库内容覆盖插件目录——GitHub 上的
平台仓库不含 vendor/lark-cli 与 vendored kit，走该通道会丢二进制且 kit 回退到
site-packages v0.2.0(缺 apply_identity 必炸)。zip 通道是唯一安全发版方式，
除非把 vendor 提交进 GitHub 仓库或先升 site-packages kit。

**验证**：本地 221 passed / ruff 绿；实例 v0.4.0 加载、适配器 16:38:02 重建、
consumer pid=2387、vendor 二进制就绪、"飞书网关就绪"日志确认。
NOT VERIFIED：真实群消息收发回归（依赖飞书侧发消息）、user 身份实际调用
（需管理员先 /lark_auth_login 设备授权——授权后 api()/call() 默认仍 bot，
需要 user 身份的调用方显式传 as_identity="user"）。

### 登录态自动维护三重修复（平台 v0.4.1，17:26 部署并实测闭环）

用户指出"配置好开启登录态维护后应自动检查并推送到 notify_umos,无需手动授权"。
排查发现自动维护从未真正可用,三个 bug 叠加:
1. `auth_login_start` 默认 `--domain feishu` 不是合法 CLI 域(实测 exit 2 unknown
   domain)→ 授权从未发起成功;该异常还直接杀死 _auth_loop 任务。
2. `health_of()` 被喂原始 dict(缺 auth_status_from_dict 转换)必然 AttributeError
   → 即使域合法也会在判定环节静默失败。
3. `_auth_loop` 先睡满 auth_check_hours 才首查 + 循环体无异常兜底。

**v0.4.1 修复**:授权域可配置(`auth_login_domains`,默认 docs,drive,wiki);
补类型转换;启动即首查;循环永不因单轮异常退出;持续非健康每约 24h 重发提醒卡。
**连带发现并修复**:gateway 全部 CLI 调用(api/call/fetch_doc/append_doc/
download_media/auth_*)此前不传 bin_path、依赖环境 PATH——容器内无全局 lark-cli,
即 gateway 自 v0.2.0 起在实例上从未工作过(消息面走 messenger 直连才幸存)。
现统一 `bin_path=self._binary()`。测试侧:run_loop 套件显式 user_auth_enabled=False,
消除单测拉真实 CLI 的竞态(曾致全量套件随机挂死)。

**实例闭环实测(17:26)**:重建后首查即检测 user missing → 发起 docs,drive,wiki
设备授权 → 红卡推送 notify_umos → 用户点链接完成授权(24s)→ 后台轮询成功 →
"用户重新授权完成"日志 + 绿卡。此后每 6h 自动巡检。GitHub tag v0.4.1 已推。

## 全工作区代码清理轮（2026-08-24 晚,kit v0.2.7 / qa v0.3.2 / platform v0.4.2 / bili v0.1.1）

四包并行审计(4 reviewer agent)+四包并行清理(4 task agent)。基线变化:根套件
225→223(kit 删 test_dedup_repro、qa 删 test_miss_reply_text 各 1),bili 53→49
(删 4 个 admit_message 专属测试)。全套 223+49 全绿,ruff 干净。

- **kit**:修 P1(__init__ __all__ 声明 PlatformIdentity/resolve_platform_instance
  却无 import,包根导入即 ImportError;补 import);删 parse_envelope_bytes、
  tests/test_dedup_repro.py(一次性调试脚本)、重复 _drain 定义;.tmp_home/ 残留
  目录删除并入 gitignore。platforms.py 整模块仍零生产消费方但保留(已修复导出)。
- **feishu_qa**:删幽灵配置键 ENABLE_LLM_TOOL(schema 声明但从不读取)、send_text
  空桩、format_miss_reply(与 main 内联文案双真相,保留内联版)、miss_text 死字段、
  source_root 死参数、SnapshotStore.exists()、过期 fixture qa_r8268.json/md;
  VERIFICATION_REPORT.md 标注历史快照;README 删 kit 安装步骤。
- **bili_verify**(用户确认:表格对 bot 身份开通权限,gateway.api bot 链路一行未动):
  platform_port.py 748→38 行(仅留 JoinRequest DTO);删 _platform_port 占位、
  admit_message/mark_left/find/from_plugin_config、storage 三个零调用写函数、
  失效 _uid_pattern 正则、suppress-pass 空操作与三分支同路逻辑合并;
  FEISHU_QQ_FIELD 幽灵键清除;PENDING_CHECK_INTERVAL 双轨默认统一 3800
  (唯一有意行为修正);plan.md 整删(公开 repo 含暴露的 FEISHU_APP_TOKEN,
  git 历史仍在,**该 token 建议轮换/收紧表权限**)。
- **platform**:README/requirements 清 bootstrap_cli 残留 + if True: 脚手架展平;
  create_task 强引用 self._reauth_task 防 GC + begin_reauth 失败原因落日志 +
  在途防重入;api() 委托 call()(守卫语义等价,test_gateway api 用例零修改通过);
  fetch_doc/append_doc 改走 as_identity="user" 统一身份通道;send_streaming 补
  super() 记账(核对真实基类只记账不消费生成器);测试构造参数清 bootstrap_cli。
- AGENTS.md 全面同步到网关时代(概述/架构图/目录/导入约定/基线数字)。
- **plan.md 凭据风险已评估关闭(用户确认)**:该多维表格权限只对 BOT 身份开放,
  app_token 暴露无实际改动风险,不轮换;git 历史中的 plan.md 不再追讨。
- dist/ 产物清理:两插件共删 9 个旧版/vendor zip(约 175MB),各保留当前版
  (qa v0.3.2 / platform v0.4.2,后者兼任未来打包的 lark-cli 二进制供体)。
