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

## 体育预约

- `GET /api/v1/sports/venues`
- `GET /api/v1/sports/availability?venueId=...&date=YYYY-MM-DD`
- `GET /api/v1/sports/reservations`
- `POST /api/v1/sports/reservations`
- `POST /api/v1/sports/reservations/{id}/cancel`

数据库对 `(venue_id, booking_date, time_slot, court_number)` 的有效预约建立部分唯一索引；创建预约使用立即事务。因此并发请求不会生成两条有效预约。

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
- `GET /api/v1/admin/reservations`
- `POST /api/v1/admin/reservations/{id}/cancel`
- `GET /api/v1/admin/lost-found`
- 管理员也可通过普通失物详情更新/删除接口管理失物信息
- `GET /api/v1/admin/lost-found-reports`
- `POST /api/v1/admin/announcements`
- `PUT /api/v1/admin/version/android`
- `PUT /api/v1/admin/flags/{key}`
- `GET /api/v1/admin/feedback`
- `PATCH /api/v1/admin/feedback/{id}`

管理员凭据只能从本地环境变量引导创建，不提供源码默认口令。
