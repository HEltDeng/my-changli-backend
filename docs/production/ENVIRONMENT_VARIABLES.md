# Production Environment Variables

程序不会自动读取 `.env`。systemd 通过仓库外的 `<ENV_FILE>` 注入；`.env.example` 只有占位符。

| Variable | Purpose | Production | Secret | Format / generation | Rotation | Missing behavior | Backup |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `MY_CHANGLI_ENV` | 环境边界 | 必需 | 否 | 固定 `production` | 不适用 | 拒绝 Production 入口 | 否 |
| `MY_CHANGLI_DEBUG` | 调试开关 | 必需 | 否 | 固定 `false` | 不适用 | true 时拒绝启动 | 否 |
| `MY_CHANGLI_HOST` | App server bind | 必需 | 否 | 推荐 `127.0.0.1` | 网络架构变更 | 缺失/公网通配地址拒绝 | 否 |
| `MY_CHANGLI_PORT` | 内部监听端口 | 必需 | 否 | 1–65535 | 部署变更 | 拒绝启动 | 否 |
| `MY_CHANGLI_DB_PATH` | SQLite 主库 | 必需 | 敏感路径 | 代码目录外绝对 `.db` 路径 | 迁移时 | 拒绝启动 | 数据库单独备份 |
| `MY_CHANGLI_UPLOAD_PATH` | 用户图片 | 必需 | 敏感路径 | 代码目录外绝对目录 | 迁移时 | 拒绝启动 | 是 |
| `MY_CHANGLI_LOG_PATH` | JSONL 应用日志 | 必需 | 敏感路径 | 绝对 `.jsonl`/`.log` 文件 | 日志迁移 | 拒绝启动 | 通常否，按合规留存 |
| `MY_CHANGLI_BACKUP_PATH` | 本机备份 | 必需 | 敏感路径 | 绝对目录 | 备份架构变更 | 拒绝启动 | 备份本身需异地复制 |
| `MY_CHANGLI_PHONE_HASH_SECRET` | 手机号/预约资料 HMAC 索引 | 必需 | 是 | `secrets.token_urlsafe(48)` 等至少 32 bytes | 维护窗口内重建全部摘要 | 拒绝启动；不会自动生成 | 加密离线必备 |
| `MY_CHANGLI_PII_ENCRYPTION_KEY` | 手机号 Fernet 加密 | 必需 | 是 | `Fernet.generate_key()` | 解密后重加密全部密文 | 拒绝启动；不会自动生成 | 加密离线必备 |
| `MY_CHANGLI_ALLOWED_HOSTS` | Host allowlist | 必需 | 否 | 逗号分隔，如 `api.<ROOT_DOMAIN>` | 域名变更 | 空、`*`、占位符拒绝 | 否 |
| `MY_CHANGLI_ADMIN_ORIGIN` | Admin 浏览器允许 origin | 必需 | 否 | 单个 HTTPS origin | 管理地址变更 | 非 HTTPS/缺失拒绝 | 否 |
| `MY_CHANGLI_TRUSTED_PROXY` | 唯一受信 Nginx peer | 必需 | 否 | 推荐 `127.0.0.1` | 拓扑变更 | 拒绝启动 | 否 |
| `MY_CHANGLI_WAITRESS_THREADS` | 应用线程 | 可选 | 否 | 2–64，默认 8 | 容量调优 | 非法值拒绝 | 否 |
| `MY_CHANGLI_SQLITE_BUSY_TIMEOUT_MS` | 锁等待 | 可选 | 否 | 1000–60000，默认 10000 | 锁竞争调优 | 非法值拒绝 | 否 |
| `MY_CHANGLI_MAX_BODY_BYTES` | JSON 总请求上限 | 可选 | 否 | 默认 6291456 | 合同变更 | 非法值拒绝 | 否 |
| `MY_CHANGLI_UPLOAD_MAX_BYTES` | 图片原始/规范化上限 | 可选 | 否 | 默认 3145728 | 合同变更 | 非法值拒绝 | 否 |
| `MY_CHANGLI_UPLOAD_MAX_PIXELS` | 解压像素上限 | 可选 | 否 | 默认 20000000 | 安全调优 | 非法值拒绝 | 否 |
| `MY_CHANGLI_SUPER_ADMIN_USERNAME` | 一次性首管 bootstrap | 仅 bootstrap | 是 | 非默认用户名 | 创建后移除 | bootstrap 拒绝 | 密码库记录账号名 |
| `MY_CHANGLI_SUPER_ADMIN_PASSWORD` | 一次性首管密码 | 仅 bootstrap | 是 | 至少 16 位随机值 | bootstrap 后立即删除/首次登录轮换 | bootstrap 拒绝 | 密码管理器，不进普通备份 |

本系统不使用 JWT，也没有长期 session signing/auth secret：登录 token 每次随机生成，数据库只保存 SHA-256 摘要。TLS 私钥由 Nginx 管理，不属于 Backend 环境变量。
