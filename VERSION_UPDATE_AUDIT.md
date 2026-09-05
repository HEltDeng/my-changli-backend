# Android 版本检查链路审计

## 当前启用链路

App 冷启动首页可用后，低优先级调用 `GET /api/v1/public/version/android`；“关于与检查更新”提供手动检查。服务端只返回状态为 `PUBLISHED` 的 Android 版本。`DRAFT` 与 `DISABLED` 不对 App 可见。

当前“立即更新”只显示本地模拟说明，不会下载或安装 APK。版本后台可维护版本号、说明、最低支持版本、强制更新演示标记、URL、大小和 SHA-256。

## 旧上游链路处理

- 原 `AppVersionApi`、`LegacyApkApi`、`UpdateDialog` 与 `DownloadService` 代码仍因兼容和未来安全实现参考而保留；
- `UserStore.QueryIsLastedApk` 已改为明确的本地 no-op，不再创建原上游 Retrofit 客户端，也不再回退旧接口；
- 新入口只使用 `CAMPUS_TOOLS_API_BASE_URL`，不会访问旧上游服务的域名、APK 地址、密钥或资源；
- 旧下载服务没有被新检查流程调用。

## 未来真实发布前

必须配置 HTTPS 下载、APK 大小与 SHA-256 双重校验、失败文件删除、同 applicationId/正式签名覆盖校验、未知来源权限提示和 Android 系统安装确认。不得静默安装或绕过用户确认。
