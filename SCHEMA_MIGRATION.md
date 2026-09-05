# 本地数据模型与未来数据库迁移

## 当前 SQLite 数据域

- `users`、`sessions`：川农用户、管理员、角色、账号状态与会话。所有业务以内部 `users.id` 关联，手机号不是业务主键。
- `reservations`、`reservation_*`、`venues`、`time_slots`、`inventory_blocks`：场地、库存、预约、规则、资料快照与操作事件。
- `lost_found_*`：失物信息、收藏、认领申请和举报。
- `resource_*`：资料条目、浏览、收藏和投诉。
- `feedback`、`feedback_events`：用户反馈及公开/内部处理轨迹。
- `user_events`、`admin_audit_logs`、`risk_candidates`：用户行为、管理员审计和异常候选。
- `announcements`、`website_announcements`、`app_versions`、`remote_flags`：APP公告、官网独立公告、Android 版本和功能开关。网站公告表由 `schema.sql` 的 `CREATE TABLE IF NOT EXISTS` 幂等创建；不改动 APP公告表。

完整手机号使用不可逆摘要进行精确检索，并以本地密钥加密保存可恢复值；密码只保存加盐 `scrypt` 摘要。教务账号、密码、Cookie、Session、Token、课表、成绩和考试不进入这些表。

## 迁移与索引策略

启动时只做幂等的增列、默认值补齐与索引创建，不删除旧列或现有测试数据。常用索引覆盖手机号摘要、预约学号/姓名摘要、内部用户 ID、账号角色/状态、事件时间、预约日期与状态。有效预约仍由部分唯一索引保证同一场地、日期、时段和编号只能有一条有效记录。

本轮为 `remote_flags` 增加 `updated_by`、`change_note`，并保留原有 JSON payload 兼容旧记录；公共接口只下发 App 所需的状态、用户提示、预计恢复时间和更新时间。预约列表增加场馆、日期、状态、核验状态、场地号和预约编号组合索引，支持体育运营分页检索。所有增列与索引均通过 `initialize_database()` 幂等创建，可在已有本地数据库上重复启动。

迁移到 PostgreSQL/MySQL 时：

1. 先冻结写入并做 SQLite 一致性备份；
2. 将毫秒时间戳统一映射为 `BIGINT` 或带时区时间；
3. 将 SQLite 部分唯一索引改为 PostgreSQL 条件唯一索引，或在 MySQL 中以生成列/事务锁实现；
4. 保持内部 UUID、外键、状态枚举和手机号摘要不变；
5. 在影子库校验每表数量、外键、预约唯一性和抽样业务查询；
6. 通过服务层数据源配置切换，不让 Android 直接访问数据库；
7. 完成回滚演练后才允许正式切换。

当前文档只用于未来准备，不连接生产数据库。

## 迁移版本机制

本地 SQLite 采用“幂等增量迁移”策略，没有独立版本表：

1. `schema.sql` 是完整基线，新库直接执行；
2. 已有库通过 `server.initialize_database()` 内的 `ensure_column()` 按需补齐新列，重复执行安全；
3. 结构变更与索引创建全部集中在初始化流程中，迁移顺序即初始化流程中的执行顺序；
4. 迁移失败会抛出明确异常并回滚当前事务，服务不会以半迁移状态继续启动；
5. 未来若引入 PostgreSQL/MySQL，建议启用正式迁移工具（版本号、正向/回滚脚本、校验和），并将上述幂等列补充逻辑保留为兼容层。

本轮工程化将 SQLite 连接与初始化基础能力移入 `core.py`，`server.py` 保持业务路由不变。新增 `core.configure_test_runtime()` 用于测试隔离，测试数据库路径不得等于本地开发库路径。
