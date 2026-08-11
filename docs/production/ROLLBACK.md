# Rollback

## 代码或依赖失败

停止服务，切回上一个已知良好 release checkout/venv，保持数据库、上传、env 和备份不动；启动后验证 health/readiness 和主 API。

## 启动或 health 失败

先读取 systemd 与应用日志；若是配置缺失，不降低 fail-fast 或填默认 secret。恢复上一版本配置与代码，再启动验证。

## 数据库变化失败

1. 停服务，禁止继续写入。
2. 保留当前故障库。
3. 若新代码的幂等向前 schema 与旧代码兼容，可直接回旧代码；不确定时使用更新前已验证备份，按 `DATABASE_RESTORE.md` 离线恢复。
4. 恢复工具会先创建 `pre-restore-*` 安全副本。

## 验收

回滚后必须验证 `/health`、`/ready`、普通登录、Admin RBAC、public bootstrap、版本/flags、关键只读业务和备份任务。记录事故时间、SHA、数据库备份名和客观结果，不记录 secret/PII。
