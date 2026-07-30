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
- `POST /api/v1/auth/logout`
- `DELETE /api/v1/auth/account`

手机号注册只保存不可逆 HMAC 摘要和脱敏展示值，密码只保存加盐 `scrypt` 摘要。学校教务账号、密码、Cookie 与会话不进入本后端。校内身份字段仅为未来获得正式授权后的兼容预留。

## 体育预约

- `GET /api/v1/sports/venues`
- `GET /api/v1/sports/availability?venueId=...&date=YYYY-MM-DD`
- `GET /api/v1/sports/reservations`
- `POST /api/v1/sports/reservations`
- `POST /api/v1/sports/reservations/{id}/cancel`

数据库对 `(venue_id, booking_date, time_slot, court_number)` 的有效预约建立部分唯一索引；创建预约使用立即事务。因此并发请求不会生成两条有效预约。

每日有效预约上限、每日主动取消上限、开场前取消截止时间和超限限制时长均由服务端规则表执行。管理员取消与解除限制使用独立管理接口和审计记录，不计入用户主动取消次数。

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
- `POST /api/v1/feedback`

## 管理员基础 API

- `GET /api/v1/admin/venues`
- `PATCH /api/v1/admin/venues/{id}`
- `POST /api/v1/admin/venues/{id}/time-slots`
- `POST /api/v1/admin/inventory/block`
- `GET /api/v1/admin/users`
- `PATCH /api/v1/admin/users/{id}`
- `POST /api/v1/admin/users/{id}/clear-restrictions`
- `GET /api/v1/admin/reservation-rules`
- `PUT /api/v1/admin/reservation-rules`
- `GET /api/v1/admin/reservations`
- `POST /api/v1/admin/reservations/{id}/cancel`
- `GET /api/v1/admin/lost-found`
- 管理员也可通过普通失物详情更新/删除接口管理失物信息
- `GET /api/v1/admin/lost-found-reports`
- `POST /api/v1/admin/announcements`
- `PATCH /api/v1/admin/announcements/{id}`
- `DELETE /api/v1/admin/announcements/{id}`
- `PUT /api/v1/admin/version/android`
- `PUT /api/v1/admin/flags/{key}`
- `GET /api/v1/admin/feedback`
- `PATCH /api/v1/admin/feedback/{id}`
- `GET /api/v1/admin/audit-logs`

管理员凭据只能从本地环境变量引导创建，不提供源码默认口令。
