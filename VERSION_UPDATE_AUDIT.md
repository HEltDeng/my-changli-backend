# Android 版本检查链路审计

## 当前启用链路

App 冷启动首页可用后，“关于与检查更新”都会调用
`GET /api/v1/public/version/android?currentVersionCode=<code>`。服务端只读取
`app_versions` 中 `platform='android' AND status='PUBLISHED'` 的唯一配置；`DRAFT`
和 `DISABLED` 不对 App 可见。管理后台保存和发布、公开版本检查、启动 bootstrap
以及服务动态卡片都使用这套版本数据，不另建版本系统。

公开版本响应保留 Android V1 已使用的 snake_case 字段，同时增加直接更新合同字段：
`versionCode`、`versionName`、`minimumSupportedVersionCode`、`releaseNotes`、
`downloadUrl`、`apkSha256`、`apkSize`、`publishedAt` 和 `forceUpdate`。顶层也返回
`updateAvailable`、`minimumVersionViolated`、`forceUpdate`；旧客户端继续读取原字段，
新客户端可以只依赖一个 manifest 响应完成检查。

强制策略是服务端明确计算的有效策略：当前版本号不低于最新版本时
`updateAvailable=false` 且顶层 `forceUpdate=false`；低于最新版本时，后台配置的
`force_update=true` 或客户端低于 `minimum_version_code`，才返回
`forceUpdate=true`。`mandatoryUpdate` 继续作为 V1 兼容别名。最低支持版本因此是
服务端兜底的强制更新边界，而不是仅把 checkbox 原样转发给客户端。

## 旧上游链路处理

- 原 `AppVersionApi`、`LegacyApkApi`、`UpdateDialog` 与 `DownloadService` 代码仍因兼容和未来安全实现参考而保留；
- `UserStore.QueryIsLastedApk` 已改为明确的本地 no-op，不再创建原上游 Retrofit 客户端，也不再回退旧接口；
- 新入口只使用 `CAMPUS_TOOLS_API_BASE_URL`，不会访问原“长理星球”的域名、APK 地址、密钥或资源；
- 旧下载服务没有被新检查流程调用。

## Android 端尚未闭环的部分

Backend 已提供 APK 直链、SHA-256、大小、版本说明和有效强制策略；Android 端仍需在
另一轮接入下载进度、SHA-256/包名/正式签名校验、失败文件清理、PackageInstaller
确认、安装权限和重启。当前不能把 Backend/API/Admin 的 PASS 报告为 App 内安装
E2E PASS，也不能静默安装或绕过用户确认。

## 未来真实发布前

必须配置 HTTPS 下载、APK 大小与 SHA-256 双重校验、失败文件删除、同 applicationId/
正式签名覆盖校验、未知来源权限提示和 Android 系统安装确认。没有真实设备的安装器、
权限、升级后数据保留证据时，Release Report 必须写 `PARTIAL`。以后每次 Android
Release 都必须在同一发布记录中同时核对 APK 元数据、官网配置与 manifest、后端公开
版本接口、官网更新日志和线上 APK SHA-256；任意一项不一致都不能标记为发布通过。
