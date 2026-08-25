# API 契约摘要

所有响应采用：

```json
{"ok":true,"data":{}}
```

失败响应采用对应 HTTP 状态码及：

```json
{"ok":false,"error":{"code":"ERROR_CODE","message":"可读说明"}}
```

除健康检查、设备认证、注册登录和公共启动配置外，请求需携带 `Authorization: Bearer <token>`。

## 身份

- `POST /api/v1/auth/device`：Debug App 设备级测试身份。
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `GET /api/v1/auth/me`
- `PATCH /api/v1/auth/profile`
- `GET /api/v1/auth/reservation-profile`
- `PUT /api/v1/auth/reservation-profile`
- `DELETE /api/v1/auth/reservation-profile`
- `POST /api/v1/auth/logout`
- `DELETE /api/v1/auth/account`

手机号注册使用不可逆 HMAC 摘要做精确检索，列表只返回脱敏值；供受控管理员查看的号码密文使用独立本地密钥加密。密码只保存加盐 `scrypt` 摘要。学校教务账号、密码、Cookie、会话及教务查询结果不进入本后端。

## 体育预约

- `GET /api/v1/sports/venues`
- `GET /api/v1/sports/context`
- `GET /api/v1/sports/availability?venueId=...&date=YYYY-MM-DD`
- `GET /api/v1/sports/reservations`
- `POST /api/v1/sports/reservations`
- `POST /api/v1/sports/reservations/{id}/cancel`

数据库对 `(venue_id, booking_date, time_slot, court_number)` 的有效预约建立部分唯一索引；创建预约使用立即事务。因此并发请求不会生成两条有效预约。

每日有效预约上限、每日主动取消上限、开场前取消截止时间和超限限制时长均由服务端规则表执行。管理员取消与解除限制使用独立管理接口和审计记录，不计入用户主动取消次数。

`sports/context` 返回当前规则、用户当天已用/剩余次数、限制状态和预约资料。预约识别学号由用户主动填写并同意保存；服务端可配置为选填或必填。每条预约保存资料快照，之后修改资料不影响历史记录。

体育预约、失物招领和资料库由 `remote_flags` 控制。`MAINTENANCE` 与 `CLOSED` 状态会分别以 HTTP 503 返回 `FEATURE_MAINTENANCE`、`FEATURE_CLOSED`。新建预约、发布、收藏、投诉等写操作不能绕过服务端门禁；已存在的预约仍允许用户查看和取消，避免维护期间失去必要的履约退出路径。

## 资料库

- `GET /api/v1/library`：支持 `q`、`category`、`favorites`、`recent`
- `GET /api/v1/library/{id}`
- `POST /api/v1/library/{id}/view`
- `POST /api/v1/library/{id}/favorite`
- `POST /api/v1/library/{id}/complaints`

第一期只有管理员能发布资料条目，资源地址仅允许 HTTP/HTTPS。收藏、浏览与投诉均关联当前我的长理账号；未实现学生上传或真实付费。

## 独立失物招领

- `GET /api/v1/lost-found`，支持 `type`、`q`、`mine`
- `POST /api/v1/lost-found`
- `GET /api/v1/lost-found/{id}`
- `PATCH /api/v1/lost-found/{id}`
- `DELETE /api/v1/lost-found/{id}`
- `POST /api/v1/lost-found/{id}/favorite`
- `GET /api/v1/lost-found/favorites`
- `POST /api/v1/lost-found/{id}/claim`
- `POST /api/v1/lost-found/{id}/report`

`clientLocalId` 在同一所有者下唯一，用于 Android 旧本地数据的幂等迁移。

## 公共基础能力

