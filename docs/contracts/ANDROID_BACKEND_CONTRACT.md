# Android ↔ Backend Contract

合同基于 Android `CampusToolsApi.kt` 与 Backend 当前路由。Base URL 必须以 `/api/v1/` 结尾。成功为 `{"ok":true,"data":...}`；失败为 HTTP 4xx/5xx 与 `{"ok":false,"error":{"code":"...","message":"..."}}`。

Auth 列：Public 不要求 token；Bearer 要求 `Authorization: Bearer <token>`；Admin 还执行服务端 RBAC。常见错误为 400 validation、401 unauthorized/session expired、403 forbidden/account/feature、404、409、413、429、503；公网 500 不含 stack trace。

| Method | Path | Auth | Request | Response | Feature |
| --- | --- | --- | --- | --- | --- |
| GET | `/health` | Public | — | `{status:"ok"}` | liveness |
| GET | `/ready` | Public | — | `{status:"ready"}` / 503 | DB readiness |
| POST | `/auth/device` | Public + rate | `installationId` | token + user | legacy migration identity |
| POST | `/auth/register` | Public + rate | phone/password/nickname/installationId | token + PHONE user | account |
| POST | `/auth/login` | Public + rate | phone or admin username + password | token + user | account/Admin |
| POST | `/auth/logout` | Bearer | `{}` | `loggedOut` | account |
| GET/PATCH | `/auth/me`, `/auth/profile` | Bearer | profile uses nickname | `AuthUser` | account |
| GET/PUT/DELETE | `/auth/reservation-profile` | Bearer | studentId/name/consented | reservation context | sports profile |
| DELETE | `/auth/account` | Bearer | password | deleted | account deletion |
| GET | `/public/bootstrap` | Public | — | flags + announcements + published version | startup config |
| GET | `/public/version/android` | Public | `currentVersionCode` query | update policy + version | app update |
| GET | `/public/service-updates` | Public | — | update/maintenance/announcement cards | service page |
| GET | `/sports/venues` | Bearer | — | venues/time slots | sports |
| GET | `/sports/context` | Bearer | — | rules/usage/restriction/profile | sports |
| GET | `/sports/availability` | Bearer | venueId/date | slots/courts | sports |
| GET/POST | `/sports/reservations` | Bearer | POST venue/date/slot/court | reservations | sports |
| POST | `/sports/reservations/{id}/cancel` | Bearer | `{}` | reservation | sports |
| GET/POST | `/lost-found` | Bearer; POST rate | GET filters / POST item + optional base64 image | posts/post | lost-found |
| GET/PATCH/DELETE | `/lost-found/{id}` | Bearer | PATCH item | post/delete result | lost-found |
| GET | `/lost-found/favorites` | Bearer | — | posts | lost-found |
| POST | `/lost-found/{id}/favorite` | Bearer | `{}` | favorite result | lost-found |
| POST | `/lost-found/{id}/claim` | Bearer | `{}` | claim result | lost-found |
| POST | `/lost-found/{id}/report` | Bearer + rate | reason | report result | lost-found |
| GET | `/library` | Bearer or flag-controlled guest | q/category/favorites/recent | resources | resource library |
| GET | `/library/{id}` | Bearer or flag-controlled guest | — | resource | resource library |
| POST | `/library/{id}/view` | optional/required by flag state | `{}` | view result | resource library |
| POST | `/library/{id}/favorite` | Bearer | `{}` | favorite result | resource library |
| POST | `/library/{id}/complaints` | Bearer | reason/detail | complaint result | resource library |
| POST | `/feedback` | Bearer + rate | category/title/content/appVersion | feedback id/status | feedback |
| GET | `/feedback`, `/feedback/{id}` | Bearer | — | own feedback/events | feedback |
| all | `/admin/*` and `/api/v1/admin/*` | Admin RBAC | Admin UI/API models | role-scoped data | operations |

图片字段仍为纯 base64，不接收客户端文件名或服务器路径。Backend 重新解码/规范化并返回 `/uploads/<random-id>.<ext>`；Android `absoluteUrl()` 以 API host 拼接。

学校教务账号、密码、验证码、Cookie、课表、成绩和考试数据不在任何上述请求模型内，禁止加入。

## 兼容性结论

当前 Android DTO 与 Backend 字段兼容。Backend 新增的 `minimumVersionViolated`/`mandatoryUpdate` 会被旧 DTO 安全忽略，Android 现有 `VersionPolicy` 已按 minimum/force 计算。没有发现必须在本模块跨仓库修复的破坏性不一致。
