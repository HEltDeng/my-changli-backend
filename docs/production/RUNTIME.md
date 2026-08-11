# Production Runtime

## 决定

Production 使用 `Waitress 3.x -> wsgi.application -> ApiHandler`。Waitress 是生产级纯 Python WSGI server，支持 Linux 与 Windows；现有代码不是 WSGI framework，因此 `wsgi.py` 只负责把 WSGI environ/response 适配到现有 handler，不重写业务路由。

`server.py` 的 `ThreadingHTTPServer` 只用于本地开发和既有 HTTP 端到端测试；当 `MY_CHANGLI_ENV=production` 时直接运行它会拒绝启动。

## 启动

```bash
set -a
. /etc/mychangli/backend.env
set +a
/opt/mychangli/backend/.venv/bin/python /opt/mychangli/backend/production.py
```

正式长期运行使用 systemd 模板，不在 shell 中裸跑。

## 初始并发

- 进程：1。SQLite V1 不启动多个应用进程。
- Waitress threads：8，环境变量 `MY_CHANGLI_WAITRESS_THREADS` 可在 2–64 调整。
- 连接上限：200；请求体同时受 Waitress、应用和 Nginx 限制。
- App server 显式绑定 `127.0.0.1:<BACKEND_PORT>`，Nginx 是唯一公网入口。

V1 的短事务、单机规模和有限写并发适合此模型。若线程长期耗尽、P95 延迟持续升高或 SQLite 锁等待明显增加，先定位慢请求和写竞争；需要多个进程/实例时，应先迁移 PostgreSQL，而不是盲目增加 workers。

Waitress 官方项 `expose_tracebacks` 固定为 false。受信代理只配置回环 Nginx，清除所有不受信的 `X-Forwarded-*` 值。

## Upstream references

- Waitress official documentation: <https://docs.pylonsproject.org/projects/waitress/en/latest/>
- Waitress package metadata and supported Python versions: <https://pypi.org/project/waitress/>
