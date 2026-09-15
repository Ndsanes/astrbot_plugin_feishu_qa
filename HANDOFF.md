
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
- **bili v0.1.2(用户指出弃用项遗留)**:移除"已弃用"配置键 FEISHU_APP_ID/
  FEISHU_APP_SECRET 全链路残留(schema 两块/FeishuCfg 字段/from_dict/get/as_dict
  条目/README 措辞),CHANGELOG 记录;qa 10 键全活、platform 无插件 schema 已核。
  实例经 GitHub update 通道升到 v0.1.2,重载无错。
- **qa v0.3.3(用户指出 ENABLED_GROUPS 非 UMO 格式)**:白名单双形态——裸群 ID
  (原行为不变)+ 完整 UMO(按事件 unified_msg_origin 精确匹配);管理员拒绝诊断
  现附当前会话 UMO 供直接复制。测试桩统一补 unified_msg_origin 属性(修 platform
  桩先载 sys.modules 污染 qa 用例的形状漂移),根套件基线 223→226。已部署实例。
- **qa v0.4.0(规格书落地:QA 图片按需发送)**:Agent 命中带图 QA 后经
  `qa_entry_images(entry_id)` 从本地已同步图片直发截图——有效条目返回
  MessageEventResult 走核心 tool_direct_result 通道(直发并终止 Agent),
  无效条目回错误文本给 LLM 继续;零网络零飞书调用。**连带修复**
  search_feishu_qa 返回值契约(旧 yield plain_result 会直发原始 JSON 并
  终止 Agent,线上从未调用过故未暴露)。KB 导出脚本 tools/export_kb_chunks.py
  (breadcrumb+截图标记,文档字段名是 file_name、chunks 是字符串列表);
  标记语料已导入实例空库"音频软件全家桶 FAQ"(41 chunks),原话检索 Top-1
  相关度 0.981 且标记 entry_id 与线上语料全对齐。基线 226→235。
  待用户实测:真实群里 @bot 问 FAQ 问题,观察 astr_kb_search 命中 FAQ 库 +
  Agent 是否调用 qa_entry_images 直发截图;kb_names 加"音频软件全家桶 FAQ"
  需 WebUI 勾选(PUT system-config 会回滚该键,不能代改)。
- **qa v0.4.1(FAQ 出处引用规范)**:`on_llm_request` 钩子注入静态提示词——
  回答引用「全家桶FAQ」条目时末尾注明 📄 出处(文档名+章节路径),并约束
  仅在用户索要截图/步骤强依赖配图时才调 qa_entry_images(23:12 实测模型
  拿到标记但未主动发图,靠此规范引导)。基线 235→238。
- **qa v0.5.0(原文整体投递)**:新增 `qa_send_answer(entry_id)`——Agent 命中
  FAQ 条目后按文档原文(标题+正文+署名)连同截图经 event.send 整体直发
  (复用 Tier 0 组装/发送链路),返回回执防止模型复述;Agent 循环不终止。
  KB 标记瘦身为 `[配图 qa_xxx]`(旧长标记文档已删重导);引用规范升级为
  四条规则(命中即整体投递/补图用 qa_entry_images/注明出处/禁止臆测图N)。
  基线 238→242。23:24 实测确认:kb_names=2 生效、FAQ 库四席压过手册、
  钩子注入成功(prompt_tokens 1004→1165)但小模型未主动发图——本版引导修正。
- **platform v0.4.6 + qa 网关契约修复(图片链路三重根因)**:①qa 客户端
  Path(网关返回) 未处理 None 契约→TypeError;②lark-cli 对 docx 内嵌图片
  走 drive 直连必 403,正确命令是 `docs +media-preview --token <tok>`;
  ③CLI 以 cwd 相对路径回报产物且对已存在输出退出码 2(--overwrite 不被
  部分 子命令/版本 生效)——网关现按 dest_dir 归位路径并预清理残留。
  连带修 sync_once 缺图自愈(unchanged 快速路径曾永久跳过补下载)。
  终局:image_failures 110→0。诊断利器:平台侧失败 WARN 含完整 CLI JSON。
