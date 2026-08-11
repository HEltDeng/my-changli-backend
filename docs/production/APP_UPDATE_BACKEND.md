# Android App Update Backend

## 保留的合同

- `GET /api/v1/public/version/android?currentVersionCode=N` 只读取 `PUBLISHED` 记录。
- 返回 latest/minimum version code、version name、update message、HTTPS download URL、APK SHA-256、size、`force_update` 和 `published_at`。
- 顶层同时返回 `updateAvailable`、`minimumVersionViolated`、`mandatoryUpdate`。当前 Android 会用 `force_update || current < minimum_version_code` 自行计算 mandatory，新增顶层字段向后兼容。
- Admin `PUT /api/v1/admin/version/android` 支持 DRAFT/PUBLISHED/DISABLED；不安全 URL、错误 hash、size 或 minimum > latest 会拒绝。

## 验证策略

- old but above minimum：提示可更新，不错误强制；
- current/latest：`updateAvailable=false`；
- below minimum：`minimumVersionViolated=true`、`mandatoryUpdate=true`；
- DRAFT/DISABLED：不下发。

版本检查不会因 V1 暂未启用 APK 自下载而删除。未来下载必须 HTTPS、校验 size/SHA-256、验证正式签名与 applicationId，并由 Android 系统确认安装；Backend 不静默安装。
