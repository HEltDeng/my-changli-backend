# Deployment SOP

以下是拿到全新腾讯云 Linux 后的执行顺序。本轮未实际购买服务器、配置 DNS、申请证书或创建 Production secret；相应步骤均标记 `FUTURE CLOUD STEP`。

## 1. Server 与服务用户 — FUTURE CLOUD STEP

1. 创建受支持的 Linux 实例，完成系统更新、防火墙和 SSH 密钥登录；禁止密码公网 SSH。
2. 创建非 root 用户 `<APP_USER>`，不赋予交互式 sudo；只给运行 Backend 所需权限。
3. 安装 Python 3.11+、venv、Nginx、Git 和 SQLite CLI。

## 2. Application

1. 将已验收 commit checkout 到 `<APP_DIR>`，记录 SHA；不要在生产目录直接开发。
2. 创建 `<APP_DIR>/.venv`，执行 `.venv/bin/python -m pip install -r requirements.lock.txt`；`requirements.txt` 只用于维护支持范围。
3. 按 `FILESYSTEM_LAYOUT.md` 建目录、owner、group 和 mode。
4. 从 `.env.example` 复制变量名到仓库外 `<ENV_FILE>`，填入真实值；先执行只读配置导入验证，不能保留占位符。

## 3. Database 与 Admin

1. 指向全新的空数据库路径。
2. 在受控终端临时提供一次性首管变量，执行 `python manage.py bootstrap-production`。
3. 删除一次性密码变量；确认普通用户/历史业务均为 0、版本为 DRAFT、integrity 为 ok。
4. 执行一次 `scripts/backup_database.py` 和 `scripts/verify_backup.py`。

## 4. Local Backend

1. 临时前台运行 `python production.py`。
2. 使用正确 Host 请求 `http://127.0.0.1:<BACKEND_PORT>/health` 和 `/ready`。
3. 检查日志无 traceback、secret、token 或 PII；停止前台进程。

## 5. systemd

1. 用实际路径替换 `deploy/systemd/mychangli-backend.service.example` 占位符并安装到 `/etc/systemd/system/`。
2. `systemd-analyze verify`、`systemctl daemon-reload`、`systemctl enable --now mychangli-backend`。
3. 检查非 root 用户、Restart、读写目录和重启后恢复。

## 6. Nginx、DNS、TLS — FUTURE CLOUD STEP

1. 替换 `deploy/nginx/mychangli.conf.example` 中 `<ROOT_DOMAIN>`、`<BACKEND_PORT>`、证书占位符。
2. `nginx -t` 后 reload；Backend 仍只绑定回环。
3. 配置 `api.<ROOT_DOMAIN>` DNS 到服务器公网 IP。
4. 完成 ICP/APP 备案及证书签发后启用 TLS；验证 HTTP 强制跳 HTTPS、TLS 链、HSTS 和真实 Host。

## 7. Smoke、Backup、Monitoring、Launch

1. HTTPS 检查 `/health`、`/ready`、public bootstrap、版本、flags、Admin 登录、普通账号登录/注销、失物安全图片和 403 RBAC。
2. Android 仅在独立 Release 模块把 Base URL 注入为 `https://api.<ROOT_DOMAIN>/api/v1/`，完成真机网络回归。
3. 安装每日备份 timer/cron，首次备份后立刻做隔离恢复演练。
4. 配置 `MONITORING.md` 的最小告警，观察错误、延迟、磁盘和备份。
5. 只有用户验收、法律/授权、域名/备案、签名和候选包检查都闭合后，才进入 launch 决策。
