# Backend Update SOP

禁止在生产直接裸 `git pull && restart`。

1. Pre-check：确认用户批准的目标 SHA、当前 SHA、干净 checkout、磁盘、`/health`、`/ready` 和最近备份状态。
2. Backup：执行在线 SQLite backup 并 verify；记录文件名和 SHA-256，不记录数据内容。
3. Fetch/review：在独立 release 目录 fetch，审查 diff、依赖、schema 和环境变量变化；不得 force/reset 生产历史。
4. Test：在隔离 venv 运行全套测试；用生产配置结构和 TEST_ONLY 临时数据跑 production-like smoke。
5. Compatibility：确认 schema 为可向前兼容幂等变更；破坏性迁移必须有独立 migration/rollback 计划。
6. Deploy：安装锁定依赖，原子切换代码 release/symlink；持久目录不动。
7. Restart：`systemctl restart mychangli-backend`。
8. Verify：`/health`、`/ready`、public bootstrap、Admin/RBAC、版本/flags 和主 API smoke。
9. Observe：至少观察一个约定窗口的 5xx、延迟、锁等待、磁盘和日志。
10. Success：记录最终 SHA；失败立即执行 `ROLLBACK.md`。
