# 我的长理 Backend 架构

本文以 `d864ee4aa6f8ae24398e057bba017bf39823f856` 的实际源码为起点，记录本轮 Production Backend 模块采用的现状判断与目标边界。它不代表真实云资源已经创建。

## Development 当前结构

```text
Android Debug / Browser Admin
  -> http://127.0.0.1:8787
  -> Python ThreadingHTTPServer
  -> ApiHandler(BaseHTTPRequestHandler)
       |- /health
       |- /api/v1/auth/*
       |- /api/v1/sports/*
       |- /api/v1/lost-found/*
       |- /api/v1/library/*
       |- /api/v1/feedback/*
       |- /api/v1/public/*
       |- /api/v1/admin/*
       |- /admin/* static assets
       `- /uploads/* images
  -> SQLite + local uploads
```

### 运行与路由

- Python framework：没有 Flask、Django、FastAPI 或其他 Web framework；业务直接建立在 Python 标准库 HTTP handler 上。
- 开发入口：`server.py:main()`。
- HTTP server：`ThreadingHTTPServer`，仅保留给 Windows 本地开发和原有端到端测试。
- API routing：`ApiHandler.route()` 做显式 method/path 分派，具体业务方法仍位于 `server.py`。
- 基础能力：`core.py` 提供错误模型、校验、JSON、分页和 SQLite 连接生命周期。

### 身份、权限与管理后台

- Auth：手机号账号或旧安装标识账号；密码为带随机盐的 scrypt；客户端拿到的随机 session token 只以 SHA-256 摘要入库。
- Account：资料更新、预约识别资料、退出、注销均在 `/api/v1/auth/*`。
- RBAC：服务端区分普通用户、`SUPER_ADMIN`、体育、失物、资料库和内容运营角色；Admin API 不依赖前端隐藏按钮授权。
- Admin：`admin/index.html`、`admin/app.js`、`admin/styles.css` 由后端同源提供；Bearer token 仅存浏览器 `sessionStorage`，不使用 Cookie session。
- Audit：`admin_audit_logs` 记录管理操作；`user_behavior_events` 和 `risk_candidates` 记录最小业务风控事件。

### 数据与业务模块

- Database：单文件 SQLite；schema 位于 `schema.sql`，启动时由 `initialize_database()` 幂等补列和建立索引。
- Announcements：公共公告与 Admin 管理。
- Feature flags：`remote_flags` 控制体育预约、失物招领、资料库等联网功能及维护状态。
- Version：`app_versions` 保存 Android latest/minimum version、说明、下载 URL、SHA-256、文件大小、强制更新与发布状态。
- Feedback：用户反馈、工作流状态、公开回复、内部备注和事件历史。
- Reservation：场馆、时段、库存、预约规则、取消限制、资料快照及模拟支付状态。
- Lost & found：发布、检索、编辑、删除、收藏、认领、举报与单张图片。
- Resource library：外部链接资料、可见性、浏览、收藏、投诉及 Admin 发布/下架。
- Uploads：base64 图片写入本地目录，通过随机 UUID 文件名读取；本轮会补齐鉴权前落盘、内容规范化和孤儿清理。

### 已确认边界

- 教务账号、密码、验证码、Cookie、课表、成绩和考试数据不属于本后端，也不得进入本后端。
- 体育预约仍为模拟业务，不连接学校真实库存、核销或支付。
- 原生 Android 请求不需要浏览器 CORS；Admin 使用同源 API。

## Production 目标结构

```text
Android Release / Admin Browser
  -> HTTPS
  -> api.<ROOT_DOMAIN>
  -> Nginx (TLS, Host, body limit, security headers, proxy headers)
  -> 127.0.0.1:<BACKEND_PORT>
  -> Waitress (single process, bounded worker threads, no tracebacks)
  -> WSGI adapter
  -> existing ApiHandler routes and business logic
  -> /var/lib/mychangli/{database,uploads}
     /var/log/mychangli/
     /var/backups/mychangli/
     /etc/mychangli/backend.env
```

### 目标设计决定

- Production App Server 采用 Waitress。现有 handler 通过薄 WSGI 适配层复用，避免为“换框架”重写 4,000 多行业务代码；Waitress 同时支持 Linux 与 Windows，线程模型适合 V1 单机 SQLite。
- Production 只运行一个应用进程，初始使用有限线程并保持 SQLite WAL；不会为了形式引入 Redis、PostgreSQL、Docker 或 Kubernetes。
- Nginx 是唯一公网入口，负责 TLS、HTTP 到 HTTPS 跳转、请求体限制和反向代理。Waitress 仅显式绑定回环地址，并只信任 Nginx 所在回环代理。
- Production 配置必须显式声明环境、监听、数据库、上传、日志、备份、允许 Host、Admin origin 和秘密；缺失或占位值直接拒绝启动。
- 持久数据、上传、日志、备份和 secret 与 Git checkout 分离；代码回滚不得覆盖这些目录。
- `/health` 用作进程存活检查，`/ready` 用作数据库可用性检查；两者不泄露主机名、路径、环境变量或管理员信息。

### V1 SQLite 决定

V1 继续使用 SQLite，前提是单机、单应用进程、有限写并发和在线一致性备份。当前每次操作使用独立连接，预约关键写入已使用 `BEGIN IMMEDIATE`，schema 已有业务唯一约束和主要索引。生产配置启用外键、WAL、busy timeout、合理同步级别、启动完整性检查和在线 backup API。

出现持续 `database is locked`、需要多个 Backend 实例、并发写显著上升、分析查询影响在线请求、数据增长使备份/恢复窗口不可接受或需要水平扩展时，再计划迁移 PostgreSQL。迁移必须保留当前唯一约束、事务语义、RBAC 与审计契约。
