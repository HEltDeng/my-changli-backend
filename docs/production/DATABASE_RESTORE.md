# Database Restore SOP

恢复必须离线，先确认备份的 manifest/SHA-256 和 SQLite integrity。

1. `systemctl stop mychangli-backend`。
2. 记录当前代码 SHA、事故时间和选定备份文件；不要删除现库。
3. `python scripts/verify_backup.py <BACKUP_FILE>`，必须 exit 0。
4. 执行：

   ```bash
   python scripts/restore_database.py <BACKUP_FILE> --confirm-offline-restore
   ```

5. 工具先用 SQLite online backup API 为现库创建 `pre-restore-*` 安全副本，再把备份恢复到临时库、做 integrity check，最后原子替换目标；不会把正在运行的主库当普通文件复制。
6. 复核 owner/mode：数据库 `0600`、目录 `0750`，均归服务用户。
7. `systemctl start mychangli-backend`。
8. 请求 `/health`、`/ready`，再检查 public bootstrap、管理员登录、普通用户登录和一项非破坏性查询。
9. 观察日志与 Nginx 5xx；异常则停止服务并从 `pre-restore-*` 回退。

自动测试已演练：写入 -> online backup -> 改变状态 -> offline restore -> integrity -> 原数据恢复。测试数据全部位于临时目录。
