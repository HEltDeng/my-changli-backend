# Production Database Bootstrap

生产库必须从空路径创建，禁止复制 `data/my-changli.db` 或测试库。

## 顺序

1. 配置完整 Production env 与空的 `MY_CHANGLI_DB_PATH`。
2. 临时注入 `MY_CHANGLI_SUPER_ADMIN_USERNAME`、`MY_CHANGLI_SUPER_ADMIN_PASSWORD`。
3. 执行 `python manage.py bootstrap-production`。
4. 命令执行 schema、幂等初始化、WAL 和 integrity check，并拒绝任何已有用户或业务记录。
5. 命令创建唯一首个 `SUPER_ADMIN`，密码只保存为 scrypt 摘要。
6. 从 env 文件/终端删除一次性密码，启动 `production.py`。

## 干净状态定义

- 0 普通用户，创建后只有 1 个首管；
- 0 预约、失物、认领、举报、反馈、公告、收藏、浏览和投诉；
- Android 版本默认是 `DRAFT`，不会向 App 发布；
- 场馆/时段、remote flags 和 5 个资料外链是源码明确的系统默认配置，不是历史开发数据；
- `PRAGMA integrity_check = ok`、`journal_mode = wal`、外键开启。

`production.py` 每次启动都会复核完整性和至少一个启用的 `SUPER_ADMIN`；不满足时拒绝服务。