- **qa v0.7.0(章节直达链接)**:qa_send_answer 投递形态改为飞书文档锚点
  深链列表(官方格式 文档URL#block_id,locator 语料 41/41 齐备);引用规范
  双轨制——FAQ 命中转链接、手册类来源 LLM 正常作答。注意块 ID 随文档
  结构编辑重生成,链接新鲜度=最近一次同步(定时同步会自动跟进)。
- **qa v0.7.1(markdown 超链接)**:qq_official 平台的链接列表改用
  `[标题](文档URL#block_id)`——官方适配器默认原生 markdown(msg_type=2)
  投递可点击;其他平台维持标题行+裸链接。桩补 get_platform_name。
  根套件基线 245→246。
- **qa v0.7.2(命中预判注入)**:02:22 C2C 实测双主题混合提问时模型跳过
  qa_send_answer 自行转述——纯提示词引导对小模型不够硬。钩子在 LLM 请求
  前跑本地词法检索,MEDIUM+ 命中把具体 entry_ids 指令注入 req.prompt 尾部
  (缓存安全通道:系统提示词恒定逐字节不变,AGENTS.md 检索不进系统提示词
  约束得以遵守);未命中零改动。基线 246→248。
  **v0.7.5 补充根因**:relinked=0 的更深原因——lark-cli docs +fetch 默认
  `--detail simple` 导出 XML 不含块 ID,source_locator 自始为空(测试夹具
  是人工带 id 导出,掩盖了这一点)。platform fetch_doc 加 detail 透传,
  qa 同步改 with-ids 后 relinked=42 全量填充(platform v0.4.7/qa v0.7.5)。
  部署后首轮同步 relinked=0:线上 locator 与实时抓取一致,块 ID 未过期;
  "点了不跳转"的剩余变量锁定在 QQ→飞书客户端传递链路(疑似移动端
  丢 #fragment),待用户桌面浏览器 A/B 鉴别。
- **qa v0.7.4(回退裸链接)**:用户浏览器实测机器人 markdown 超链接落地后
  地址栏无 #fragment——QQ 官方原生 markdown 渲染剥 href 锚点(裸文本 URL
  则走客户端识别路径完整保留,已实测)。所有平台统一"标题行 + 👉 裸链接",
  回归测试断言输出禁含 "]("。基线 248 不变。
- **qa v0.8.0(魔法链接 token 化 + 功能瘦身)**:参考 Modu ADR-011——命中
  预判改发 `[[qa:N]]` 短 token(映射存事件级 extra),qa_send_answer 展开
  还原(真实 ID 双轨放行);下线 qa_entry_images/search_feishu_qa 工具;
  KB chunk 去除 [配图] 标记并重导。注意:KB documents 列表主键是 doc_id,
  DELETE 打错键会静默无效。基线 248→237。
- **qa v0.8.1(短码引用定稿)**:chunk 尾部烧入 `[ref:短码]`(entry_id
  去 qa_ 前缀取前 5 位 hex,41 条零碰撞);模型从检索结果原样搬运短码
  调 qa_send_answer,_entry_refs 双键索引(短码+完整id)解析。
  钩子动态词法预判整段移除——发现即持有,词法漏召/幻觉 token 两类
  失败模式一并消失。qa v0.8.1 + platform v0.4.7 均已部署。
- **qa v0.8.2(自动附链定稿)**:11:45 实测模型再次跳过 qa_send_answer
  直接转述步骤——确认小模型合规性不可依赖后,改为确定性钩子:
  on_llm_tool_respond 解析 astr_kb_search 结果中的 [ref:短码],命中即由
  插件自动投递章节链接列表(上限3条/同轮去重),零模型依赖。
  引用规范同步改为自动化语义。qa v0.8.2 已部署。
  **v0.8.2 修正**:12:30 实测自动附链生效(三条链接与条目一一对应)但
  用了裸链接——此前"markdown 渲染剥 fragment"系 locator 为空时代的
  误判(链接里本无锚点可剥)。qq_official 恢复 markdown 超链接形态,
  新增 TestAutoFaqLinks 四用例。基线 237→240。
- **qa v0.8.3(形态统一)**:12:49 实测群聊 @ 命中仍走 Tier 0"原文全文
  +截图"旧形态(词法 HIGH 直答),与私聊链接形态不一致——Tier 0 投递
  统一改为章节直达链接列表(经 _build_link_list,平台感知 md/裸链接),
  /问 与群内 @ 同步生效;零 LLM 与 stop_event 阻断不变。基线 240。
- **qa v0.8.4(文案定稿)**:直达链接列表头/来源署名按用户定稿精简
  ("直接命中 N 条肖闻的解答" / " > Xiaowenn《有福同享全家桶Q&A汇总》")。

## 双故障排障轮（qa v0.8.5，2026-09-15）

用户反馈一次群聊里两个独立故障：先被"直接命中 1 条肖闻的解答"（内容不相干——
问混音台却答登录激活），随后改走知识库又连续两次 `astr_kb_search execution
timeout after 20 seconds`。根因彼此无关，各自修复。

### 故障 1：Tier 0 领域高频词误命中（代码修复）

`cakewalk sonar的混音台在哪` 打到 6.59 分直答【如何登录激活】——实例生产阈值
就是 6.5（非代码默认 9.0）。全篇文档都在 Cakewalk 语境，"cakewalk/sonar"两个
满语料高频词把**每条目一起抬高**却不区分任何条目（实测 idf 仅 0.14/0.26），
而真正携带意图的"混音台"在全语料零命中（idf 0.82）。于是硬答一条不相干的，
还把主 Agent 挡在门外（stop_event）。

**修复**：`retrieval/scorer.py` 引入**主题词支撑闸门**——按 IDF 识别查询中的
"主题词"（≥0.35，排除满语料领域词），条目若在 症状标签/标题/分类 三处都覆盖
不到任一主题词，分数 ×0.3 压回低分区交还主 Agent。门槛 0.35 取自 r8268/r8394
两份语料的稳定平台期（0.30~0.35 行为一致）。校准：20 真实 query 判定完全不变
（直答 17/20，正确条目被惩罚数 0/20），4 条负例零泄漏，误命中 6.59→1.98(LOW)。
新增 `TestTopicSupportGate` 四用例，其中阈值用**生产值 6.5** 回归（代码默认 9.0
掩盖过这个 bug，必须用线上真值测）。根套件 244 passed / ruff 绿。

### 故障 2：KB rerank 超时（实例配置修复，未改代码）

`tool_call_timeout=20`s，而 Moark Qwen3-Reranker-8B 实测 30–52s（与文档数无关，
1 条 doc 也要 36s）→ 每次检索必然超时。**同模型在 SiliconFlow 只要 0.3–0.7s**。
已在实例新建 provider `Siliconflow Qwen3-Reranker-8B`
（`https://api.siliconflow.cn/v1` + `/rerank`，复用 bge-m3 的 key），
绑定到「Cakewalk sonar」KB（原「音频软件全家桶 FAQ」本无 reranker）。
整链路 21.9s → 2.1s，且 top-1 正确命中 Console view 章节。

### 踩坑（重要）

1. **改 provider 配置会静默毒死 KB 的 rerank**：`PUT /api/v1/providers/by-id`
   重建 provider 实例（旧实例被 terminate → `client=None`），但 KBHelper 的
   vec_db 仍持旧引用 → 之后每次 retrieve 都 "Rerank 执行失败，已跳过重排序"，
   异常文本为空（正是 vllm_rerank_source 的 `assert self.client is not None`）。
   症状：排序静默退化为融合结果 + 检索突然快得反常（<1s）。**修复**：改完
   provider 必须对每个绑定它的 KB 发一次 `PUT /api/v1/knowledge-bases/{kb_id}`
   触发 KBHelper 重建。本轮即踩中并修复。
2. `PUT /api/v1/providers/by-id` 载荷必须带 `provider_id`，否则 400
   "Missing key: provider_id"。
3. `POST /api/v1/providers/{id}/test` 对 vllm_rerank 是 lazy 判定——Moark 明明
   能跑却报 `unavailable`，不能当健康判据。
4. 该 KB 的 FAQ 语料**确实没有**混音台条目（实测 "混音台" 检索 top-1 是轨道视图
   条目）——所以正确答案本来就该由 Agent 结合用户手册（Console view / Alt+2）
   给出，Tier 0 正确地选择了不抢答。

### 部署

zip 通道（DELETE → upload，`delete_config:false`/`delete_data:false` 保配置与
语料），实例 v0.8.5 / activated / failed 空 / 语料 45 entries 完好、配置
HIGH_CONFIDENCE_THRESHOLD=6.5 原样保留。dist 旧 zip 已清理。

### 追加:自动附链误发无关章节(qa v0.8.6,23:42 部署)

v0.8.5 上线后用户实测:问"Cakewalk 的混音台怎么打开",正文答案正确
(Console View / Alt+2,由 Agent 依手册给出),但**附链**发了三条完全无关的
FAQ:「连接MIDI设备」「Clip重叠」「搜索不到自带音源」。

**根因**:`auto_send_faq_links` 从 `astr_kb_search` 返回文本里按**出现顺序**
取前 3 个 `[ref:短码]` 就发。而 KB 检索的相关度是为**该次检索词**服务的
(模型用的是 "Cakewalk 混音台 怎么打开 控制台 快捷键"),靠前的 FAQ 只说明
它与 "Cakewalk" 同域,不代表答得上用户的问题。实测这三条按**用户原问题**
本地判定只有 0.49/0.63/1.12 分,全在 LOW 区——即 Tier 0 直答认定的"不合格"，
却被附链当作"答案在这里"承诺给用户。

**修复**(`retrieval/scorer.py` + `main.py`):`linkable_entries()` 用**用户原
问题**(`_strip_wake` 剥 @唤醒)本地判定,沿用与 Tier 0 同一套证据标准——
①本地 MEDIUM+;②头部(症状标签/标题/分类)覆盖查询主题词。不达标不发;
输出按相关度降序(不再随检索顺序浮动)。查询无主题词时闸门不生效(避免
"cakewalk"这类正常提问被一律拒绝)。

**校准**:20 真实 query 正确条目零误拦(0/20);全语料 124 组"用户会怎么问"
措辞自测,自身条目附着率 100%;事故现场三条目全部拦下。新增
`TestAutoFaqLinksRelevance` 六用例(含"传入顺序相反也应输出同序")。
根套件 249 passed / ruff 绿。已 zip 通道部署 v0.8.6,语料 45 条与配置
(阈值 6.5)完好。

**教训**:任何"面向用户的断言/承诺"(附链、直答、卡片)都必须以**用户原话**
为判定基准,不能复用为**中间检索词**计算的分数或顺序——两者服务对象不同。

### /learn 素材取用与转发聊天记录（qa v0.8.7，2026-09-16 01:06 部署）

**实测推翻了一个我先前的错误判断**：我曾断言"qq_official 不解析转发"
（证据是适配器里 `forward`/`Forward`/`Nodes` 出现次数为 0）。这是**测错了东西**——
我看的是 AstrBot 的转发*组件*类型，而官方 QQ 机器人是在**服务端**就把合并转发
展开成纯文本再下发，插件根本不需要转发组件。

- 协议事实：`message_type: 102 = 聊天记录`（103=引用、3=结构化卡片、101=并行消息），
  `content` 里是平台预渲染的转录：`[群聊的聊天记录]` + `=== 消息 N ===` +
  `[消息内容]` / `[发送者]`，嵌套引用另带 `[关联消息] → 第N条`。
  官方文档：`qq_docs/autogen/event/group_at_message_create.md`（iYeXin/qybot 镜像）。
- 线上实证：00:36 那条转发（UMO `default_1903893532:FriendMessage:...`）
  完整落在 `events.message_str` 并进入会话库（363 字符），模型推理里也写明
  "用户发了群聊记录"。即**无需开 `group_message_history_enable`、无需新增落库**。
- 关键设计点：转录是**转发者人工筛选**的，信噪比远高于"抓最近 50 条群聊"
  （群里绝大多数是闲聊，实测 30 条样本里 29 条非 @ 且多为 `妈妈！`/`🤔`/表情）。
  且群成员**不用加机器人好友**（官方机器人无法被加好友），把记录发群里
  再引用/`/learn` 即可触发——这是权限受限下唯一可行的素材入口。

**改动**：`/learn` 素材三档优先级 = 聊天记录转录 > 引用消息 > 平台历史（aiocqhttp）。
此前只走 `get_group_msg_history`，在 QQ 上直接回"当前平台不支持"，正是走不通的原因。
候选回复标注素材来源；三条都取不到时给可操作提示而非死胡同。

**顺带修复**：`build_learn_prompt` 遇单条超 4000 字素材会**整条丢弃**
（`if total+len(line) > MAX: break`）→ prompt 素材区为空 → 模型无从判断，
表现为静默判"没有新问答"。改为首尾保留式截断（问答分居转录两端，砍任一端
都会毁掉素材）+ 显式省略标注。

**教训（gitignore）**：`.gitignore` 的 `AstrBot/` 未锚定 + gitignore 大小写不敏感
→ 连带忽略各插件 `tests/stubs/astrbot/`。后果是**桩文件不在版本控制内**：
本轮一次 `cp` 覆盖把 lark_cli_platform 的桩打坏（3 个测试红），因无 git 历史
只能按真实 astrbot 4.28.1 签名重建。已修：规则锚定为 `/AstrBot/`，
并把 lark 桩包纳入版本控制。**改桩前先确认该文件是否被跟踪。**

### /learn ok 确认分支失效（qa v0.8.8，2026-09-16 01:25 部署）

**用户实测首跑即暴露**：`/learn`（引用聊天记录）**成功**产出候选并回复
"[素材来源: 引用消息] 发现可学习的新 QA..."，但紧接着的 `/learn ok` 回了
"没能取到可分析的素材"——确认链路断了。

**根因**：`WakingCheckStage` 在唤醒检查时**已把 `wake_prefix("/")` 剥掉**
（`waking_check/stage.py:130`），handler 收到的 `event.message_str` 是
`learn ok` 而不是 `/learn ok`；而 `_strip_command` 只硬编码认识 "问" 系列前缀，
对 `learn ok` 原样返回 → `text.lower() in ("ok","确认","yes")` 永不成立 →
确认/放弃两个分支都被跳过 → 掉进素材分析分支，又因这条消息没有引用/转录
而回"没能取到可分析的素材"。**缺陷特征**：文案具有误导性（像是素材问题，
实为参数解析问题）。

**修复**：`_strip_command(message_str, *command_names)` 显式传指令名
（`"learn"/"学习"`、`"问"/"qa"/"Q&A"`），并兼容仍带 `/` 的入口。
连带修正 `/问` 的别名形式（`qa xxx` 此前剥不干净）。

**验证方法（值得复用）**：写完回归测试后，用"临时把实现退化回修复前"跑一遍，
确认测试确实**会红**且报错文案与线上一致——否则测试可能只是记录了当前行为
而非真正守住缺陷。本轮 7 个新用例退化后 6 红，报错原文即线上那句回复。

**连带修复**：qq_official 上 `/learn` 每轮都试图 `event.bot.api.call_action`
（其 `bot` 是 `BotAPI`，无该方法）→ AttributeError 被兜住，行为正确但每轮刷
一条 `读取群历史失败` 日志；现按平台判定，不做注定失败的尝试。

### /学习 指令未注册别名（qa v0.8.9，2026-09-16 01:33 部署）

用户发 `/学习`（引用聊天记录），机器人只回了句"学到了 记下("——**handler
根本没执行**。

**定位方法（值得复用）**：对比两次线上日志的 `star_request` 行。01:20 那次有
`plugin -> astrbot_plugin_feishu_qa - learn`，01:29 那次**没有**——只到
`on_group_message` 就没了，紧接着是 `ready to request llm provider`。据此可断定
"命令过滤没过、消息流进主 Agent"，而不是 handler 内部逻辑出错。

**根因**：只注册了 `@filter.command("learn")` 一个指令名，用户发的是中文
`/学习`，命令过滤器不认；而 `on_group_message` 在群里只处理 @ 唤醒、不碰指令，
于是整条链路静默走偏（无报错、无提示）。

**修复**：`@filter.command("learn", alias={"学习"})`。`/learn`、`/学习`、
`/学习 ok`、`/learn ok` 四种写法均可用。

**实例侧核验通道**：`GET /api/v1/commands` 会列出全部指令及其 aliases/enabled，
可直接确认注册结果——比翻日志可靠：
```
learn  aliases=['学习']  enabled=True  activated=True
```
（注意响应是 `data.items`，不是 `data.commands`。）

**同类风险**：`/问` 已注册 `{"qa", "Q&A"}`，但中文名只有"问"；若日后加中文
指令，记得同步注册别名。已加 `test_学习_alias_registered` 守卫。
