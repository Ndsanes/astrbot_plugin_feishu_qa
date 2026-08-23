# astrbot-lark-kit

围绕官方 [`lark-cli`](https://github.com/larksuite/cli) 的稳定适配层,供 AstrBot 插件复用:

- **子进程调用**:`run_lark_cli` / `run_lark_cli_json`,统一超时、cwd 与二进制定位
  (注入 bin_path → vendored → `LARK_CLI_PATH` → PATH);
- **envelope 解析**:lark-cli 的 JSON 信封收敛为类型化结果;
- **错误归一**:所有失败路径收敛到 `LarkKitError` 层级,下游不解析 returncode/stderr;
- **限流**:`RateLimiter`;
- **登录态健康检查**:`auth_status_from_dict` / `health_of`;
- **平台标识解析**:`resolve_platform_instance`(UMO → 平台实例身份,纯函数)。

零第三方运行时依赖(仅 Python 标准库),requires-python >= 3.12。

## 安装

```bash
pip install git+https://github.com/Ndsanes/astrbot_lark_kit.git@v0.1.0
```

开发模式(工作区源码):AstrBot 插件以相对导入 + vendored 子包兜底的方式消费本包,
详见各插件 README。

## 设计边界

本包只做 transport / auth / 标识层,不做业务 SDK:
不含 docs、bitable、wiki、message 等领域 API,也不 import AstrBot。
