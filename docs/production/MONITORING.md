# Monitoring

## 上线第一版最低要求

- 每分钟从服务器本机检查 `/health`；从外部 HTTPS 检查一次 public `/health`。
- `/ready` 失败、连续 5xx、systemd restart loop 立即告警。
- 根分区、数据库、上传、日志和备份目录在 80%/90% 两级告警。
- 每日备份命令 exit code 非 0、manifest 校验失败或 26 小时无新备份告警。
- Nginx 4xx/5xx 突增、TLS 到期和证书链失败告警。
- 应用 JSONL 中 `INTERNAL_ERROR` 或持续高 latency 告警；日志内容不得进入公共分析平台前先做数据边界审查。

可用 systemd timer/cron + curl + 磁盘脚本 + 邮件/短信告警实现，不要求重量级平台。监控账号不读取数据库内容或 secret。

## 未来增强

用户增长、多个实例或更严格 SLO 出现后，再评估集中 metrics、日志聚合、分布式 tracing、外部 uptime 和共享限流。V1 不引入 Kubernetes/Redis 作为形式化装饰。
