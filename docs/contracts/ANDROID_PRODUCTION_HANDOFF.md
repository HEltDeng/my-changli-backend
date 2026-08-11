# Android Production Handoff

本文件只记录未来 Android Production Release 模块动作；本轮未修改 Android。

1. 域名、备案、TLS 和 Backend 验收后，将仓库外构建参数 `MY_CHANGLI_CAMPUS_TOOLS_API_BASE_URL_RELEASE` 设为 `https://api.<ROOT_DOMAIN>/api/v1/`；保留结尾 `/`。
2. 当前 Release 默认 `https://campus-tools.invalid/api/v1/` 会 fail closed；在真实端点前不得绕过 `ServiceEndpointPolicy`。
3. 真机验证 TLS 链、无明文回退、登录/注销/session 过期、401/403/429/503 可读行为和网络不可用离线降级。
4. 验证 public bootstrap、service updates、版本 minimum/force、remote flags 的 CLOSED/MAINTENANCE，避免关闭功能持续重试。
5. 若启用 APK 下载，必须校验 HTTPS、size、SHA-256、applicationId 与同一正式签名，再交给系统安装确认；store URL 尚不是独立字段，可继续使用 `download_url` 或在独立合同评审后扩展。
6. 验证失物图片绝对 URL、3 MiB/像素限制错误、上传取消和 413 UX。
7. 不得把教务密码、Cookie、验证码、成绩或课表发送到本 Backend。

ANDROID_HANDOFF_REQUIRED：仅正式 Release Base URL、真实 TLS/网络回归和下载/商店策略需要在后续 Android 模块处理；当前 API DTO 无破坏性差异。
