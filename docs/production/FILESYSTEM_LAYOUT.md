# Production Filesystem Layout

推荐 Linux 布局：

```text
/opt/mychangli/backend/              Git checkout + .venv（发布代码）
/etc/mychangli/backend.env           Production env，仓库外
/var/lib/mychangli/database/         my-changli.db、WAL/SHM
/var/lib/mychangli/uploads/          规范化用户图片
/var/log/mychangli/                  backend.jsonl 与轮转文件
/var/backups/mychangli/              SQLite online backups/manifests
```

服务用户拥有 `/var/lib/mychangli`、`/var/log/mychangli`、`/var/backups/mychangli` 的必要写权限，不拥有 `/etc/mychangli/backend.env`。代码更新只替换 `/opt/mychangli/backend` 的 release checkout；数据库、上传、备份和 secret 绝不放在 Git checkout 内，因此 rollback 不会误删持久数据。

建议 mode：env `0640`，数据库/备份 `0600`，持久目录 `0750`，服务 `UMask=0027`。异地备份是未来云步骤，本轮不上传 COS 或真实数据。
