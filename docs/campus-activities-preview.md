# 校园活动第一版：隔离预览说明

本分支为预览开发，不是正式发布。母版为当前 Backend/Admin 正式工程，基线 71238c79d1d1aae81d549312976e470dc7a10610。Android 基线为 e306adad1484078c21073d9db1c2b8d416953d14；未从历史工程复制业务代码。

## 数据与迁移

沿用现行 SQLite、core.connection、initialize_database 的幂等迁移方式。新增 campus_activities.migrate，事务内执行，迁移记录 ID 为 `20261004_campus_activities_v1`。没有引入第二套 ORM 或迁移工具。

- activities：活动信息、发布学院和可见范围、报名与活动时间、人数上限、发布状态、public_id、发布人、时间戳；预留 checkin_enabled/checkin_start_at/checkin_end_at。
- activity_registrations：用户及学校身份快照、按活动授权的加密电话快照、ACTIVE/CANCELLED、报名/取消时间；预留 checkin_status/checked_in_at。
- activity_profiles：用户确认的学校身份、来源 CLIENT_SCHOOL_LOGIN、同意时间、可复用的加密电话号码。
- users 增加 activity_college_id、activity_publish_scope。
- schema_migrations 记录迁移 ID、applied_at。

报名、取消、重新报名、修改容量均使用 BEGIN IMMEDIATE，事务内读取有效人数。UNIQUE(activity_id,user_id) 与 UNIQUE(activity_id,student_number) 防止重复占位。取消更新状态，不物理删除；重新报名复用原记录。现行用户注销/隐私删除流程仍依法删除该用户的活动资料，区别于普通取消报名。

本轮迁移只落在项目 Output 内的隔离预览库，不连接、不复制生产数据库。回滚应用代码时保留新增表/字段/迁移记录，不执行 DROP 或清库；旧版本忽略新增字段。重新上线同版本可幂等重跑迁移。正式迁移须另行经过用户发布验收。

## API 与权限

学生端：GET /api/v1/activities、GET /activities/mine、GET /activities/{id或public_id}、POST /activities/{id}/register、DELETE /activities/{id}/registration；GET/PUT /activities/profile。所有路径位于 /api/v1。

后台：GET/POST /api/v1/admin/activities、GET/PUT /admin/activities/{id}、GET /admin/activities/{id}/registrations、GET /admin/activities/{id}/registrations/export。导出沿用 JSON 响应封装，返回文件名及 XLSX base64；Admin 生成 Blob 下载。姓名、学号等作为文本单元格，避免公式解释。

复用现行 auth/login、users、sessions 和管理员管理界面。新增 ACTIVITY_PUBLISHER；服务端限制该角色仅进入活动路径，再逐活动校验绑定学院。教师不能修改主办学院/单位冒充其他学院。默认仅本学院可见；超级管理员可授权其发布全校可见活动。超级管理员可管理全部活动、学院账号、停用账号。停用会撤销会话。

普通报名 API 不接受姓名、学号、学院字段；从用户已确认资料取得快照，并在报名事务中核验当前会话安装身份仍绑定该学号。学校 SSO 的层级组织名称取最后一个非空层级作为学院；已保存的层级名称读取时也统一处理。

重要身份边界：当前工程学校绑定来源本来就是 CLIENT_SCHOOL_LOGIN。Android 从既有学校登录 SSO 获取姓名、学号、学院，用户只能确认、不能编辑；服务器核验绑定一致性，但本轮没有新增学校服务器签发的可验证身份凭证。因此不能宣称后端已经获得独立的学校官方身份认证，定制客户端伪造身份断言仍是继承的信任边界。

电话必须活动要求、学生本次确认后才采集；不要求电话的活动名单与导出不出现电话字段。名单不返回用户 UUID、设备 ID、Token 等内部资料。

## 实时性与分享

列表、首页、详情、我的活动均来自同一活动表和报名表；人数实时查询，单次列表在一致的读取事务内生成。Android 操作成功即刷新，前台恢复/周期刷新；后台名单每 5 秒刷新。跨客户端无 WebSocket 推送，因此不同读取时刻存在最多一个刷新周期的显示延迟，不能承诺屏幕上每一瞬间完全同步。

稳定 public_id 与 mychangli://activity/{public_id} Deep Link 已完成，后台可复制；本轮没有官网 HTTPS 活动分享页。签到只有字段预留；未实现签到、复杂问卷、推送和社区功能。

## 自动化验证

`python -m unittest discover -s tests -q`：94 项通过。新增测试覆盖真实 HTTP 流程、重复报名、取消和重新报名、8 个请求竞争 1 个名额、学院隔离、角色越权、账号停用、电话授权、XLSX、迁移幂等、学校绑定变更、用户隐私删除、容量编辑及学院名称归一化。

Admin JavaScript 通过 node --check。真实浏览器已完成学院老师登录、表单填写、预览、发布和报名名单人数检查。内置浏览器下载工具超时，未将浏览器文件落盘标为 PASS；真实 API 的 XLSX 已保存并用 openpyxl 重新打开验证。

## 运行预览

使用现有 run_candidate_local.py --state-dir <项目Output内独立目录> --port 8794，仅监听 127.0.0.1。使用 scripts/seed_activity_preview.py 创建少量明确标识测试的真实活动，拒绝缺少 CANDIDATE_ONLY 标记的目录。

Android 通过 adb reverse tcp:8794 tcp:8794 访问本机活动服务。USB 断开会导致活动接口暂时不可达；原有业务仍访问正式 API。预览凭据、数据库、实际导出及截图只保留在本机 Output，禁止提交 Git。服务保留运行供本轮验收。

## 发布边界

正式用户更新 NO；latest.json 修改 NO；最低支持版本修改 NO；强制更新 NO；GitHub Release/Tag NO；官网正式版本修改 NO；生产迁移/部署 NO。
