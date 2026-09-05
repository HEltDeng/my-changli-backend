# Android App Update Backend

## 保留的合同

- `GET /api/v1/public/version/android?currentVersionCode=N` 只读取 `PUBLISHED` 记录。
- 返回 latest/minimum version code、version name、update message、HTTPS download URL、APK SHA-256、size、`force_update` 和 `published_at`，并以 camelCase 别名提供同一份直接更新合同。
- 顶层同时返回 `updateAvailable`、`minimumVersionViolated`、`forceUpdate`、`mandatoryUpdate`。服务端只在确有新版本且后台配置强制更新或客户端低于最低支持版本时返回 `forceUpdate=true`；`mandatoryUpdate` 是兼容别名。
- Admin `PUT /api/v1/admin/version/android` 支持 DRAFT/PUBLISHED/DISABLED；不安全 URL、错误 hash、size 或 minimum > latest 会拒绝。

## 验证策略

- old but above minimum：提示可更新，不错误强制；
- current/latest：`updateAvailable=false`、`forceUpdate=false`，即使最新发布记录本身勾选了强制更新；
- below minimum：`minimumVersionViolated=true`、`mandatoryUpdate=true`；
- DRAFT/DISABLED：不下发。

版本检查不会因 V1 暂未启用 APK 自下载而删除。未来下载必须 HTTPS、校验 size/SHA-256、验证正式签名与 applicationId，并由 Android 系统确认安装；Backend 不静默安装。
