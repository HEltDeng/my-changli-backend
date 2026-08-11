# Database Restore SOP

恢复必须离线。Production restore 只接受完整 verified backup：SQLite 文件存在且 integrity 为 `ok`，相邻 manifest 存在、JSON 有效、format 为 `sqlite3-online-backup-v1`，并且 filename、bytes、SHA-256 与实际文件完全一致。任一条件失败都拒绝恢复，不会因为数据库仍可打开而降级放行。

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

自动测试已演练：写入 -> online backup -> 改变状态 -> offline restore -> integrity -> 原数据恢复；并覆盖 manifest 缺失、非法 JSON、错误 format/filename/size/SHA-256 和已篡改但仍可打开的 SQLite。测试数据全部位于临时目录。
