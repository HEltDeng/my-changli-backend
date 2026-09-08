# Android 更新控制链路审计

## 唯一实时事实源

更新链路现在把两个概念分开保存：

- `app_versions` / `app_release_versions` 只描述已经发布的 APK：版本名、版本号、下载地址、SHA-256、大小、发布说明和发布时间；
- `app_update_policies` 保存当前运营控制策略：是否检查更新、是否强制、最低支持版本、`policy_revision`、修改时间和操作人。

管理后台的 Release Metadata 与 Update Policy 使用两个独立表单和接口。策略保存后必须重新 GET 当前配置并比对服务器返回值和 revision，页面不会把本地 checkbox 状态当作保存成功。

公开更新检查的唯一实时事实源是数据库中的当前 policy。`GET /api/v1/public/update-policy/android` 是新合同；`GET /api/v1/public/version/android` 保留给 V1.0.4 等历史客户端，并返回同一个实时决策。两条接口都设置禁止缓存响应头。

官网 `releases/latest.json` 只保留 Release Metadata，不再包含 `forceUpdate` 或 `minimumVersionCode`。静态文件即使被浏览器或 CDN 缓存，也不能再次成为强制更新事实源。

## 决策规则

服务端分别计算：

```text
updateAvailable = updateEnabled && latestVersionCode > currentVersionCode
minimumVersionViolated = currentVersionCode < minimumSupportedVersionCode
mandatoryUpdate = updateAvailable && forceUpdateEnabled && minimumVersionViolated
```

因此版本号较旧只说明“存在更新”；只有服务器当前 policy 同时开启强制更新且客户端低于最低支持版本时才进入强制更新。关闭 `forceUpdateEnabled` 后，即使最低支持版本仍高于客户端，下一次成功请求也返回普通更新而不是强制更新。

每次策略修改都在一个事务中执行 `policy_revision + 1`，并把操作人、旧策略、新策略、revision 和修改时间写入审计日志。客户端检查日志记录客户端版本、最新版本、服务器决策和 policy revision。

## V1.0.4 兼容适配

V1.0.4 读取 `latestVersion.force_update`，并把 `minimum_version_code` 当成独立强制条件。兼容响应因此必须把服务器当前的最终决策同时映射到这两个旧字段：

- 当前决定强制：`force_update=true`，最低版本返回当前策略值；
- 当前决定普通更新或无更新：`force_update=false`，最低版本兼容返回 `1`。

这使不可修改的 V1.0.4 在下一次成功请求后也能解除强制状态。V1.0.4 本身只在 Activity 创建时检查更新，前后台切换不会重新请求；这个二进制限制只能通过杀进程重启或未来客户端升级解决。

## 新客户端状态机

Android 候选客户端使用显式状态：`IDLE`、`CHECKING`、`NO_UPDATE`、`OPTIONAL_UPDATE`、`FORCE_UPDATE`、`DOWNLOADING`、`VERIFYING`、`INSTALLING`、`ERROR`。成功响应总是覆盖旧决策，允许 `FORCE_UPDATE -> OPTIONAL_UPDATE / NO_UPDATE` 反向切换。

强制状态不落地保存为权威事实；冷启动、回到前台和强制更新页“重新检查”都会请求服务器。网络失败可以显示错误，但不会用旧的本地 `force=true` 永久阻止用户。下载中断恢复只保存安装文件事实，不保存强制策略。

## 缓存边界

更新策略 API 返回：

```http
Cache-Control: no-store, no-cache, must-revalidate, max-age=0
Pragma: no-cache
Expires: 0
```

生产审计没有发现 Redis、Memcached、Varnish 或 Nginx proxy cache。静态 `latest.json` 仍可以按普通发布文件缓存，但由于已经移除控制字段，不再影响强制更新撤回。

## 真实发布边界

本次 P0 只产生开发分支代码和内部测试候选包，不修改生产版本号、生产数据库策略、正式 `latest.json`、官网 APK 或 GitHub Release，也不部署 Backend/Website。未来部署必须在人工验收后单独执行，并重新验证 Backend 直连、Nginx、正式域名和可能存在的 CDN/Edge 四层响应。
