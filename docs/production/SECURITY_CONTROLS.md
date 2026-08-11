# Production Security Controls

- Auth：scrypt 密码摘要；随机 token 只存 SHA-256；普通 App 用户 absolute TTL 为 30 天，Admin 默认 8 小时且只能配置在 30 分钟至 24 小时；logout 删除当前 session；管理员密码重置撤销目标全部 session；账号状态和 Admin enabled 在每次鉴权检查。
- RBAC：所有 `/api/v1/admin/*` 在服务端检查角色/模块权限；只有明确属于合法 `ADMIN_ROLES` 的数据库值才获得权限，空值/未知值不回退且 Production readiness 拒绝；普通用户和跨模块运营员返回 403。
- Admin Web：同源静态资源、Bearer token 在 `sessionStorage`、无 Cookie auth，因此不依赖 Cookie CSRF token；不发送 CORS `*`，危险 Admin 请求的浏览器 Origin 必须匹配配置值。
- Abuse：单进程内存滑动窗口保护登录、注册、设备认证、反馈和失物写操作；登录还按账号标识限制。迁移多实例时需换成共享限流存储。
- Proxy：Production Waitress bind 只接受 loopback IP，并叠加 Host allowlist；Waitress 只信任回环 Nginx，并清除不受信代理头。客户端不能自行伪造可信来源 IP。
- Input：JSON 对象、长度、分页、日期、枚举、URL 和所有 ID 均校验；SQL 使用参数绑定；公网异常不回传堆栈。
- Upload：鉴权通过后才落盘；base64 和总大小受限；Pillow 完整解码、像素上限、只接收 JPEG/PNG/WebP、去除元数据后重新编码；随机文件名、原子写入、`nosniff`/sandbox，更新/删除/幂等冲突清理文件。
- Filesystem：代码只读；数据库、上传、日志、备份和 env 分离；服务非 root、systemd sandbox、UMask 0027。

V1 限流在进程重启后清空，这是已知的单机边界；Nginx 可在云部署时再加粗粒度连接/请求限制，但不能取代应用的账号维度限制。

Image decoding and normalization uses Pillow; supported ranges are declared in
`requirements.txt` and deployed versions are pinned in `requirements.lock.txt`.
Upstream package metadata: <https://pypi.org/project/pillow/>.
