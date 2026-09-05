# Android 版本检查链路审计

## 当前启用链路

App 冷启动首页可用后，低优先级调用 `GET /api/v1/public/version/android`；“关于与检查更新”提供手动检查。服务端只返回状态为 `PUBLISHED` 的 Android 版本。`DRAFT` 与 `DISABLED` 不对 App 可见。

当前 Backend 更新接口返回正式版本元数据；正式 Android V1 当前由 `AppUpdateChecker` 以 Backend 为主源。计划中的 V1.0.3 客户端候选版将增加官网 `releases/latest.json` 作为后端失败或响应异常时的同域兜底，并把返回的 HTTPS APK URL 交给统一 AppUpdateCoordinator，在本地完成下载、SHA-256、包名、versionCode 和正式签名证书校验后，通过 Android PackageInstaller 请求用户确认安装。

## 旧上游链路处理

- 原 `AppVersionApi`、`LegacyApkApi`、`UpdateDialog` 与 `DownloadService` 代码仍因兼容和未来安全实现参考而保留；
- `UserStore.QueryIsLastedApk` 已改为明确的本地 no-op，不再创建原上游 Retrofit 客户端，也不再回退旧接口；
- 新入口只使用 `CAMPUS_TOOLS_API_BASE_URL`，不会访问原“长理星球”的域名、APK 地址、密钥或资源；
- 旧下载服务没有被新检查流程调用。

## 未来真实发布前

必须配置 HTTPS 下载、APK 大小与 SHA-256 双重校验、失败文件删除、同 applicationId/正式签名覆盖校验、未知来源权限提示和 Android 系统安装确认。不得静默安装或绕过用户确认。没有真实设备的安装器、权限、升级后数据保留证据时，Release Report 必须写 `PARTIAL`。

以后每次 Android Release 都必须在同一发布记录中同时核对 APK 元数据、官网配置与 manifest、后端公开版本接口、官网更新日志和线上 APK SHA-256；任意一项不一致都不能标记为发布通过。项目级 `Invoke-MyChangliReleaseClosure.ps1` 默认强制发布 Backend，ProductionRelease 模式不能跳过版本同步或公网一致性检查。
