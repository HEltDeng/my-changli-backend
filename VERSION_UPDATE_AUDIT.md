# Android 版本检查链路审计

## 当前启用链路

App 冷启动首页可用后，低优先级调用 `GET /api/v1/public/version/android`；“关于与检查更新”提供手动检查。服务端只返回状态为 `PUBLISHED` 的 Android 版本。`DRAFT` 与 `DISABLED` 不对 App 可见。

当前“立即更新”只显示本地模拟说明，不会下载或安装 APK。版本后台可维护版本号、说明、最低支持版本、强制更新演示标记、URL、大小和 SHA-256。

## V1.0.2 生产发布记录（2026-09-05）

生产源仍是 `app_versions` 的 `platform=android` 且 `status=PUBLISHED` 记录，公开接口为 `GET /api/v1/public/version/android`。本次已将正式记录更新为：

- `latest_version_name=1.0.2`、`latest_version_code=34`；
- `minimum_version_code=32`、`force_update=0`；
- 正式官网 APK URL、大小 `6028071` 字节和 SHA-256；
- `published_at` 使用本次正式发布时刻，更新说明与官网 V1.0.2 发布摘要一致。

生产数据库位于独立数据目录，发布前由自动化创建 SQLite 在线快照并保存后端环境文件和 systemd unit；WAL 数据库的写入由 `mychangli` 服务用户执行，发布后必须验证服务 active、健康接口、版本接口、bootstrap 和 service-updates。官网 `releases/latest.json` 是同域兜底清单，后端正常时 App 不会无意义地重复请求它。

以后的 Android Release 必须在同一发布记录中同时核对 APK 元数据、官网配置与 manifest、后端公开版本接口和线上 APK SHA-256；任意一项不一致都不能标记为发布通过。

## 旧上游链路处理

- 原 `AppVersionApi`、`LegacyApkApi`、`UpdateDialog` 与 `DownloadService` 代码仍因兼容和未来安全实现参考而保留；
- `UserStore.QueryIsLastedApk` 已改为明确的本地 no-op，不再创建原上游 Retrofit 客户端，也不再回退旧接口；
- 新入口只使用 `CAMPUS_TOOLS_API_BASE_URL`，不会访问旧上游服务的域名、APK 地址、密钥或资源；
- 旧下载服务没有被新检查流程调用。

## 未来真实发布前

必须配置 HTTPS 下载、APK 大小与 SHA-256 双重校验、失败文件删除、同 applicationId/正式签名覆盖校验、未知来源权限提示和 Android 系统安装确认。不得静默安装或绕过用户确认。
