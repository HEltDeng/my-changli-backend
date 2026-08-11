# Logging and Audit

应用日志为 JSONL，至少包含 timestamp、level、route（不含 query）、status、latency、request id 和 event。生产日志写入 `MY_CHANGLI_LOG_PATH`，10 MiB 轮转、保留 7 个文件；systemd 同时接收控制台日志。

禁止记录完整 request/response body、Authorization、Bearer token、Cookie、密码、secret、手机号、学号、姓名或加密 key。`logging_config.redact()` 对结构字段和常见文本凭据二次脱敏；异常响应只向公网返回稳定 `INTERNAL_ERROR`，stack trace 只在服务器日志。

Admin 操作由 `admin_audit_logs` 记录动作、对象和最小摘要；不会把密码、完整 token 或 PII 原文写入详情。用户风控事件与管理员审计分表。日志访问仅限运维，按事故、合规和磁盘预算确定保留期。

测试 `test_logging_redacts_credentials_and_pii` 使用临时随机值验证日志输出不包含密码、Bearer、手机号或 secret。