- `GET /api/v1/public/bootstrap`：公告、版本、远程开关。
- `GET /api/v1/public/website-announcements`：官网独立公告。`data.websiteAnnouncements` 只返回网站公告的 `id`、`title`、`content`、`publishedAt`、`updatedAt`，不会读取或返回 App 公告。
- `GET /api/v1/public/version/android?currentVersionCode=...`：只返回已发布的 Android 版本检查结果。
- `GET /api/v1/public/service-updates`：服务页“服务动态”聚合接口。只返回用户端必要信息，按优先级稳定排序：
  - `UPDATE`：已发布且高于当前客户端的 Android 版本（版本号、版本名、更新说明、是否强制/最低版本）；
  - `MAINTENANCE` / `CLOSED`：体育预约、失物招领、资料库的维护/关闭状态（模块键、面向用户说明、可选预计恢复时间）；
  - `ANNOUNCEMENT`：已启用且 `service_visible=1` 的公告。
  - 排序：强制/最低版本升级 > 功能关闭 > 功能维护 > 普通版本更新 > 公告。
  - 不返回管理员、审计、内部备注、下载私钥、Token 或系统路径。
- `POST /api/v1/feedback`

## 管理员基础 API

- `GET /api/v1/admin/venues`
- `PATCH /api/v1/admin/venues/{id}`
- `POST /api/v1/admin/venues/{id}/time-slots`
- `POST /api/v1/admin/inventory/block`
- `GET /api/v1/admin/users`
- `GET /api/v1/admin/users/{id}/details`
- `POST /api/v1/admin/users/{id}/phone`
- `PATCH /api/v1/admin/users/{id}`
- `POST /api/v1/admin/users/{id}/clear-restrictions`
- `GET /api/v1/admin/reservation-rules`
- `PUT /api/v1/admin/reservation-rules`
- `GET /api/v1/admin/reservations`
- `GET /api/v1/admin/reservations/{id}`
- `POST /api/v1/admin/reservations/{id}/profile`
- `POST /api/v1/admin/reservations/{id}/verify`
- `POST /api/v1/admin/reservations/{id}/cancel`
- `GET /api/v1/admin/lost-found`
- 管理员也可通过普通失物详情更新/删除接口管理失物信息
- `GET /api/v1/admin/lost-found-reports`
- `GET|POST /api/v1/admin/resources`
- `PATCH|DELETE /api/v1/admin/resources/{id}`
- `GET /api/v1/admin/resource-complaints`
- `PATCH /api/v1/admin/resource-complaints/{id}`
- `POST /api/v1/admin/announcements`
- `PATCH /api/v1/admin/announcements/{id}`
- `DELETE /api/v1/admin/announcements/{id}`
- `GET /api/v1/admin/website-announcements`
- `POST /api/v1/admin/website-announcements`
- `PATCH /api/v1/admin/website-announcements/{id}`
- `DELETE /api/v1/admin/website-announcements/{id}`
- `PUT /api/v1/admin/version/android`
- `PUT /api/v1/admin/flags/{key}`
- `GET /api/v1/admin/feedback`
- `PATCH /api/v1/admin/feedback/{id}`
- `GET /api/v1/admin/audit-logs`
- `GET|POST /api/v1/admin/admins`（仅超级管理员）
- `PATCH /api/v1/admin/admins/{id}`（仅超级管理员）
- `POST /api/v1/admin/admins/{id}/reset-password`（仅超级管理员）
- `GET /api/v1/admin/overview/{sports|lost-found|library|content}`（对应模块工作台）
- `GET /api/v1/admin/resource-activity?user=...&kind=views|favorites`
- `GET /api/v1/admin/lost-found-claims?user=...`

后台角色为 `SUPER_ADMIN`、`SPORTS_OPERATOR`、`LOST_FOUND_OPERATOR`、`LIBRARY_OPERATOR`、`CONTENT_OPERATOR`。模块隔离在服务端路由执行，直接输入其他模块 URL 或调用接口仍返回 403，并记录最小化拒绝审计。

`SPORTS_OPERATOR` 只能在体育预约接口内读取预约履约所需的完整手机号、预约姓名、预约学号、内部用户 ID 和该用户的体育预约历史；全局用户档案、失物、资料库、用户行为和管理员审计接口仍返回 403。体育完整资料查看、核验、取消和规则修改均写入最小必要管理员审计，不在审计详情保存这些字段原文。

管理员凭据只能从本地环境变量引导创建，不提供源码默认口令。
